"""Accepted-format gate.

Two jobs, both done before anything reaches the engine:

1. Allowlist by extension. Anything not listed is refused — this is a deny-by-
   default gate, so a format the engine grows later stays refused until we add
   it here deliberately.
2. Sniff the bytes for audio/video containers. Audio and video are explicitly
   out of scope for this GUI, and an extension alone is not proof: an `.mp3`
   renamed to `.png` must still be refused.

The extension → "kind" mapping mirrors how the engine routes formats, purely so
the UI can label a result before the engine answers. It is a display hint, never
a decision: the engine's own `kind` from the report always wins.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Bitmap formats the engine cleans (EXIF / XMP / C2PA and friends).
IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".avif",
    ".heic",
    ".heif",
    ".bmp",
    ".gif",
    ".tiff",
    ".tif",
}

#: Structured documents and markup. The engine calls these "container".
CONTAINER_EXTS = {
    ".svg",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".odt",
    ".epub",
    ".html",
    ".htm",
    ".md",
    ".markdown",
}

#: Plain text.
TEXT_EXTS = {".txt"}

ALLOWED_EXTS = IMAGE_EXTS | CONTAINER_EXTS | TEXT_EXTS

#: Extensions the textarea can be submitted as, mapped to the filename we send
#: upstream. The extension is what selects the engine's pipeline, so this is the
#: user-visible "treat my text as…" control.
TEXT_INPUT_FORMATS = {
    "text": "input.txt",
    "markdown": "input.md",
    "html": "input.html",
}

#: Extensions whose cleaned output stays human-readable text, so the diff-based
#: highlighter can mark every hit in place.
HIGHLIGHTABLE_EXTS = {".txt", ".md", ".markdown", ".html", ".htm", ".svg"}

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".epub": "application/epub+zip",
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}

#: ISO-BMFF brands that mean "still image", not video. Everything else behind an
#: `ftyp` box (isom, mp42, qt, M4A, M4V, …) is audio/video and gets refused.
_IMAGE_FTYP_BRANDS = {
    b"heic",
    b"heix",
    b"heim",
    b"heis",
    b"hevc",
    b"hevx",
    b"mif1",
    b"msf1",
    b"avif",
    b"avis",
}


class RejectedFormat(ValueError):
    """Raised when a file is refused before it reaches the engine."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class FormatInfo:
    ext: str
    kind: str  # "text" | "image" | "container"
    mime: str
    highlightable: bool


def extension_of(name: str) -> str:
    """Lowercased final extension of *name*, or "" when it has none."""
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in base[1:]:
        return ""
    return "." + base.rsplit(".", 1)[-1].lower()


def kind_for_ext(ext: str) -> str:
    if ext in IMAGE_EXTS:
        return "image"
    if ext in CONTAINER_EXTS:
        return "container"
    return "text"


def sniff_av(data: bytes) -> str | None:
    """Return an audio/video format name when *data* looks like one, else None.

    Deliberately conservative about ISO-BMFF: HEIC and AVIF share the `ftyp`
    header with MP4, so the brand decides. An unknown brand is treated as video,
    because refusing an odd still image is a smaller failure than handing a
    movie to an engine we told the user we would not use for movies.
    """
    if len(data) < 12:
        return None

    if data[:3] == b"ID3":
        return "MP3"
    # MPEG audio frame sync: 11 set bits, with a valid layer and bitrate nibble.
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0 and (data[1] & 0x18) != 0x08:
        return "MP3"
    if data[:4] == b"RIFF" and data[8:12] in (b"WAVE", b"AVI "):
        return "WAV" if data[8:12] == b"WAVE" else "AVI"
    if data[:4] == b"OggS":
        return "OGG"
    if data[:4] == b"fLaC":
        return "FLAC"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "Matroska/WebM"
    if data[:4] == b"\x30\x26\xb2\x75":
        return "ASF/WMV"
    if data[:4] == b"FORM" and data[8:12] == b"AIFF":
        return "AIFF"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand not in _IMAGE_FTYP_BRANDS:
            label = brand.decode("ascii", "replace").strip()
            return f"MP4/QuickTime ({label})" if label else "MP4/QuickTime"
    return None


def classify(name: str, data: bytes) -> FormatInfo:
    """Validate *name*/*data* and describe the format, or raise RejectedFormat."""
    ext = extension_of(name)
    if not ext:
        raise RejectedFormat(
            "no_extension",
            f"{name!r} has no file extension, so the format cannot be determined.",
        )
    if ext not in ALLOWED_EXTS:
        raise RejectedFormat(
            "unsupported_extension",
            f"{ext} files are not supported. Supported: "
            + ", ".join(sorted(e.lstrip('.') for e in ALLOWED_EXTS))
            + ".",
        )
    av = sniff_av(data)
    if av is not None:
        raise RejectedFormat(
            "audio_video",
            f"{name!r} contains {av} data. Audio and video are out of scope for this tool.",
        )
    return FormatInfo(
        ext=ext,
        kind=kind_for_ext(ext),
        mime=_MIME_BY_EXT.get(ext, "application/octet-stream"),
        highlightable=ext in HIGHLIGHTABLE_EXTS,
    )


def accept_attribute() -> str:
    """Value for the file input's `accept=` attribute."""
    return ",".join(sorted(ALLOWED_EXTS))
