"""The local exiftool pass.

The subprocess tests are skipped when exiftool is not installed; the pure
classification and summarising logic always runs.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import zipfile

import pytest

from app import metascan
from app.config import Settings
from tests.conftest import build_client

HAS_EXIFTOOL = shutil.which("exiftool") is not None
needs_exiftool = pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool is not installed")


def make_pdf(producer: str = "SecretTool 9000") -> bytes:
    """A minimal but valid PDF whose Info dictionary identifies its author."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
    ]
    stream = b"BT /F1 12 Tf 20 100 Td (Hello) Tj ET"
    objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(
        b"<< /Producer (" + producer.encode() + b") /Author (Jo) /Keywords (mark-1234) >>"
    )

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


def make_docx(creator: str = "Francesco Caiafa") -> bytes:
    """A minimal but valid DOCX whose core properties name an author.

    Only the parts exiftool needs to recognise the container and read its
    metadata: the content-types map that makes it a DOCX rather than a bare
    ZIP, and the core properties it maps onto XMP-dc tags.
    """
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>Briefing</dc:title><dc:creator>{creator}</dc:creator>"
        "</cp:coreProperties>"
    )
    types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.'
        'openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("word/document.xml", document)
        archive.writestr("docProps/core.xml", core)
    return buffer.getvalue()


def reads_zip_containers() -> bool:
    """Whether this machine's exiftool can open a ZIP container at all.

    It needs Archive::Zip, a Perl module exiftool only recommends. Without it
    a DOCX reads as a bare ZIP, and the tests that depend on the difference
    would be asserting the environment rather than the code.
    """
    if not HAS_EXIFTOOL:
        return False
    scanner = metascan.MetaScanner(enabled=True)
    asyncio.run(scanner.probe())
    result = asyncio.run(scanner.scan("probe.docx", make_docx()))
    tags = {entry["tag"] for entry in result["flagged"] + result["other"]}
    return "File:FileType" not in tags or any(tag.startswith("XMP") for tag in tags)


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
        # OOXML, and only readable at all since exiftool got a seekable input.
        "XML:LastModifiedBy",
        "XML:Company",
        "XML:Application",
    ],
)
def test_identifying_tags_are_flagged(tag):
    assert metascan.classify_tag(tag) is not None


@pytest.mark.parametrize(
    "tag",
    [
        "EXIF:ExposureTime",
        "PDF:PageCount",
        "File:MIMEType",
        "PNG:BitDepth",
        "EXIF:ImageWidth",
        # A record-format version number, not the program that wrote the file.
        "IPTC:ApplicationRecordVersion",
    ],
)
def test_harmless_tags_are_not_flagged(tag):
    assert metascan.classify_tag(tag) is None


def test_a_tool_name_is_not_reported_as_a_person():
    """CreatorTool is a program. It matches /creator/ too, so order matters."""
    assert metascan.classify_tag("XMP:CreatorTool") == ("authoring tool", metascan.PRIVACY)
    assert metascan.classify_tag("XMP:Creator") == ("identity", metascan.PRIVACY)


@pytest.mark.parametrize(
    "tag, value",
    [
        ("IPTC:DigitalSourceType", "trainedAlgorithmicMedia"),
        ("XMP:CreatorTool", "Stable Diffusion 1.5"),
        ("PDF:Producer", "ChatGPT"),
        ("XMP:C2PAManifest", "present"),
        ("PNG:Parameters", "prompt: a cat, seed: 42"),
    ],
)
def test_ai_markers_are_told_apart_from_privacy_tags(tag, value):
    assert metascan.classify_tag(tag, value)[1] == metascan.AI


@pytest.mark.parametrize(
    "tag, value",
    [
        # The question this whole split answers: a name in a Word file is not
        # evidence that a machine wrote it.
        ("XMP:Creator", "Francesco Caiafa"),
        ("XML:LastModifiedBy", "Francesco Caiafa"),
        ("XML:Company", "Some GmbH"),
        ("PDF:Producer", "Microsoft Word"),
        ("XMP:CreatorTool", "Adobe Photoshop 25.0"),
        ("EXIF:GPSLatitude", "47 deg 22'"),
        # A photograph says so in the very tag that names generated media.
        ("IPTC:DigitalSourceType", "digitalCapture"),
    ],
)
def test_ordinary_metadata_is_not_evidence_of_ai(tag, value):
    reason, kind = metascan.classify_tag(tag, value)
    assert kind == metascan.PRIVACY, f"{tag}={value} classified as {reason}"


def test_an_unknown_ai_tool_still_reports_the_tag():
    """The name list ages; the failure mode must stay harmless.

    A generator this list has never heard of is still reported as an authoring
    tool — described less sharply, never dropped.
    """
    reason, kind = metascan.classify_tag("XMP:CreatorTool", "Nebulizer 9000")
    assert kind == metascan.PRIVACY
    assert reason == "authoring tool"


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


@pytest.mark.skipif(
    not hasattr(os, "memfd_create"), reason="memfd is Linux-only; the pipe fallback is used"
)
def test_memfd_hands_over_the_bytes_seekably():
    """The whole point of the memfd: the reader can seek, and the data survives."""
    fd = metascan._memfd(b"abcdef")
    assert fd is not None
    try:
        os.lseek(fd, 3, os.SEEK_SET)
        assert os.read(fd, 3) == b"def"
        os.lseek(fd, 0, os.SEEK_SET)
        assert os.read(fd, 6) == b"abcdef"
    finally:
        os.close(fd)


@needs_exiftool
@pytest.mark.skipif(
    not hasattr(os, "memfd_create"), reason="a piped DOCX reads as a bare ZIP; memfd is Linux-only"
)
@pytest.mark.skipif(
    not reads_zip_containers(), reason="this exiftool has no Archive::Zip"
)
def test_a_docx_is_read_as_a_document_not_as_a_zip():
    """The regression: piped in on stdin, a DOCX gave up nothing but ZIP fields.

    The engine's own report named the author of the very same file, so "no
    identifying metadata" from this check was not a second opinion — it was a
    wrong one.
    """
    scanner = metascan.MetaScanner(enabled=True)
    asyncio.run(scanner.probe())
    result = asyncio.run(scanner.scan("briefing.docx", make_docx()))

    assert result["ok"] is True
    found = {entry["tag"]: entry["value"] for entry in result["flagged"]}
    creators = [value for tag, value in found.items() if tag.endswith(":Creator")]
    assert "Francesco Caiafa" in creators
    all_tags = {entry["tag"] for entry in result["flagged"] + result["other"]}
    assert not all_tags <= {tag for tag in all_tags if tag.startswith(("ZIP:", "File:"))}


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
        # Reported, with the reason, and marked as a privacy tag rather than
        # as evidence of anything AI.
        author = next(e for e in scan["flagged"] if e["tag"] == "PDF:Author")
        assert author["kind"] == metascan.PRIVACY
        # The engine calls this file unremarkable and so does the verdict: an
        # author name in a PDF is not a watermark, and saying "watermarks
        # found" because of one is a false positive, not a second opinion.
        assert item["suspicious"] is False
        assert item["flagged_by"] == []
    finally:
        client.__exit__(None, None, None)


@needs_exiftool
def test_an_ai_marker_in_the_metadata_does_make_a_file_suspicious(monkeypatch):
    """The other side of the split: this one really is the tool's business."""
    monkeypatch.setenv("GUI_EXIFTOOL", "1")
    client = build_client(Settings())
    try:
        response = client.post(
            "/api/scan/files",
            files={"files": ("gen.pdf", make_pdf(producer="Stable Diffusion XL"), "application/pdf")},
        )
        item = response.json()["items"][0]
        assert item["suspicious"] is True
        assert item["flagged_by"] == ["metadata"]
        kinds = {e["tag"]: e["kind"] for e in item["metadata_scan"]["flagged"]}
        assert kinds["PDF:Producer"] == metascan.AI
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

        result = client.post(
            "/api/clean",
            json={"ids": [scan_id], "options": {"strip_all_metadata": True}},
        ).json()
        item = result["items"][0]
        assert item["ok"] is True
        assert any(tag.startswith("PDF:Author") for tag in item["remaining_metadata"])
        assert item["verified"] is False
    finally:
        client.__exit__(None, None, None)


@needs_exiftool
def test_metadata_the_options_asked_to_keep_is_not_a_failed_removal(monkeypatch):
    """`keep_non_ai_metadata` is on by default: the author is meant to survive.

    Reporting it as a leftover would fail a removal that did exactly what it
    was told to do, and would train the reader to ignore the warning.
    """
    monkeypatch.setenv("GUI_EXIFTOOL", "1")
    client = build_client(Settings())
    try:
        scan = client.post(
            "/api/scan/files", files={"files": ("probe.pdf", make_pdf(), "application/pdf")}
        ).json()
        result = client.post(
            "/api/clean", json={"ids": [scan["items"][0]["id"]], "options": {}}
        ).json()
        item = result["items"][0]
        assert item["ok"] is True
        assert item["remaining_metadata"] == []
        assert item["verified"] is True
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
