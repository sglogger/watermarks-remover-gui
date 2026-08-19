"""FastAPI application: the browser-facing half of the Watermarks Detection & Remover GUI.

Responsibilities, all of which exist because the engine cannot do them itself:

* translate multipart uploads into the engine's JSON/base64 envelope;
* keep the engine's API key out of the browser (the engine also sends no CORS
  headers, so a static page could not call it anyway);
* refuse audio and video, and anything outside the supported format list;
* turn "original vs cleaned" into exact highlight positions;
* hold scan results in memory so Remove does not re-upload.

The engine itself is never modified, vendored, or reimplemented.
"""

from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Sequence

from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, contract, diffmark, formats
from .cache import ScanCache, ScanEntry
from .config import Settings, get_settings
from .contract import ContractStatus, ReleaseChecker
from .models import (
    CleanItem,
    CleanRequest,
    CleanResponse,
    LoginRequest,
    ScanItem,
    ScanResponse,
    TextScanRequest,
)
from .security import (
    SESSION_COOKIE,
    AuthMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from .upstream import UpstreamClient, UpstreamError, decode_cleaned

log = logging.getLogger("wr-gui")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

#: Above this size the original text is not echoed back to the browser for
#: inline highlighting; the findings list is shown instead.
MAX_INLINE_TEXT_BYTES = 1_000_000


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    app.state.client = UpstreamClient(
        settings.core_url,
        api_key=settings.core_api_key,
        timeout=settings.core_timeout,
    )
    app.state.cache = ScanCache(settings.cache_ttl, settings.cache_max_bytes)
    app.state.releases = ReleaseChecker(
        settings.releases_url, enabled=settings.update_check
    )
    app.state.contract = await _run_contract_check(app.state.client)
    for message in app.state.contract.messages():
        log.warning("engine contract: %s", message)
    for note in app.state.contract.notes():
        log.info("engine contract: %s", note)
    try:
        yield
    finally:
        await app.state.client.aclose()


async def _run_contract_check(client: UpstreamClient) -> ContractStatus:
    """Read the engine's own OpenAPI spec. Never fatal — the UI reports it."""
    try:
        spec = await client.openapi()
    except UpstreamError as exc:
        return contract.check_contract(None, error=str(exc))
    if not isinstance(spec, dict):
        return contract.check_contract(None, error="specification was not an object")
    return contract.check_contract(spec)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Watermarks Detection & Remover GUI",
        description="Web frontend for the watermarks-remover engine.",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.contract = ContractStatus()

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(AuthMiddleware, token=settings.auth_token)
    app.add_middleware(RateLimitMiddleware, per_minute=settings.rate_limit_per_min)

    _register_routes(app)
    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_options(raw: Any, status: ContractStatus) -> dict[str, Any]:
    """Keep only options the engine currently accepts, coerced to booleans.

    The engine rejects a request outright if it carries an option it does not
    know, so filtering here is what keeps the UI working across upstream
    releases that add or drop options.
    """
    allowed = contract.default_options(status)
    out = dict(allowed)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in allowed:
                out[key] = bool(value)
    return out


def _report_hits(report: Any, _depth: int = 0) -> int | None:
    """Best-effort total hit count from a report of unknown shape."""
    if _depth > 4 or not isinstance(report, dict):
        return None
    for key in ("suspicious_total", "total", "hit_count"):
        value = report.get(key)
        if isinstance(value, int):
            return value
    total: int | None = None
    for value in report.values():
        if isinstance(value, dict):
            nested = _report_hits(value, _depth + 1)
            if nested is not None:
                total = (total or 0) + nested
    return total


def _report_findings(report: Any) -> list[str]:
    """Human-readable leftovers from a report, for the post-clean verdict."""
    if not isinstance(report, dict):
        return []
    out: list[str] = []
    for key in ("findings", "post_findings", "notes"):
        value = report.get(key)
        if isinstance(value, list):
            out.extend(str(entry) for entry in value if entry)
    return out[:20]


def _report_hit_list(report: Any) -> list[Any]:
    if isinstance(report, dict):
        hits = report.get("hits")
        if isinstance(hits, list):
            return hits
    return []


def _as_text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _cleaned_name(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return f"{name}.cleaned"
    return f"{stem}.cleaned.{ext}"


async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    """Read an upload, stopping as soon as it exceeds *limit*."""
    buffer = bytearray()
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise formats.RejectedFormat(
                "too_large",
                f"{upload.filename!r} is larger than the {limit // (1024 * 1024)} MB limit.",
            )
    return bytes(buffer)


# ---------------------------------------------------------------------------
# Core scan / clean pipeline
# ---------------------------------------------------------------------------


async def _scan_payloads(
    app: FastAPI,
    payloads: list[tuple[str, bytes]],
    options: dict[str, Any],
) -> tuple[list[ScanItem], list[str]]:
    """Inspect every payload, and highlight the ones whose output stays text."""
    client: UpstreamClient = app.state.client
    cache: ScanCache = app.state.cache
    status: ContractStatus = app.state.contract
    settings: Settings = app.state.settings
    warnings: list[str] = []

    items: list[ScanItem] = []
    accepted: list[tuple[int, str, bytes, formats.FormatInfo]] = []

    for index, (name, data) in enumerate(payloads):
        try:
            info = formats.classify(name, data)
        except formats.RejectedFormat as exc:
            items.append(
                ScanItem(name=name, ok=False, size=len(data), error=exc.detail)
            )
            continue
        items.append(
            ScanItem(
                name=name,
                kind=info.kind,
                size=len(data),
                highlightable=info.highlightable,
            )
        )
        accepted.append((len(items) - 1, name, data, info))

    if not accepted:
        return items, warnings

    inspect_pairs = [(name, data) for _, name, data, _ in accepted]
    try:
        reports = await _inspect_all(client, inspect_pairs, status, settings)
    except UpstreamError as exc:
        for slot, name, _, _ in accepted:
            items[slot].ok = False
            items[slot].error = str(exc)
        return items, warnings

    # Highlighting needs the cleaned bytes, so clean the text-like items now.
    clean_targets: list[tuple[int, str, bytes]] = []
    for (slot, name, data, info), report in zip(accepted, reports):
        item = items[slot]
        if not report.get("ok", True):
            item.ok = False
            item.error = str(report.get("error") or "the engine could not process this file")
            continue
        item.kind = str(report.get("kind") or info.kind)
        item.suspicious = bool(report.get("suspicious"))
        item.report = report.get("report")
        item.id = cache.put(
            ScanEntry(
                name=name,
                ext=info.ext,
                kind=item.kind,
                mime=info.mime,
                original=data,
                report=item.report,
            )
        )
        # Deliberately not gated on `suspicious`. The engine's container
        # inspector reports metadata findings but does not look for invisible
        # characters, so a Markdown file full of zero-width spaces comes back
        # "not suspicious" while /clean happily strips eight of them. The clean
        # output is the only complete answer, so ask for it either way.
        if info.highlightable and _as_text(data) is not None:
            clean_targets.append((slot, name, data))

    if clean_targets:
        try:
            cleaned_results = await _clean_all(
                client,
                [(name, data) for _, name, data in clean_targets],
                options,
                status,
                settings,
            )
        except UpstreamError as exc:
            warnings.append(f"Could not compute highlight positions: {exc}")
            cleaned_results = [{"ok": False, "error": str(exc)}] * len(clean_targets)

        for (slot, name, data), result in zip(clean_targets, cleaned_results):
            item = items[slot]
            if not result.get("ok", True):
                warnings.append(
                    f"{name}: positions unavailable ({result.get('error') or 'clean failed'})."
                )
                continue
            try:
                cleaned_bytes = decode_cleaned(result)
            except UpstreamError as exc:
                warnings.append(f"{name}: {exc}")
                continue
            if cleaned_bytes is None:
                continue

            original_text = _as_text(data)
            cleaned_text = _as_text(cleaned_bytes)
            if original_text is None or cleaned_text is None:
                continue

            highlight = diffmark.highlight(
                original_text, cleaned_text, _report_hit_list(item.report)
            )
            if not highlight.spans:
                continue
            diffmark.to_utf16_offsets(original_text, highlight.spans)
            item.highlight = highlight.to_dict()
            # The diff found something the inspector did not report; the file
            # is watermarked whatever the inspector said.
            item.suspicious = True
            if item.id:
                cache.update(item.id, cleaned_bytes, options, result.get("report"))
            if len(data) <= MAX_INLINE_TEXT_BYTES:
                item.text = original_text
            if not highlight.exact:
                warnings.append(
                    f"{name}: the change is too large to mark character by character; "
                    "the affected region is highlighted as a block."
                )

    return items, warnings


async def _inspect_all(
    client: UpstreamClient,
    pairs: Sequence[tuple[str, bytes]],
    status: ContractStatus,
    settings: Settings,
) -> list[dict[str, Any]]:
    if status.batch_supported and len(pairs) > 1:
        return await client.inspect_batch(pairs, cap=settings.core_batch_cap)
    return [await client.inspect(name, data) for name, data in pairs]


async def _clean_all(
    client: UpstreamClient,
    pairs: Sequence[tuple[str, bytes]],
    options: dict[str, Any],
    status: ContractStatus,
    settings: Settings,
) -> list[dict[str, Any]]:
    if status.batch_supported and len(pairs) > 1:
        return await client.clean_batch(pairs, options, cap=settings.core_batch_cap)
    return [await client.clean(name, data, options) for name, data in pairs]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        page = WEB_DIR / "index.html"
        if not page.is_file():
            return JSONResponse({"error": "frontend assets are missing"}, status_code=500)
        return FileResponse(page, media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        icon = WEB_DIR / "favicon.svg"
        if icon.is_file():
            return FileResponse(icon, media_type="image/svg+xml")
        return Response(status_code=204)

    @app.get("/api/ping")
    async def ping() -> dict[str, Any]:
        return {"ok": True, "auth_required": bool(app.state.settings.auth_enabled)}

    @app.post("/api/login")
    async def login(payload: LoginRequest) -> Response:
        settings: Settings = app.state.settings
        if not settings.auth_enabled:
            return JSONResponse({"ok": True, "auth_required": False})
        import secrets as _secrets

        if not _secrets.compare_digest(payload.token, settings.auth_token):
            return JSONResponse(
                {"error": "invalid_token", "detail": "That access token is not valid."},
                status_code=401,
            )
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            settings.auth_token,
            httponly=True,
            samesite="strict",
            max_age=12 * 3600,
        )
        return response

    @app.get("/api/formats")
    async def supported_formats() -> dict[str, Any]:
        settings: Settings = app.state.settings
        status: ContractStatus = app.state.contract
        return {
            "extensions": sorted(formats.ALLOWED_EXTS),
            "accept": formats.accept_attribute(),
            "text_formats": sorted(formats.TEXT_INPUT_FORMATS),
            "highlightable": sorted(formats.HIGHLIGHTABLE_EXTS),
            "max_upload_mb": settings.max_upload_mb,
            "max_files": settings.max_files,
            "options": contract.ui_options(status),
        }

    @app.get("/api/status")
    async def status_route() -> dict[str, Any]:
        client: UpstreamClient = app.state.client
        releases: ReleaseChecker = app.state.releases
        status: ContractStatus = app.state.contract

        engine: dict[str, Any] = {"ok": False, "url": app.state.settings.core_url}
        version: str | None = None
        try:
            health = await client.health()
            engine["ok"] = bool(health.get("ok", True))
            version = health.get("version")
            engine["version"] = version
        except UpstreamError as exc:
            engine["error"] = str(exc)

        if engine["ok"]:
            try:
                engine["capabilities"] = await client.capabilities()
            except UpstreamError as exc:
                engine["capabilities_error"] = str(exc)

            if not status.checked:
                # The engine may have come up after us; retry the contract check.
                app.state.contract = status = await _run_contract_check(client)

        release = await releases.get(version)
        return {
            "app": {"name": "watermarks-remover-gui", "version": __version__},
            "engine": engine,
            "contract": status.to_dict(),
            "release": release.to_dict(),
            "cache": app.state.cache.stats(),
            "auth_required": app.state.settings.auth_enabled,
        }

    @app.post("/api/scan/text", response_model=ScanResponse)
    async def scan_text(payload: TextScanRequest) -> ScanResponse:
        status: ContractStatus = app.state.contract
        text = payload.text
        if not text.strip():
            return ScanResponse(items=[], warnings=["Nothing to scan — the text is empty."])

        filename = formats.TEXT_INPUT_FORMATS.get(payload.format)
        if filename is None:
            return ScanResponse(
                items=[],
                warnings=[
                    f"Unknown text format {payload.format!r}. "
                    "Use one of: " + ", ".join(sorted(formats.TEXT_INPUT_FORMATS)) + "."
                ],
            )

        options = _sanitize_options(payload.options, status)
        items, warnings = await _scan_payloads(
            app, [(filename, text.encode("utf-8"))], options
        )
        # The browser already holds the text it just sent; no need to echo it.
        for item in items:
            item.text = None
        return ScanResponse(items=items, warnings=warnings)

    @app.post("/api/scan/files", response_model=ScanResponse)
    async def scan_files(
        files: list[UploadFile] = File(default=[]),
        options: str = Form(default="{}"),
    ) -> ScanResponse:
        settings: Settings = app.state.settings
        status: ContractStatus = app.state.contract
        warnings: list[str] = []

        if not files:
            return ScanResponse(items=[], warnings=["No files were uploaded."])
        if len(files) > settings.max_files:
            warnings.append(
                f"Only the first {settings.max_files} files were processed "
                f"({len(files)} were selected)."
            )
            files = files[: settings.max_files]

        try:
            parsed_options = json.loads(options) if options else {}
        except ValueError:
            parsed_options = {}
            warnings.append("Options could not be read; safe defaults were used.")
        clean_options = _sanitize_options(parsed_options, status)

        payloads: list[tuple[str, bytes]] = []
        rejected: list[ScanItem] = []
        for upload in files:
            name = upload.filename or "unnamed"
            try:
                data = await _read_upload(upload, settings.max_upload_bytes)
            except formats.RejectedFormat as exc:
                rejected.append(ScanItem(name=name, ok=False, error=exc.detail))
                continue
            finally:
                await upload.close()
            payloads.append((name, data))

        items, scan_warnings = await _scan_payloads(app, payloads, clean_options)
        return ScanResponse(items=rejected + items, warnings=warnings + scan_warnings)

    @app.post("/api/clean", response_model=CleanResponse)
    async def clean_route(payload: CleanRequest = Body(...)) -> CleanResponse:
        client: UpstreamClient = app.state.client
        cache: ScanCache = app.state.cache
        status: ContractStatus = app.state.contract
        settings: Settings = app.state.settings

        options = _sanitize_options(payload.options, status)
        items: list[CleanItem] = []
        warnings: list[str] = []

        pending: list[tuple[int, str, ScanEntry]] = []
        verify_targets: list[tuple[int, str, bytes]] = []

        for scan_id in payload.ids:
            entry = cache.get(scan_id)
            if entry is None:
                items.append(
                    CleanItem(
                        id=scan_id,
                        name="(expired)",
                        ok=False,
                        error="This scan expired. Scan the file again.",
                    )
                )
                continue
            item = CleanItem(id=scan_id, name=entry.name)
            items.append(item)
            if entry.cleaned is not None and entry.options == options:
                # Already computed during the scan under these exact options.
                item.report = entry.clean_report
                _finish_clean_item(item, entry)
                verify_targets.append((len(items) - 1, entry.name, entry.cleaned))
                continue
            pending.append((len(items) - 1, scan_id, entry))

        if pending:
            pairs = [(entry.name, entry.original) for _, _, entry in pending]
            try:
                results = await _clean_all(client, pairs, options, status, settings)
            except UpstreamError as exc:
                for slot, _, _ in pending:
                    items[slot].ok = False
                    items[slot].error = str(exc)
                results = []

            for (slot, scan_id, entry), result in zip(pending, results):
                item = items[slot]
                if not result.get("ok", True):
                    item.ok = False
                    item.error = str(
                        result.get("error") or "the engine could not clean this file"
                    )
                    continue
                try:
                    cleaned_bytes = decode_cleaned(result)
                except UpstreamError as exc:
                    item.ok = False
                    item.error = str(exc)
                    continue
                if cleaned_bytes is None:
                    item.ok = False
                    item.error = "the engine returned no cleaned data"
                    continue
                cache.update(scan_id, cleaned_bytes, options, result.get("report"))
                item.report = result.get("report")
                _finish_clean_item(item, cache.get(scan_id) or entry)
                verify_targets.append((slot, entry.name, cleaned_bytes))

        # Prove it worked: run the cleaned bytes back through /inspect.
        if verify_targets:
            try:
                checks = await _inspect_all(
                    client,
                    [(name, data) for _, name, data in verify_targets],
                    status,
                    settings,
                )
            except UpstreamError as exc:
                warnings.append(f"Could not verify the cleaned output: {exc}")
                checks = []
            for (slot, _, _), check in zip(verify_targets, checks):
                item = items[slot]
                item.verified = not bool(check.get("suspicious"))
                item.remaining_hits = _report_hits(check.get("report"))
                if not item.verified:
                    item.remaining_findings = _report_findings(check.get("report"))

        return CleanResponse(items=items, warnings=warnings)

    @app.get("/api/download/{scan_id}", include_in_schema=False)
    async def download(scan_id: str) -> Response:
        entry = app.state.cache.get(scan_id)
        if entry is None or entry.cleaned is None:
            return JSONResponse(
                {"error": "not_found", "detail": "Nothing cleaned under that id."},
                status_code=404,
            )
        name = _cleaned_name(entry.name)
        return Response(
            content=entry.cleaned,
            media_type=entry.mime,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/download.zip", include_in_schema=False)
    async def download_zip(ids: str = "") -> Response:
        cache: ScanCache = app.state.cache
        wanted = [i for i in ids.split(",") if i]
        buffer = io.BytesIO()
        written = 0
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            used: set[str] = set()
            for scan_id in wanted:
                entry = cache.get(scan_id)
                if entry is None or entry.cleaned is None:
                    continue
                name = _cleaned_name(entry.name)
                candidate, counter = name, 2
                while candidate in used:
                    candidate = f"{counter}-{name}"
                    counter += 1
                used.add(candidate)
                archive.writestr(candidate, entry.cleaned)
                written += 1
        if not written:
            return JSONResponse(
                {"error": "not_found", "detail": "None of those ids have cleaned output."},
                status_code=404,
            )
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="cleaned-files.zip"'},
        )


def _finish_clean_item(item: CleanItem, entry: ScanEntry) -> None:
    """Fill in the download link, and the cleaned text when it is text."""
    if entry.cleaned is None:
        return
    item.cleaned_name = _cleaned_name(entry.name)
    item.download_url = f"/api/download/{item.id}"
    if entry.ext in formats.HIGHLIGHTABLE_EXTS and len(entry.cleaned) <= MAX_INLINE_TEXT_BYTES:
        item.text = _as_text(entry.cleaned)


app = create_app()


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    settings = get_settings()
    logging.basicConfig(
        level=os.environ.get("GUI_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        "app.main:app", host=settings.bind, port=settings.port, log_level="info"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
