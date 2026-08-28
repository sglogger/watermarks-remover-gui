"""A second opinion on file metadata, from a locally run exiftool.

Why this exists
---------------
The engine already ships exiftool — `/capabilities` reports ``exiftool: true``
and a PDF report carries a ``tools.exiftool`` block proving it ran. What the
engine does not do is hand the output back: it keeps a short list of
"interesting lines" and drops the rest. Measured against a PDF whose Info
dictionary held ``/Producer``, ``/Author`` and ``/Keywords``, the engine
returned ``findings: []`` and ``suspicious: false`` while exiftool named all
three. The engine says as much itself, in the note it attaches to every PDF:
"PDF inspection is best-effort; exiftool/c2patool give more reliable metadata
detection".

So this is a second opinion, never a replacement. The engine's report is still
passed through untouched; what exiftool finds is reported beside it under its
own key, attributed to this app rather than to the engine.

Off by default (``GUI_EXIFTOOL=1`` turns it on). It runs a third-party binary
over untrusted uploads, which is precisely the work the engine isolates in its
own container, so switching it on is a deliberate trade. Bytes are piped in on
stdin — nothing is written to disk, which keeps the memory-only promise the
scan cache makes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from typing import Any, Sequence

from . import formats

log = logging.getLogger("wr-gui.metascan")

#: Formats where a container can hide metadata the diff-based highlighter can
#: never see. Text and markup are left out: their watermarks are characters in
#: the body, and `/clean` plus the diff already accounts for every one.
SCAN_EXTS = formats.IMAGE_EXTS | {".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".epub"}

#: Tags that describe the pipe we fed exiftool, not the file that came down it.
_PIPE_TAGS = {
    "SourceFile",
    "File:FileName",
    "File:Directory",
    "File:FileSize",
    "File:FileModifyDate",
    "File:FileAccessDate",
    "File:FileInodeChangeDate",
    "File:FilePermissions",
    "ExifTool:ExifToolVersion",
}

#: Tag → why it matters, first match wins. The point of the split is that only
#: these tags are treated as a finding: a photo's exposure time is metadata but
#: identifies nobody, while its GPS position and camera serial do.
_FLAG_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"digitalsourcetype"), "AI-generation marker"),
    (re.compile(r"c2pa|jumbf|contentcredential|provenance"), "content-credentials provenance"),
    (re.compile(r"(^|:)gps"), "location"),
    (
        re.compile(r"producer|software|creatortool|generator|xmptoolkit|encoder|hostcomputer"),
        "authoring tool",
    ),
    (re.compile(r"author|creator|artist|by-?line|owner|credit|contact|writer|editor"), "identity"),
    (re.compile(r"copyright|rights|licen[cs]e"), "rights statement"),
    (re.compile(r"email|website|(^|:)url|phone|telephone|(^|:)address"), "contact detail"),
    (
        re.compile(r"documentid|instanceid|derivedfrom|documentancestors|(^|:)history|versionid"),
        "document identity chain",
    ),
    (re.compile(r"serialnumber|bodyserial|internalserial|lensserial"), "device serial"),
    (re.compile(r"(^|:)make$|(^|:)model$|cameramodel|lensmodel|(^|:)software$"), "device"),
    (
        re.compile(r"prompt|(^|:)seed|checkpoint|workflow|stablediffusion|generationdata"),
        "generation parameters",
    ),
    (
        re.compile(
            r"comment|keywords|subject|description|(^|:)title|caption|headline|instructions"
        ),
        "free-text field",
    ),
)

#: Enough to show what is in a file without letting one pathological upload
#: push a megabyte of tag values through the JSON response.
MAX_TAGS = 400
MAX_VALUE_CHARS = 400


def classify_tag(tag: str) -> str | None:
    """Return why *tag* identifies someone or something, or None if it doesn't."""
    lowered = tag.lower()
    for pattern, reason in _FLAG_RULES:
        if pattern.search(lowered):
            return reason
    return None


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif isinstance(value, bool):
        text = "yes" if value else "no"
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > MAX_VALUE_CHARS:
        text = text[: MAX_VALUE_CHARS - 1] + "…"
    return text


def summarize(tags: dict[str, Any]) -> dict[str, Any]:
    """Split one exiftool object into flagged findings and the rest."""
    flagged: list[dict[str, str]] = []
    other: list[dict[str, str]] = []
    truncated = False

    for tag, raw in tags.items():
        if tag in _PIPE_TAGS:
            continue
        value = _stringify(raw)
        if not value:
            continue
        if len(flagged) + len(other) >= MAX_TAGS:
            truncated = True
            break
        reason = classify_tag(tag)
        if reason:
            flagged.append({"tag": tag, "value": value, "reason": reason})
        else:
            other.append({"tag": tag, "value": value})

    return {"flagged": flagged, "other": other, "truncated": truncated}


@dataclass
class MetaScanner:
    """Runs exiftool over bytes, or reports plainly why it cannot."""

    enabled: bool = False
    command: str = "exiftool"
    timeout: float = 20.0
    max_bytes: int = 32 * 1024 * 1024
    concurrency: int = 4

    available: bool = False
    version: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(max(1, self.concurrency))

    # -- lifecycle -----------------------------------------------------------

    async def probe(self) -> None:
        """Find out once, at startup, whether the binary is actually there."""
        if not self.enabled:
            self.error = None
            return
        if shutil.which(self.command) is None:
            self.error = f"{self.command!r} is not on PATH"
            return
        try:
            code, stdout, stderr = await self._run(["-ver"], b"")
        except OSError as exc:
            self.error = f"could not start {self.command!r}: {exc}"
            return
        if code != 0:
            self.error = (stderr or f"{self.command} -ver exited {code}").strip()[:200]
            return
        self.available = True
        self.version = stdout.strip() or None
        self.error = None

    def wanted(self, ext: str) -> bool:
        return self.available and ext in SCAN_EXTS

    # -- work ----------------------------------------------------------------

    async def scan(self, name: str, data: bytes) -> dict[str, Any]:
        """Read *data*'s metadata. Always returns a result, never raises."""
        result: dict[str, Any] = {
            "tool": "exiftool",
            "version": self.version,
            "ok": False,
            "flagged": [],
            "other": [],
            "truncated": False,
            "error": None,
        }
        if len(data) > self.max_bytes:
            result["error"] = (
                f"skipped: {len(data)} bytes is over the "
                f"{self.max_bytes} byte metadata-check limit"
            )
            return result

        try:
            code, stdout, stderr = await self._run(
                ["-json", "-G", "-a", "-api", "largefilesupport=1", "-"], data
            )
        except asyncio.TimeoutError:
            result["error"] = f"timed out after {self.timeout:g}s"
            return result
        except OSError as exc:
            result["error"] = f"could not run {self.command!r}: {exc}"
            return result

        # exiftool exits non-zero on a file it cannot parse, but still prints
        # whatever it did read, so the output is worth looking at either way.
        try:
            parsed = json.loads(stdout) if stdout.strip() else []
        except ValueError:
            result["error"] = (stderr.strip() or "exiftool sent output that is not JSON")[:200]
            return result

        if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], dict):
            result["error"] = (stderr.strip() or "exiftool reported nothing for this file")[:200]
            return result

        result.update(summarize(parsed[0]))
        result["ok"] = True
        if code != 0 and stderr.strip():
            # Partial read: say so, but keep what was recovered.
            result["error"] = stderr.strip()[:200]
        log.debug("%s: %d flagged tag(s)", name, len(result["flagged"]))
        return result

    async def scan_many(
        self, items: Sequence[tuple[str, bytes]]
    ) -> list[dict[str, Any]]:
        async def one(name: str, data: bytes) -> dict[str, Any]:
            async with self._semaphore:
                return await self.scan(name, data)

        return list(await asyncio.gather(*(one(name, data) for name, data in items)))

    # -- plumbing ------------------------------------------------------------

    async def _run(self, args: list[str], stdin: bytes) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            self.command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    # -- introspection -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "tool": "exiftool",
            "version": self.version,
            "error": self.error,
            "extensions": sorted(SCAN_EXTS),
        }


def flagged_tags(result: Any) -> list[str]:
    """Compact "tag (reason)" lines from a scan result, for compact display."""
    if not isinstance(result, dict):
        return []
    out = []
    for entry in result.get("flagged") or []:
        if isinstance(entry, dict) and entry.get("tag"):
            out.append(f"{entry['tag']} ({entry.get('reason', 'metadata')})")
    return out
