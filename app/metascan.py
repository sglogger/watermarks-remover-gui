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
own container, so switching it on is a deliberate trade. The bytes are handed
over in RAM — an anonymous ``memfd`` where the kernel has one, a pipe on stdin
otherwise — so nothing is written to disk and the scan cache keeps its
memory-only promise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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

#: What a flagged tag is evidence *of*. The distinction is the whole point:
#: this is a watermark remover, and a Word document that names its author is
#: not machine-generated. Both kinds are worth showing and both can be
#: stripped, but only ``AI`` is a finding in the sense the verdict means.
AI = "ai"
PRIVACY = "privacy"

#: Sentinel kind: the tag names a program, and only its *value* says whether
#: that program is an AI generator. `PDF:Producer` is `Microsoft Word` about as
#: often as it is anything else.
_BY_TOOL = "tool"

#: Names that make an authoring-tool value an AI generator. A list of product
#: names will always be behind the market, so the failure mode is chosen
#: deliberately: a miss downgrades the label from "AI tool" to "authoring
#: tool", and the tag is still shown, still flagged, still removable. Nothing
#: disappears because a name is missing here — it is only described less
#: sharply.
_AI_TOOL_NAMES = re.compile(
    r"stable ?diffusion|midjourney|dall[·\-– ]?e|openai|chatgpt|(^|\W)gpt[\-\d]|claude|anthropic"
    r"|gemini|imagen|firefly|copilot|sora|flux\.?1|ideogram|leonardo\.ai|runway|novelai"
    r"|comfyui|automatic1111|invokeai|craiyon|nightcafe|playground ?ai|recraft|grok|diffusion"
    r"|llama|mistral|deepseek|qwen|kling|veo ?[23]|luma ?(ai|labs)|pika|synthid"
    r"|generative ?(ai|fill|expand)|text[- ]to[- ]image|diffusion model",
    re.IGNORECASE,
)

#: IPTC's DigitalSourceType is the one tag that states outright how a file came
#: to be — and it says "digitalCapture" for a photograph just as plainly as it
#: says "trainedAlgorithmicMedia" for a generated image. Only the latter is a
#: finding; reading the tag name alone would flag every camera JPEG that
#: bothers to fill it in.
_AI_SOURCE_TYPE = re.compile(r"algorithmic", re.IGNORECASE)

#: Tag → why it matters, first match wins. The point of the split is that only
#: these tags are treated as a finding: a photo's exposure time is metadata but
#: identifies nobody, while its GPS position and camera serial do.
_FLAG_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"c2pa|jumbf|contentcredential|provenance"),
        "content-credentials provenance",
        AI,
    ),
    (
        # `(^|:)parameters$` is where Automatic1111 and ComfyUI write the whole
        # prompt, seed and sampler blob of a locally generated image — the most
        # common AI marker there is on a PNG, and it is not called "prompt".
        re.compile(
            r"prompt|(^|:)seed|checkpoint|workflow|stablediffusion|generationdata"
            r"|(^|:)parameters$"
        ),
        "generation parameters",
        AI,
    ),
    (re.compile(r"(^|:)gps"), "location", PRIVACY),
    (
        # `(^|:)application$` and not a bare `application`, so that IPTC's
        # ApplicationRecordVersion — a format version number, not a program —
        # stays out of it. XML:Application is what OOXML calls the writing app.
        re.compile(
            r"producer|software|creatortool|generator|xmptoolkit|encoder|hostcomputer"
            r"|(^|:)application$"
        ),
        "authoring tool",
        _BY_TOOL,
    ),
    (
        # LastModifiedBy is the name of whoever last saved the document, and
        # OOXML puts it in every file Word writes.
        re.compile(
            r"author|creator|artist|by-?line|owner|credit|contact|writer|editor"
            r"|lastmodifiedby|lastsavedby|lastauthor"
        ),
        "identity",
        PRIVACY,
    ),
    (re.compile(r"(^|:)company$|(^|:)manager$|organi[sz]ation"), "organisation", PRIVACY),
    (re.compile(r"copyright|rights|licen[cs]e"), "rights statement", PRIVACY),
    (re.compile(r"email|website|(^|:)url|phone|telephone|(^|:)address"), "contact detail", PRIVACY),
    (
        re.compile(r"documentid|instanceid|derivedfrom|documentancestors|(^|:)history|versionid"),
        "document identity chain",
        PRIVACY,
    ),
    (re.compile(r"serialnumber|bodyserial|internalserial|lensserial"), "device serial", PRIVACY),
    (re.compile(r"(^|:)make$|(^|:)model$|cameramodel|lensmodel|(^|:)software$"), "device", PRIVACY),
    (
        re.compile(
            r"comment|keywords|subject|description|(^|:)title|caption|headline|instructions"
        ),
        "free-text field",
        PRIVACY,
    ),
)

#: Enough to show what is in a file without letting one pathological upload
#: push a megabyte of tag values through the JSON response.
MAX_TAGS = 400
MAX_VALUE_CHARS = 400


def classify_tag(tag: str, value: str = "") -> tuple[str, str] | None:
    """Return ``(reason, kind)`` for *tag*, or None if it gives nothing away.

    *value* matters for the two rules that cannot be decided from the tag name:
    what program wrote the file, and what IPTC says the file's source was.
    """
    lowered = tag.lower()
    if "digitalsourcetype" in lowered:
        return (
            ("AI-generation marker", AI)
            if _AI_SOURCE_TYPE.search(value)
            else ("digital source type", PRIVACY)
        )
    for pattern, reason, kind in _FLAG_RULES:
        if not pattern.search(lowered):
            continue
        if kind is _BY_TOOL:
            return (
                ("AI tool named in the metadata", AI)
                if _AI_TOOL_NAMES.search(value)
                else (reason, PRIVACY)
            )
        return reason, kind
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
        verdict = classify_tag(tag, value)
        if verdict:
            reason, kind = verdict
            flagged.append({"tag": tag, "value": value, "reason": reason, "kind": kind})
        else:
            other.append({"tag": tag, "value": value})

    return {"flagged": flagged, "other": other, "truncated": truncated}


def _memfd(data: bytes) -> int | None:
    """A seekable, anonymous, RAM-only copy of *data* — or None where there is none.

    exiftool reads a pipe strictly forwards, and every ZIP-based format keeps
    its index at the very end of the file: piped in on stdin, a DOCX, XLSX,
    PPTX, ODT or EPUB comes back as a bare ``FileType: ZIP`` with none of its
    Office metadata, while the same bytes in a real file yield the XMP block
    naming the document's author. That is not a hypothetical — it is why this
    check reported "no identifying metadata" for files whose engine report
    listed ``XMP-dc:Creator``.

    A memfd is a file as far as seeking is concerned and a memory buffer in
    every other respect, so the format is read in full without the bytes
    reaching a filesystem. Linux only; elsewhere the caller falls back to
    stdin and ZIP-based formats stay shallow.
    """
    create = getattr(os, "memfd_create", None)
    if create is None or not os.path.isdir("/proc/self/fd"):
        return None
    try:
        fd = create("wr-metascan")
    except OSError:
        return None
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        os.close(fd)
        return None
    return fd


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

        args = ["-json", "-G", "-a", "-api", "largefilesupport=1"]
        # The child opens the path itself, so /proc/self/fd is resolved in its
        # own process — the inherited descriptor, not ours.
        fd = _memfd(data)
        try:
            if fd is None:
                code, stdout, stderr = await self._run([*args, "-"], data)
            else:
                code, stdout, stderr = await self._run(
                    [*args, f"/proc/self/fd/{fd}"], b"", pass_fd=fd
                )
        except asyncio.TimeoutError:
            result["error"] = f"timed out after {self.timeout:g}s"
            return result
        except OSError as exc:
            result["error"] = f"could not run {self.command!r}: {exc}"
            return result
        finally:
            if fd is not None:
                os.close(fd)

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

    async def _run(
        self, args: list[str], stdin: bytes, pass_fd: int | None = None
    ) -> tuple[int, str, str]:
        extra: dict[str, Any] = {"pass_fds": (pass_fd,)} if pass_fd is not None else {}
        proc = await asyncio.create_subprocess_exec(
            self.command,
            *args,
            stdin=asyncio.subprocess.DEVNULL if pass_fd is not None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **extra,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(None if pass_fd is not None else stdin), timeout=self.timeout
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


def flagged_tags(result: Any, kinds: set[str] | None = None) -> list[str]:
    """Compact "tag (reason)" lines from a scan result, for compact display.

    *kinds* restricts the output to AI markers, privacy tags, or both. The
    caller after a removal passes only what the chosen options were meant to
    strip: with `keep_non_ai_metadata` on — the default — an author name that
    survives is the engine doing as it was told, not a failed removal.
    """
    if not isinstance(result, dict):
        return []
    out = []
    for entry in result.get("flagged") or []:
        if not isinstance(entry, dict) or not entry.get("tag"):
            continue
        if kinds is not None and entry.get("kind", PRIVACY) not in kinds:
            continue
        out.append(f"{entry['tag']} ({entry.get('reason', 'metadata')})")
    return out


def has_ai_marker(result: Any) -> bool:
    """Whether the scan found a tag that says the file was machine-made."""
    if not isinstance(result, dict):
        return False
    return any(
        isinstance(entry, dict) and entry.get("kind") == AI
        for entry in result.get("flagged") or []
    )
