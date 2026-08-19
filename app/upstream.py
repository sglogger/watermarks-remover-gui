"""Thin async client for the upstream watermarks-remover HTTP service.

This is the *only* place that knows the engine's wire format. Nothing from the
upstream source tree is vendored or reimplemented — we speak its published JSON
API and nothing else, so upgrading the engine is a matter of changing an image
tag.

The engine takes base64 inside a JSON envelope rather than multipart, and sends
no CORS headers, which is why the browser talks to us and we talk to it.
"""

from __future__ import annotations

import base64
from typing import Any, Iterable, Sequence

import httpx


class UpstreamError(RuntimeError):
    """The engine was unreachable, or answered in a way we cannot use."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UpstreamClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- plumbing ------------------------------------------------------------

    async def _get(self, path: str) -> Any:
        try:
            response = await self._client.get(path)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"GET {path} failed: {exc}") from exc
        return self._decode(response, path)

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"POST {path} failed: {exc}") from exc
        return self._decode(response, path)

    @staticmethod
    def _decode(response: httpx.Response, path: str) -> Any:
        if response.status_code >= 400:
            detail = response.text.strip()[:400] or response.reason_phrase
            raise UpstreamError(
                f"engine returned {response.status_code} for {path}: {detail}",
                status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(f"engine sent non-JSON for {path}") from exc

    # -- introspection -------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        return await self._get("/health")

    async def capabilities(self) -> dict[str, Any]:
        return await self._get("/capabilities")

    async def openapi(self) -> dict[str, Any]:
        return await self._get("/openapi.json")

    # -- work ----------------------------------------------------------------

    async def inspect(self, name: str, data: bytes) -> dict[str, Any]:
        return await self._post("/inspect", _envelope(name, data))

    async def clean(
        self, name: str, data: bytes, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = _envelope(name, data)
        if options:
            payload["options"] = options
        return await self._post("/clean", payload)

    async def inspect_batch(
        self, items: Sequence[tuple[str, bytes]], *, cap: int = 50
    ) -> list[dict[str, Any]]:
        return await self._batch("/inspect/batch", items, None, cap)

    async def clean_batch(
        self,
        items: Sequence[tuple[str, bytes]],
        options: dict[str, Any] | None = None,
        *,
        cap: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._batch("/clean/batch", items, options, cap)

    async def _batch(
        self,
        path: str,
        items: Sequence[tuple[str, bytes]],
        options: dict[str, Any] | None,
        cap: int,
    ) -> list[dict[str, Any]]:
        """Run a batch, chunked to the engine's per-request file cap."""
        results: list[dict[str, Any]] = []
        chunk_size = max(1, cap)
        for chunk in _chunks(items, chunk_size):
            files = []
            for name, data in chunk:
                entry = _envelope(name, data)
                if options:
                    entry["options"] = options
                files.append(entry)
            payload = await self._post(path, {"files": files})
            batch = payload.get("results")
            if not isinstance(batch, list) or len(batch) != len(chunk):
                raise UpstreamError(
                    f"{path} returned {len(batch) if isinstance(batch, list) else 'no'} "
                    f"results for {len(chunk)} files"
                )
            results.extend(batch)
        return results


def _envelope(name: str, data: bytes) -> dict[str, Any]:
    return {"file": base64.b64encode(data).decode("ascii"), "name": name}


def decode_cleaned(payload: dict[str, Any]) -> bytes | None:
    """Pull the cleaned bytes out of a /clean response, if present."""
    raw = payload.get("cleaned")
    if not isinstance(raw, str):
        return None
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc:
        raise UpstreamError("engine sent cleaned data that is not valid base64") from exc


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
