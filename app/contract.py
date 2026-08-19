"""Stay in step with an engine we do not control.

Two independent checks, both non-blocking by design. This GUI is meant to keep
working across upstream releases, so a surprise never becomes a crash: it
becomes a banner that names exactly what changed.

* :func:`check_contract` reads the engine's own ``/openapi.json`` at startup and
  confirms the routes and clean options we rely on still exist. It also reports
  the option list the engine actually accepts, which is what the UI renders — so
  an option added or dropped upstream shows up without a code change here.
* :func:`fetch_latest_release` asks GitHub for the newest engine release, purely
  so the UI can say "a newer version exists". This is the app's only outbound
  network call and it can be switched off entirely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

#: Routes this GUI calls. Missing ones are reported, not fatal — the affected
#: feature degrades (e.g. no batch endpoint means we fall back to per-file calls).
REQUIRED_PATHS = ("/health", "/capabilities", "/inspect", "/clean")
OPTIONAL_PATHS = ("/inspect/batch", "/clean/batch")

#: Clean options the UI offers today, with the safe default and a warning for
#: the ones that can change content beyond the watermark itself.
KNOWN_OPTIONS: dict[str, dict[str, Any]] = {
    "keep_non_ai_metadata": {
        "label": "Keep non-AI metadata",
        "help": "Preserve camera, author and timestamp fields; remove only AI provenance markers.",
        "default": True,
        "risk": None,
    },
    "also_layer_a_text": {
        "label": "Also scan text inside documents",
        "help": "Look for invisible characters in the text parts of PDFs, DOCX, EPUB and friends.",
        "default": True,
        "risk": None,
    },
    "aggressive_homoglyphs": {
        "label": "Aggressive homoglyph replacement",
        "help": "Also replace Latin lookalikes and fullwidth characters.",
        "default": False,
        "risk": "Can alter legitimate non-Latin text and code samples.",
    },
    "nfkc": {
        "label": "Unicode NFKC normalisation",
        "help": "Normalise the whole text to NFKC after cleaning.",
        "default": False,
        "risk": "Rewrites ligatures, fractions and formatting characters throughout the document.",
    },
    "strip_all_metadata": {
        "label": "Strip all metadata",
        "help": "Remove every metadata field, not just the AI provenance ones.",
        "default": False,
        "risk": "Destroys copyright, camera and authorship information permanently.",
    },
}

#: Options we never surface: they need optional heavy backends, or belong to the
#: detection layers this GUI deliberately leaves out.
HIDDEN_OPTIONS = {"remove_pixel", "detect_before", "detect_after"}


@dataclass
class ContractStatus:
    ok: bool = True
    checked: bool = False
    missing_paths: list[str] = field(default_factory=list)
    degraded_paths: list[str] = field(default_factory=list)
    #: Option names the engine currently accepts, or None when unknown.
    accepted_options: list[str] | None = None
    unknown_options: list[str] = field(default_factory=list)
    dropped_options: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def batch_supported(self) -> bool:
        return not self.degraded_paths

    def messages(self) -> list[str]:
        """Drift the user should act on."""
        out: list[str] = []
        if self.error:
            out.append(f"Could not read the engine's API contract: {self.error}")
        if self.missing_paths:
            out.append(
                "The engine no longer serves: " + ", ".join(self.missing_paths) + "."
            )
        if self.dropped_options:
            out.append(
                "The engine dropped these options: "
                + ", ".join(self.dropped_options)
                + ". They are hidden from the Advanced panel."
            )
        if self.unknown_options:
            out.append(
                "The engine gained options this UI does not expose yet: "
                + ", ".join(self.unknown_options)
                + "."
            )
        return out

    def notes(self) -> list[str]:
        """Drift that changes nothing the user can see or fix.

        Kept out of `messages` so the UI does not shout about it: a missing
        batch endpoint costs a few extra round trips and nothing else.
        """
        if not self.degraded_paths:
            return []
        return [
            "This engine build has no batch endpoints ("
            + ", ".join(self.degraded_paths)
            + "), so files are processed one at a time."
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "batch_supported": self.batch_supported,
            "missing_paths": self.missing_paths,
            "degraded_paths": self.degraded_paths,
            "unknown_options": self.unknown_options,
            "dropped_options": self.dropped_options,
            "messages": self.messages(),
            "notes": self.notes(),
        }


def extract_clean_options(spec: dict[str, Any]) -> list[str] | None:
    """Read the accepted clean-option names out of an OpenAPI spec.

    Returns None when the spec does not describe them in the shape we know; the
    caller then falls back to the built-in list rather than hiding everything.
    """
    try:
        schema = spec["paths"]["/clean"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        options = schema["properties"]["options"]["properties"]
    except (KeyError, TypeError):
        return None
    if not isinstance(options, dict):
        return None
    return sorted(options)


def check_contract(spec: dict[str, Any] | None, error: str | None = None) -> ContractStatus:
    """Compare the engine's advertised API against what this GUI needs."""
    status = ContractStatus()
    if spec is None:
        status.error = error or "no specification returned"
        status.ok = False
        return status

    status.checked = True
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        status.error = "the specification has no usable `paths` section"
        status.ok = False
        return status

    status.missing_paths = [p for p in REQUIRED_PATHS if p not in paths]
    status.degraded_paths = [p for p in OPTIONAL_PATHS if p not in paths]

    accepted = extract_clean_options(spec)
    status.accepted_options = accepted
    if accepted is not None:
        offered = set(KNOWN_OPTIONS)
        status.dropped_options = sorted(offered - set(accepted))
        status.unknown_options = sorted(set(accepted) - offered - HIDDEN_OPTIONS)

    status.ok = not status.missing_paths
    return status


def ui_options(status: ContractStatus) -> list[dict[str, Any]]:
    """The Advanced-panel option list, filtered by what the engine accepts."""
    accepted = status.accepted_options
    out: list[dict[str, Any]] = []
    for name, meta in KNOWN_OPTIONS.items():
        if accepted is not None and name not in accepted:
            continue
        out.append({"name": name, **meta})
    return out


def default_options(status: ContractStatus) -> dict[str, bool]:
    return {opt["name"]: bool(opt["default"]) for opt in ui_options(status)}


@dataclass
class ReleaseInfo:
    current: str | None = None
    latest: str | None = None
    url: str | None = None
    published: str | None = None
    outdated: bool = False
    checked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "url": self.url,
            "published": self.published,
            "outdated": self.outdated,
            "checked": self.checked,
        }


class ReleaseChecker:
    """Caches the newest upstream release for a day; failures are silent."""

    def __init__(self, url: str, *, enabled: bool = True, ttl: float = 86400.0) -> None:
        self._url = url
        self._enabled = enabled
        self._ttl = ttl
        self._cached: ReleaseInfo | None = None
        self._fetched_at = 0.0

    async def get(self, current_version: str | None) -> ReleaseInfo:
        if not self._enabled:
            return ReleaseInfo(current=current_version, checked=False)

        now = time.monotonic()
        if self._cached is None or now - self._fetched_at > self._ttl:
            self._cached = await self._fetch()
            self._fetched_at = now

        info = ReleaseInfo(
            current=current_version,
            latest=self._cached.latest,
            url=self._cached.url,
            published=self._cached.published,
            checked=self._cached.checked,
        )
        info.outdated = bool(
            info.latest
            and current_version
            and _normalise(current_version) != _normalise(info.latest)
        )
        return info

    async def _fetch(self) -> ReleaseInfo:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    self._url, headers={"Accept": "application/vnd.github+json"}
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError):
            return ReleaseInfo(checked=False)
        return ReleaseInfo(
            latest=data.get("tag_name"),
            url=data.get("html_url"),
            published=data.get("published_at"),
            checked=True,
        )


def _normalise(version: str) -> str:
    return version.strip().lstrip("vV")
