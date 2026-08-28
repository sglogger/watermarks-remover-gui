"""The local exiftool pass.

The subprocess tests are skipped when exiftool is not installed; the pure
classification and summarising logic always runs.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from app import metascan
from app.config import Settings
from tests.conftest import build_client

HAS_EXIFTOOL = shutil.which("exiftool") is not None
needs_exiftool = pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool is not installed")


def make_pdf() -> bytes:
    """A minimal but valid PDF whose Info dictionary identifies its author."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
    ]
    stream = b"BT /F1 12 Tf 20 100 Td (Hello) Tj ET"
    objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Producer (SecretTool 9000) /Author (Jo) /Keywords (mark-1234) >>")

    out = b"%PDF-1.4\n"
    offsets = []
    for index, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % index + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )
    return out


# -- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    [
        "PDF:Author",
        "PDF:Producer",
        "XMP:CreatorTool",
        "EXIF:GPSLatitude",
        "IPTC:DigitalSourceType",
        "XMP:DocumentID",
        "MakerNotes:SerialNumber",
        "EXIF:Model",
        "PDF:Keywords",
    ],
)
def test_identifying_tags_are_flagged(tag):
    assert metascan.classify_tag(tag) is not None


@pytest.mark.parametrize(
    "tag",
    ["EXIF:ExposureTime", "PDF:PageCount", "File:MIMEType", "PNG:BitDepth", "EXIF:ImageWidth"],
)
def test_harmless_tags_are_not_flagged(tag):
    assert metascan.classify_tag(tag) is None


def test_a_tool_name_is_not_reported_as_a_person():
    """CreatorTool is a program. It matches /creator/ too, so order matters."""
    assert metascan.classify_tag("XMP:CreatorTool") == "authoring tool"
    assert metascan.classify_tag("XMP:Creator") == "identity"


def test_summarize_splits_and_drops_pipe_noise():
    result = metascan.summarize(
        {
            "SourceFile": "-",
            "File:FilePermissions": "prw-rw----",
            "ExifTool:ExifToolVersion": 13.25,
            "PDF:Author": "Jo",
            "PDF:PageCount": 1,
            "PDF:Title": "",
        }
    )
    assert [entry["tag"] for entry in result["flagged"]] == ["PDF:Author"]
    assert [entry["tag"] for entry in result["other"]] == ["PDF:PageCount"]
    assert result["truncated"] is False


def test_summarize_truncates_a_pathological_tag_list():
    tags = {f"XMP:Junk{i}": "x" for i in range(metascan.MAX_TAGS + 50)}
    result = metascan.summarize(tags)
    assert result["truncated"] is True
    assert len(result["flagged"]) + len(result["other"]) == metascan.MAX_TAGS


def test_long_values_are_clipped():
    result = metascan.summarize({"PDF:Author": "a" * 5000})
    assert len(result["flagged"][0]["value"]) == metascan.MAX_VALUE_CHARS


def test_only_container_formats_are_scanned():
    scanner = metascan.MetaScanner(enabled=True)
    scanner.available = True
    assert scanner.wanted(".pdf") is True
    assert scanner.wanted(".png") is True
    assert scanner.wanted(".docx") is True
    # Text and markup are covered end to end by the diff, so exiftool adds
    # nothing there and is not run.
    assert scanner.wanted(".md") is False
    assert scanner.wanted(".txt") is False
    assert scanner.wanted(".html") is False


def test_a_disabled_scanner_never_claims_to_be_available():
    scanner = metascan.MetaScanner(enabled=False)
    asyncio.run(scanner.probe())
    assert scanner.available is False
    assert scanner.wanted(".pdf") is False
    assert scanner.error is None


def test_a_missing_binary_is_reported_not_raised():
    scanner = metascan.MetaScanner(enabled=True, command="exiftool-that-does-not-exist")
    asyncio.run(scanner.probe())
    assert scanner.available is False
    assert "not on PATH" in (scanner.error or "")


# -- the real binary --------------------------------------------------------


@needs_exiftool
def test_exiftool_finds_the_pdf_metadata_the_engine_reports_as_nothing():
    scanner = metascan.MetaScanner(enabled=True)
    asyncio.run(scanner.probe())
    assert scanner.available is True

    result = asyncio.run(scanner.scan("probe.pdf", make_pdf()))
    assert result["ok"] is True
    found = {entry["tag"]: entry["value"] for entry in result["flagged"]}
    assert found.get("PDF:Producer") == "SecretTool 9000"
    assert found.get("PDF:Author") == "Jo"
    assert found.get("PDF:Keywords") == "mark-1234"


@needs_exiftool
def test_oversized_input_is_skipped_rather_than_run():
    scanner = metascan.MetaScanner(enabled=True, max_bytes=10)
    asyncio.run(scanner.probe())
    result = asyncio.run(scanner.scan("probe.pdf", make_pdf()))
    assert result["ok"] is False
    assert "limit" in result["error"]


@needs_exiftool
def test_garbage_bytes_do_not_crash_the_scan():
    scanner = metascan.MetaScanner(enabled=True)
    asyncio.run(scanner.probe())
    result = asyncio.run(scanner.scan("junk.pdf", b"\x00\x01\x02not a pdf at all"))
    assert isinstance(result, dict)
    assert result["flagged"] == [] or result["ok"] is True


# -- through the API --------------------------------------------------------


@needs_exiftool
def test_scan_surfaces_metadata_the_engine_missed(monkeypatch):
    monkeypatch.setenv("GUI_EXIFTOOL", "1")
    client = build_client(Settings())
    try:
        response = client.post(
            "/api/scan/files", files={"files": ("probe.pdf", make_pdf(), "application/pdf")}
        )
        item = response.json()["items"][0]
        assert item["ok"] is True
        scan = item["metadata_scan"]
        assert scan["ok"] is True
        assert "PDF:Author" in {entry["tag"] for entry in scan["flagged"]}
        # The fake engine calls this file unremarkable; the metadata check is
        # what makes it suspicious, which is the whole point of running it.
        assert item["suspicious"] is True
    finally:
        client.__exit__(None, None, None)


def test_scan_is_untouched_when_the_check_is_off(client):
    response = client.post(
        "/api/scan/files", files={"files": ("probe.pdf", make_pdf(), "application/pdf")}
    )
    item = response.json()["items"][0]
    assert item["metadata_scan"] is None
    assert item["suspicious"] is False


def test_status_reports_the_check(client):
    body = client.get("/api/status").json()
    assert body["metadata_check"]["enabled"] is False
    assert body["metadata_check"]["tool"] == "exiftool"


@needs_exiftool
def test_status_reports_a_switched_on_check(monkeypatch):
    monkeypatch.setenv("GUI_EXIFTOOL", "1")
    client = build_client(Settings())
    try:
        check = client.get("/api/status").json()["metadata_check"]
        assert check["enabled"] is True
        assert check["available"] is True
        assert check["version"]
    finally:
        client.__exit__(None, None, None)


def test_status_reports_a_broken_check(monkeypatch):
    monkeypatch.setenv("GUI_EXIFTOOL", "1")
    monkeypatch.setenv("GUI_EXIFTOOL_PATH", "exiftool-that-does-not-exist")
    client = build_client(Settings())
    try:
        check = client.get("/api/status").json()["metadata_check"]
        assert check["enabled"] is True
        assert check["available"] is False
        assert "not on PATH" in check["error"]
    finally:
        client.__exit__(None, None, None)


@needs_exiftool
def test_removal_that_leaves_metadata_behind_is_not_reported_as_verified(monkeypatch):
    """The fake engine strips invisible characters and nothing else.

    A PDF whose only marks are in its Info dictionary therefore comes back
    unchanged, and the engine's own re-inspection calls that a pass. The
    metadata re-check is what catches it.
    """
    monkeypatch.setenv("GUI_EXIFTOOL", "1")
    client = build_client(Settings())
    try:
        scan = client.post(
            "/api/scan/files", files={"files": ("probe.pdf", make_pdf(), "application/pdf")}
        ).json()
        scan_id = scan["items"][0]["id"]

        result = client.post("/api/clean", json={"ids": [scan_id], "options": {}}).json()
        item = result["items"][0]
        assert item["ok"] is True
        assert any(tag.startswith("PDF:Author") for tag in item["remaining_metadata"])
        assert item["verified"] is False
    finally:
        client.__exit__(None, None, None)


def test_remaining_metadata_stays_empty_when_the_check_is_off(client):
    scan = client.post(
        "/api/scan/files", files={"files": ("probe.pdf", make_pdf(), "application/pdf")}
    ).json()
    result = client.post(
        "/api/clean", json={"ids": [scan["items"][0]["id"]], "options": {}}
    ).json()
    assert result["items"][0]["remaining_metadata"] == []
