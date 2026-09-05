"""A stand-in for the upstream engine, wired in through httpx's MockTransport.

It implements just enough of the real contract to exercise our code: the same
routes, the same JSON/base64 envelope, and a text cleaner that strips zero-width
spaces and turns non-breaking spaces into ordinary ones — the two behaviours the
diff-based highlighter has to reproduce positions for.

Modelled on engine v0.7.0, including the three things about it that the GUI had
to be changed for:

* `suspicious` is an evidence object, not a boolean;
* cleaning anything the engine calls `text` requires a Layer B rewrite
  strategy, and is refused outright when none is configured;
* `/clean` answers 400 for a refusal, while `/clean/batch` reports the same
  refusal per file inside a 200.
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
    "normalize_spaces",
    "keep_non_ai_metadata",
    "also_layer_a_text",
    "remove_pixel",
    "remove_audio_watermark",
    "strip_all_metadata",
    "detect_before",
    "detect_after",
    "deep_images",
    "style",
    "strategy",
]

#: Options the engine types as a string enum rather than a boolean, and the
#: values each accepts. Since v0.6.0 a value outside the enum fails the whole
#: request instead of falling back to the engine's own default.
STRING_OPTIONS = {
    "remove_pixel": {"ctrlregen", "diffusion"},
    "deep_images": {"auto", "always", "lossless", "never"},
}

#: Free-text string options — validated by grammar (strategy) or not at all.
TEXT_OPTIONS = {"style", "strategy"}

#: Layer B tactics, mirroring `rewrite_text.KNOWN_TACTICS` in v0.7.0.
KNOWN_TACTICS = {"paraphrase", "backtranslate", "structural", "humanize", "code", "chunk", "mlm"}

VERSION = "v0.7.0"

#: What the engine falls back to when a request names no strategy. `None`
#: models the published Docker image, which ships no `config/clean_strategy.json`
#: and so refuses every text clean.
DEFAULT_STRATEGY = "paraphrase@0.8,mlm@0.2"


class Refused(Exception):
    """A request the engine rejects — 400 alone, per-file inside a batch."""


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
                                                name: {
                                                    "type": "string"
                                                    if name in STRING_OPTIONS
                                                    or name in TEXT_OPTIONS
                                                    else "boolean"
                                                }
                                                for name in names
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
            "/detect/batch": {"post": {}},
        },
    }


def _kind(name: str) -> str:
    if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if name.endswith((".md", ".html", ".svg", ".pdf", ".docx")):
        return "container"
    return "text"


def _suspicious(report: dict[str, Any], kind: str) -> dict[str, Any]:
    """The v0.7.0 evidence object: one entry per class, plus a rolled-up verdict."""
    carriers = int(report.get("suspicious_total") or 0)
    has_provenance = bool(report.get("has_c2pa") or report.get("has_ai_metadata"))
    classes = {
        "provenance": {
            "present": has_provenance,
            "strength": "definitive",
            "description": "Observable provenance metadata embedded in the file.",
            "signals": {
                "has_c2pa": bool(report.get("has_c2pa")),
                "has_ai_metadata": bool(report.get("has_ai_metadata")),
            },
        },
        "layer_a_unicode": {
            "present": carriers > 0,
            "strength": "deterministic",
            "description": "Invisible/format Unicode carriers detected in the text body.",
            "signals": {"suspicious_total": carriers},
        },
        "watermark_detector": {
            "present": False,
            "strength": "scheme_specific",
            "description": "A positive result from a configured detector.",
            "signals": {"detected_any": False, "detectors": []},
        },
        "stylometry": {
            "present": False,
            "strength": "heuristic",
            "description": "Stylometric AI-density score reached the threshold.",
            "signals": {"score": None, "density_tier": None},
        },
    }
    return {
        "verdict": any(c["present"] for c in classes.values()),
        "description": "Combined across heterogeneous evidence classes.",
        "classes": classes,
    }


def _inspect(name: str, data: bytes, detect: bool = False) -> dict[str, Any]:
    kind = _kind(name)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        report = {"format": "binary", "metadata": {}}
        return {
            "ok": True,
            "kind": kind,
            "suspicious": _suspicious(report, kind),
            "report": report,
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
    # A container report keeps its per-character findings under `layer_a_hits`
    # (engine v0.6.0); only a text report calls them `hits`.
    hits_key = "layer_a_hits" if kind == "container" else "hits"
    report = {"length": len(text), "suspicious_total": zwsp + nbsp, hits_key: hits}
    if detect:
        report["text_detectors"] = [{"detector": "stylometry", "available": True, "score": 0.1}]
    return {
        "ok": True,
        "kind": kind,
        "suspicious": _suspicious(report, kind),
        "report": report,
    }


def _parse_strategy(spec: str) -> list[tuple[str, float]]:
    if not spec or not spec.strip():
        raise Refused("strategy must be a non-empty list of tactic@intensity steps")
    steps = []
    for raw in spec.split(","):
        item = raw.strip()
        if "@" not in item:
            raise Refused(f"bad strategy step {item!r}; expected tactic@intensity")
        tactic, _, level = item.rpartition("@")
        if tactic.strip() not in KNOWN_TACTICS:
            raise Refused(f"unknown strategy tactic {tactic.strip()!r}")
        try:
            value = float(level)
        except ValueError:
            raise Refused(f"bad intensity {level!r}") from None
        if not 0 < value <= 1:
            raise Refused(f"intensity {value} out of range")
        steps.append((tactic.strip(), value))
    return steps


def _check_options(options: dict[str, Any]) -> None:
    unknown = [k for k in options if k not in CLEAN_OPTIONS]
    if unknown:
        # The real engine refuses the whole request on an unknown option.
        raise Refused(f"unknown option: {unknown[0]}")
    for key, allowed in STRING_OPTIONS.items():
        value = options.get(key)
        if value is not None and value not in allowed:
            # Since v0.6.0 an out-of-enum value is an error, not a fallback.
            raise Refused(f"option {key!r} must be one of {sorted(allowed)}")
    # v0.7.0 validates the strategy up front, so a typo is a 400 rather than a
    # failure halfway through a clean. An empty string fails this check.
    if "strategy" in options:
        _parse_strategy(options["strategy"])


def _clean(
    name: str,
    data: bytes,
    options: dict[str, Any],
    default_strategy: str | None = DEFAULT_STRATEGY,
) -> dict[str, Any]:
    _check_options(options)
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

    report: dict[str, Any] = {"actions": ["stripped invisible characters"]}
    if kind == "text":
        # v0.7.0: Layer B is not optional for text. Without a strategy — the
        # state the published image ships in — the whole request is refused.
        strategy = options.get("strategy") or default_strategy
        if not strategy:
            raise Refused(
                "Layer B rewrite is required for text cleaning; configure a "
                "default strategy (config/clean_strategy.json) or pass options.strategy"
            )
        report["layer_b"] = {"strategy": strategy, "steps": len(_parse_strategy(strategy))}

    cleaned = text.replace(ZWSP, "")
    if options.get("normalize_spaces", True):
        cleaned = cleaned.replace(NBSP, " ")
    report["removed"] = len(text) - len(cleaned)
    return {
        "ok": True,
        "kind": kind,
        "cleaned": base64.b64encode(cleaned.encode("utf-8")).decode(),
        "report": report,
    }


def make_transport(
    *,
    options: list[str] | None = None,
    fail: set[str] | None = None,
    default_strategy: str | None = DEFAULT_STRATEGY,
) -> httpx.MockTransport:
    """Build a transport. *fail* names routes that should return 503.

    *default_strategy* mirrors the engine's `config/clean_strategy.json`; pass
    None to model the published image, which has none and so refuses to clean
    plain text at all.
    """
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
                        "tools": {
                            "exiftool": True,
                            "qpdf": True,
                            "c2patool": False,
                            "ghostscript": False,
                            "ffmpeg": False,
                        },
                        "pixel_backends": {"ctrlregen": False, "diffusion": False},
                        "scorers": {"synthid": False, "stylometry": True},
                        "text_detectors": {"markllm": False, "gumbel": False},
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
                return httpx.Response(
                    200, json=_inspect(name, data, bool(payload.get("detect")))
                )
            try:
                result = _clean(name, data, payload.get("options", {}), default_strategy)
            except Refused as exc:
                # A single-file refusal is an HTTP 400 carrying the reason.
                return httpx.Response(400, json={"ok": False, "error": str(exc)})
            return httpx.Response(200, json=result)

        if path in ("/inspect/batch", "/clean/batch"):
            results = []
            for entry in payload.get("files", []):
                data = base64.b64decode(entry["file"])
                name = entry.get("name", "input.txt")
                if path == "/inspect/batch":
                    result = _inspect(name, data, bool(entry.get("detect")))
                else:
                    try:
                        result = _clean(
                            name, data, entry.get("options", {}), default_strategy
                        )
                    except Refused as exc:
                        # A batch keeps going: the refusal is reported per file.
                        result = {"ok": False, "error": str(exc)}
                results.append({"name": name, **result})
            return httpx.Response(200, json={"ok": True, "results": results})

        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)
