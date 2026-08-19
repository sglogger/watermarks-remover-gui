from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.config import Settings
from tests.conftest import build_client
from tests.fake_engine import NBSP, ZWSP

MARKED = f"Hello{ZWSP} world.{NBSP}Second{ZWSP} sentence."
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


# -- status and discovery ----------------------------------------------------


def test_status_reports_the_engine_and_a_clean_contract(client):
    body = client.get("/api/status").json()
    assert body["engine"]["ok"] is True
    assert body["engine"]["version"] == "v0.5.0"
    assert body["contract"]["ok"] is True
    assert body["contract"]["messages"] == []
    assert body["auth_required"] is False


def test_status_survives_an_unreachable_engine():
    test_client = build_client(fail={"/health", "/capabilities"})
    try:
        body = test_client.get("/api/status").json()
        assert body["engine"]["ok"] is False
        assert "503" in body["engine"]["error"]
    finally:
        test_client.__exit__(None, None, None)


def test_formats_lists_every_supported_type_and_the_safe_option_defaults(client):
    body = client.get("/api/formats").json()
    assert ".heic" in body["extensions"] and ".pptx" in body["extensions"]
    assert ".mp3" not in body["extensions"]
    names = [opt["name"] for opt in body["options"]]
    assert "strip_all_metadata" in names
    defaults = {opt["name"]: opt["default"] for opt in body["options"]}
    assert defaults["strip_all_metadata"] is False
    assert defaults["keep_non_ai_metadata"] is True


# -- text scanning -----------------------------------------------------------


def test_scanning_text_marks_every_occurrence(client):
    response = client.post(
        "/api/scan/text", json={"text": MARKED, "format": "text", "options": {}}
    )
    item = response.json()["items"][0]
    assert item["ok"] and item["suspicious"]
    assert item["id"]
    spans = item["highlight"]["spans"]
    # Two zero-width spaces removed, one no-break space substituted.
    assert [s["action"] for s in spans] == ["removed", "replaced", "removed"]
    for span in spans:
        assert MARKED[span["start"] : span["end"]] in (ZWSP, NBSP)
    assert item["highlight"]["exact"] is True
    # The browser already has the text it sent; it is not echoed back.
    assert item["text"] is None


def test_clean_text_reports_no_findings(client):
    body = client.post("/api/scan/text", json={"text": "Perfectly ordinary."}).json()
    item = body["items"][0]
    assert item["suspicious"] is False
    assert item["highlight"] is None


def test_empty_text_is_refused_politely(client):
    body = client.post("/api/scan/text", json={"text": "   "}).json()
    assert body["items"] == []
    assert "empty" in body["warnings"][0]


def test_an_unknown_text_format_is_refused(client):
    body = client.post("/api/scan/text", json={"text": "x", "format": "latex"}).json()
    assert body["items"] == []
    assert "latex" in body["warnings"][0]


def test_markdown_is_routed_through_the_container_pipeline(client):
    body = client.post(
        "/api/scan/text", json={"text": f"# Title{ZWSP}", "format": "markdown"}
    ).json()
    assert body["items"][0]["kind"] == "container"


# -- removing ----------------------------------------------------------------


def test_removing_returns_verified_clean_text(client):
    scan = client.post("/api/scan/text", json={"text": MARKED}).json()
    scan_id = scan["items"][0]["id"]

    result = client.post("/api/clean", json={"ids": [scan_id], "options": {}}).json()
    item = result["items"][0]
    assert item["ok"] is True
    assert ZWSP not in item["text"] and NBSP not in item["text"]
    assert item["verified"] is True
    assert item["remaining_hits"] == 0
    assert item["download_url"] == f"/api/download/{scan_id}"


def test_an_expired_scan_says_so_instead_of_failing_silently(client):
    result = client.post("/api/clean", json={"ids": ["gone"]}).json()
    assert result["items"][0]["ok"] is False
    assert "expired" in result["items"][0]["error"]


def test_downloading_before_cleaning_is_a_404(client):
    scan = client.post("/api/scan/text", json={"text": "nothing here"}).json()
    scan_id = scan["items"][0]["id"]
    assert client.get(f"/api/download/{scan_id}").status_code == 404


# -- file uploads ------------------------------------------------------------


def upload(client, files, options=None):
    payload = [("files", (name, io.BytesIO(data), "application/octet-stream")) for name, data in files]
    return client.post(
        "/api/scan/files",
        files=payload,
        data={"options": json.dumps(options or {})},
    ).json()


def test_uploading_a_mixed_batch(client):
    body = upload(
        client,
        [("notes.md", MARKED.encode()), ("shot.png", PNG), ("clean.txt", b"nothing here")],
    )
    by_name = {item["name"]: item for item in body["items"]}
    assert by_name["notes.md"]["suspicious"] is True
    assert by_name["notes.md"]["text"] == MARKED
    assert len(by_name["notes.md"]["highlight"]["spans"]) == 3
    assert by_name["shot.png"]["kind"] == "image"
    assert by_name["shot.png"]["highlight"] is None
    assert by_name["clean.txt"]["suspicious"] is False


def test_audio_is_refused_and_never_reaches_the_engine(client):
    body = upload(client, [("song.mp3", MP3), ("renamed.png", MP3)])
    for item in body["items"]:
        assert item["ok"] is False
        assert item["id"] is None
    reasons = " ".join(item["error"] for item in body["items"])
    assert "mp3 files are not supported" in reasons
    assert "MP3 data" in reasons


def test_an_oversized_upload_is_refused_per_file():
    settings = Settings()
    object.__setattr__(settings, "max_upload_mb", 0)
    test_client = build_client(settings)
    try:
        body = upload(test_client, [("big.txt", b"x" * 2048)])
        assert body["items"][0]["ok"] is False
        assert "larger than" in body["items"][0]["error"]
    finally:
        test_client.__exit__(None, None, None)


def test_too_many_files_are_trimmed_with_a_warning():
    settings = Settings()
    object.__setattr__(settings, "max_files", 2)
    test_client = build_client(settings)
    try:
        body = upload(test_client, [(f"f{i}.txt", b"hi") for i in range(5)])
        assert len(body["items"]) == 2
        assert "first 2 files" in body["warnings"][0]
    finally:
        test_client.__exit__(None, None, None)


def test_cleaning_several_files_offers_a_zip(client):
    body = upload(client, [("a.md", MARKED.encode()), ("b.md", MARKED.encode())])
    ids = [item["id"] for item in body["items"]]

    result = client.post("/api/clean", json={"ids": ids}).json()
    assert all(item["verified"] for item in result["items"])
    assert [item["cleaned_name"] for item in result["items"]] == ["a.cleaned.md", "b.cleaned.md"]

    archive = client.get("/api/download.zip", params={"ids": ",".join(ids)})
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        assert sorted(zf.namelist()) == ["a.cleaned.md", "b.cleaned.md"]
        assert ZWSP not in zf.read("a.cleaned.md").decode()


def test_a_single_download_carries_the_right_filename(client):
    body = upload(client, [("report.md", MARKED.encode())])
    scan_id = body["items"][0]["id"]
    client.post("/api/clean", json={"ids": [scan_id]})
    response = client.get(f"/api/download/{scan_id}")
    assert response.status_code == 200
    assert 'filename="report.cleaned.md"' in response.headers["content-disposition"]
    assert response.headers["content-type"].startswith("text/markdown")


# -- option handling ---------------------------------------------------------


def test_options_the_engine_never_accepted_are_dropped_before_the_call(client):
    # The engine rejects a whole request that carries an unknown option, so an
    # invented one must be filtered out rather than forwarded.
    body = client.post(
        "/api/scan/text",
        json={"text": MARKED, "options": {"made_up": True, "strip_all_metadata": True}},
    ).json()
    assert body["items"][0]["ok"] is True


def test_options_dropped_upstream_are_no_longer_sent():
    test_client = build_client(options=["keep_non_ai_metadata", "also_layer_a_text"])
    try:
        body = test_client.get("/api/formats").json()
        assert [opt["name"] for opt in body["options"]] == [
            "keep_non_ai_metadata",
            "also_layer_a_text",
        ]
        scan = test_client.post(
            "/api/scan/text", json={"text": MARKED, "options": {"nfkc": True}}
        ).json()
        assert scan["items"][0]["ok"] is True
    finally:
        test_client.__exit__(None, None, None)


# -- engine failures ---------------------------------------------------------


def test_a_failing_clean_still_returns_findings():
    test_client = build_client(fail={"/clean", "/clean/batch"})
    try:
        body = test_client.post("/api/scan/text", json={"text": MARKED}).json()
        item = body["items"][0]
        assert item["suspicious"] is True       # inspect worked
        assert item["highlight"] is None        # positions did not
        assert any("positions" in w or "highlight" in w for w in body["warnings"])
    finally:
        test_client.__exit__(None, None, None)


def test_a_failing_inspect_marks_the_items_not_the_request():
    test_client = build_client(fail={"/inspect", "/inspect/batch"})
    try:
        body = test_client.post("/api/scan/text", json={"text": MARKED}).json()
        assert body["items"][0]["ok"] is False
        assert "503" in body["items"][0]["error"]
    finally:
        test_client.__exit__(None, None, None)


def test_batchless_engine_falls_back_to_one_call_per_file():
    test_client = build_client(fail={"/inspect/batch", "/clean/batch"})
    try:
        test_client.app.state.contract.degraded_paths = ["/inspect/batch", "/clean/batch"]
        body = upload(test_client, [("a.md", MARKED.encode()), ("b.md", MARKED.encode())])
        assert all(item["ok"] for item in body["items"])
        assert all(item["suspicious"] for item in body["items"])
    finally:
        test_client.__exit__(None, None, None)


# -- protection --------------------------------------------------------------


def test_security_headers_are_always_present(client):
    headers = client.get("/api/ping").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"


@pytest.fixture
def guarded_client():
    settings = Settings()
    object.__setattr__(settings, "auth_token", "s3cret")
    test_client = build_client(settings)
    yield test_client
    test_client.__exit__(None, None, None)


def test_a_token_protected_instance_refuses_api_calls(guarded_client):
    assert guarded_client.get("/api/status").status_code == 401
    # The shell and its assets stay reachable so the login form can render.
    assert guarded_client.get("/api/ping").status_code == 200


def test_signing_in_unlocks_the_api(guarded_client):
    assert guarded_client.post("/api/login", json={"token": "wrong"}).status_code == 401
    assert guarded_client.post("/api/login", json={"token": "s3cret"}).status_code == 200
    assert guarded_client.get("/api/status").status_code == 200


def test_a_bearer_token_works_without_a_cookie(guarded_client):
    response = guarded_client.get(
        "/api/status", headers={"Authorization": "Bearer s3cret"}
    )
    assert response.status_code == 200


def test_the_rate_limit_kicks_in():
    settings = Settings()
    object.__setattr__(settings, "rate_limit_per_min", 3)
    test_client = build_client(settings)
    try:
        codes = [test_client.get("/api/formats").status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3:] == [429, 429]
        # Static pages are never throttled.
        assert test_client.get("/").status_code == 200
    finally:
        test_client.__exit__(None, None, None)
