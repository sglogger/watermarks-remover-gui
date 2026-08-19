"""A stand-in for the upstream engine, wired in through httpx's MockTransport.

It implements just enough of the real contract to exercise our code: the same
routes, the same JSON/base64 envelope, and a text cleaner that strips zero-width
spaces and turns non-breaking spaces into ordinary ones — the two behaviours the
diff-based highlighter has to reproduce positions for.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

ZWSP = "​"
NBSP = " "

CLEAN_OPTIONS = [
    "nfkc",
    "aggressive_homoglyphs",
    "keep_non_ai_metadata",
    "also_layer_a_text",
    "remove_pixel",
    "strip_all_metadata",
    "detect_before",
    "detect_after",
]

VERSION = "v0.5.0"


def openapi_spec(options: list[str] | None = None) -> dict[str, Any]:
    names = CLEAN_OPTIONS if options is None else options
    return {
        "openapi": "3.0.3",
        "info": {"title": "watermarks-remover", "version": VERSION},
        "paths": {
            "/health": {"get": {}},
            "/capabilities": {"get": {}},
            "/inspect": {"post": {}},
            "/detect": {"post": {}},
            "/clean": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "file": {"type": "string"},
                                        "name": {"type": "string"},
                                        "options": {
                                            "type": "object",
                                            "properties": {
                                                name: {"type": "boolean"} for name in names
                                            },
                                        },
                                    },
                                }
                            }
                        }
                    }
                }
            },
            "/inspect/batch": {"post": {}},
            "/clean/batch": {"post": {}},
        },
    }


def _kind(name: str) -> str:
    if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if name.endswith((".md", ".html", ".svg", ".pdf", ".docx")):
        return "container"
    return "text"


def _inspect(name: str, data: bytes) -> dict[str, Any]:
    kind = _kind(name)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "ok": True,
            "kind": kind,
            "suspicious": False,
            "report": {"format": "binary", "metadata": {}},
        }

    hits = []
    zwsp = text.count(ZWSP)
    nbsp = text.count(NBSP)
    if zwsp:
        hits.append(
            {
                "codepoint": "U+200B",
                "char": ZWSP,
                "label": "ZERO WIDTH SPACE",
                "count": zwsp,
                "kind": "strip",
                "sample_offsets": [i for i, c in enumerate(text) if c == ZWSP][:10],
            }
        )
    if nbsp:
        hits.append(
            {
                "codepoint": "U+00A0",
                "char": NBSP,
                "label": "NO-BREAK SPACE",
                "count": nbsp,
                "kind": "space",
                "sample_offsets": [i for i, c in enumerate(text) if c == NBSP][:10],
            }
        )
    return {
        "ok": True,
        "kind": kind,
        "suspicious": bool(hits),
        "report": {"length": len(text), "suspicious_total": zwsp + nbsp, "hits": hits},
    }


def _clean(name: str, data: bytes, options: dict[str, Any]) -> dict[str, Any]:
    unknown = [k for k in options if k not in CLEAN_OPTIONS]
    if unknown:
        # The real engine refuses the whole request on an unknown option.
        return {"ok": False, "error": f"unknown option: {unknown[0]}"}
    kind = _kind(name)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "ok": True,
            "kind": kind,
            "cleaned": base64.b64encode(data).decode(),
            "report": {"actions": ["stripped metadata"]},
        }
    cleaned = text.replace(ZWSP, "").replace(NBSP, " ")
    return {
        "ok": True,
        "kind": kind,
        "cleaned": base64.b64encode(cleaned.encode("utf-8")).decode(),
        "report": {"actions": ["stripped invisible characters"], "removed": len(text) - len(cleaned)},
    }


def make_transport(
    *, options: list[str] | None = None, fail: set[str] | None = None
) -> httpx.MockTransport:
    """Build a transport. *fail* names routes that should return 503."""
    failing = fail or set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in failing:
            return httpx.Response(503, text="engine down")

        if request.method == "GET":
            if path == "/health":
                return httpx.Response(200, json={"ok": True, "version": VERSION})
            if path == "/capabilities":
                return httpx.Response(
                    200,
                    json={
                        "version": VERSION,
                        "tools": {"exiftool": True, "qpdf": True, "c2patool": False},
                        "pixel_backends": {"ctrlregen": False, "diffusion": False},
                    },
                )
            if path == "/openapi.json":
                return httpx.Response(200, json=openapi_spec(options))
            return httpx.Response(404, text="not found")

        payload = json.loads(request.content or b"{}")
        if path in ("/inspect", "/clean"):
            data = base64.b64decode(payload["file"])
            name = payload.get("name", "input.txt")
            if path == "/inspect":
                return httpx.Response(200, json=_inspect(name, data))
            return httpx.Response(200, json=_clean(name, data, payload.get("options", {})))

        if path in ("/inspect/batch", "/clean/batch"):
            results = []
            for entry in payload.get("files", []):
                data = base64.b64decode(entry["file"])
                name = entry.get("name", "input.txt")
                result = (
                    _inspect(name, data)
                    if path == "/inspect/batch"
                    else _clean(name, data, entry.get("options", {}))
                )
                results.append({"name": name, **result})
            return httpx.Response(200, json={"ok": True, "results": results})

        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)
