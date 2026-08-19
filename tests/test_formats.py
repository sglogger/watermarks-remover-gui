from __future__ import annotations

import pytest

from app import formats

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_extension_allowlist_covers_every_requested_format():
    for ext in (
        ".png", ".jpg", ".jpeg", ".webp", ".avif", ".heic", ".bmp", ".gif", ".tiff",
        ".svg", ".pdf", ".docx", ".xlsx", ".pptx", ".epub", ".odt", ".html", ".md",
    ):
        assert ext in formats.ALLOWED_EXTS


def test_unknown_extension_is_refused():
    with pytest.raises(formats.RejectedFormat) as excinfo:
        formats.classify("clip.mp4", b"\x00" * 32)
    assert excinfo.value.reason == "unsupported_extension"


def test_missing_extension_is_refused():
    with pytest.raises(formats.RejectedFormat) as excinfo:
        formats.classify("README", b"hello")
    assert excinfo.value.reason == "no_extension"


@pytest.mark.parametrize(
    "data",
    [
        b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 16,   # MP3 with an ID3 tag
        b"\xff\xfb\x90\x64" + b"\x00" * 16,                   # bare MPEG audio frame
        b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16,       # WAV
        b"OggS\x00\x02" + b"\x00" * 16,                       # Ogg
        b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16,           # MP4
        b"\x1a\x45\xdf\xa3" + b"\x00" * 16,                   # Matroska
    ],
)
def test_audio_video_is_refused_even_with_an_image_extension(data):
    with pytest.raises(formats.RejectedFormat) as excinfo:
        formats.classify("actually-a-movie.png", data)
    assert excinfo.value.reason == "audio_video"


def test_heic_and_avif_share_the_mp4_header_but_are_accepted():
    for brand, ext in ((b"heic", ".heic"), (b"avif", ".avif")):
        data = b"\x00\x00\x00\x20ftyp" + brand + b"\x00" * 16
        info = formats.classify(f"photo{ext}", data)
        assert info.kind == "image"


def test_classify_reports_kind_and_highlightability():
    assert formats.classify("notes.md", b"# hi").highlightable is True
    assert formats.classify("notes.md", b"# hi").kind == "container"
    assert formats.classify("shot.png", PNG).highlightable is False
    assert formats.classify("plain.txt", b"hi").kind == "text"
