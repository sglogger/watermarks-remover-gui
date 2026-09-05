"""Engine v0.7.0: the evidence object, and the mandatory Layer B rewrite.

These are the three ways v0.7.0 broke or changed the ground under this GUI:
`suspicious` became an object, cleaning text became a rewrite that can be
refused, and two free-text options appeared. Each one is pinned here.
"""

from __future__ import annotations

import pytest

from app import contract
from app.main import evidence_classes, suspicious_verdict
from tests.conftest import build_client
from tests.fake_engine import NBSP, ZWSP

MARKED = f"Hello{ZWSP} world.{NBSP}Second{ZWSP} sentence."


# -- the evidence object -----------------------------------------------------


def test_a_structured_verdict_is_read_from_its_verdict_field():
    """A dict is always truthy, so `bool()` here would flag every file."""
    payload = {"verdict": False, "classes": {}}
    assert suspicious_verdict(payload) is False
    assert bool(payload) is True, "which is exactly the trap being avoided"


def test_a_pre_v070_boolean_verdict_still_works():
    """Older engines answer with a bare boolean; both shapes stay readable."""
    assert suspicious_verdict(True) is True
    assert suspicious_verdict(False) is False
    assert evidence_classes(True) is None


def test_only_the_classes_that_fired_are_reported_strongest_first():
    payload = {
        "verdict": True,
        "classes": {
            "stylometry": {"present": True, "strength": "heuristic", "signals": {"score": 0.9}},
            "layer_a_unicode": {"present": True, "strength": "deterministic", "signals": {}},
            "provenance": {"present": False, "strength": "definitive", "signals": {}},
        },
    }
    names = [entry["name"] for entry in evidence_classes(payload)]
    assert names == ["layer_a_unicode", "stylometry"]


def test_a_clean_file_is_not_flagged_by_the_evidence_object(client):
    body = client.post("/api/scan/text", json={"text": "Nothing unusual here."}).json()
    item = body["items"][0]
    assert item["suspicious"] is False
    assert item["evidence"] == []


def test_a_marked_file_names_the_evidence_class(client):
    body = client.post("/api/scan/text", json={"text": MARKED}).json()
    item = body["items"][0]
    assert item["suspicious"] is True
    assert [e["name"] for e in item["evidence"]] == ["layer_a_unicode"]
    assert item["evidence"][0]["strength"] == "deterministic"


# -- Layer B: the refusal ----------------------------------------------------


def test_cleaning_text_without_a_strategy_explains_what_to_configure():
    """The published engine image ships no strategy, so this is the default state."""
    test_client = build_client(default_strategy=None)
    try:
        scan = test_client.post("/api/scan/text", json={"text": MARKED}).json()
        item = scan["items"][0]
        # Scanning is unaffected: it never asks for cleaned bytes.
        assert item["suspicious"] is True and item["highlight"]["spans"]

        cleaned = test_client.post("/api/clean", json={"ids": [item["id"]]}).json()
        failed = cleaned["items"][0]
        assert failed["ok"] is False
        assert "Layer B" in failed["error"]
        # Not just the engine's sentence -- what to do about it.
        assert "WATERMARKS_REWRITE_BACKEND" in failed["error"]
        assert "README" in failed["error"]
    finally:
        test_client.__exit__(None, None, None)


def test_a_configured_strategy_cleans_text_and_reports_the_rewrite():
    test_client = build_client(default_strategy=None)
    try:
        options = {"strategy": "mlm@0.4"}
        scan = test_client.post(
            "/api/scan/text", json={"text": MARKED, "options": options}
        ).json()
        item = scan["items"][0]
        cleaned = test_client.post(
            "/api/clean", json={"ids": [item["id"]], "options": options}
        ).json()
        result = cleaned["items"][0]
        assert result["ok"] is True
        assert ZWSP not in result["text"]
        assert result["report"]["layer_b"]["strategy"] == "mlm@0.4"
    finally:
        test_client.__exit__(None, None, None)


def test_containers_clean_without_any_rewrite_backend():
    """Layer B is a text-pipeline requirement; Markdown never touches it."""
    test_client = build_client(default_strategy=None)
    try:
        scan = test_client.post(
            "/api/scan/text", json={"text": MARKED, "format": "markdown"}
        ).json()
        item = scan["items"][0]
        cleaned = test_client.post("/api/clean", json={"ids": [item["id"]]}).json()
        assert cleaned["items"][0]["ok"] is True
    finally:
        test_client.__exit__(None, None, None)


# -- Layer B: the free-text options ------------------------------------------


def test_an_empty_strategy_is_never_put_on_the_wire(client):
    """The engine validates `strategy` whenever the key is present, and "" fails."""
    assert contract.engine_options({"strategy": "", "style": "  ", "nfkc": True}) == {
        "nfkc": True
    }
    # End to end: the default (empty) strategy must not turn a clean into a 400.
    scan = client.post("/api/scan/text", json={"text": MARKED}).json()
    cleaned = client.post("/api/clean", json={"ids": [scan["items"][0]["id"]]}).json()
    assert cleaned["items"][0]["ok"] is True


def test_a_mistyped_strategy_warns_and_falls_back_instead_of_failing(client):
    body = client.post(
        "/api/scan/text", json={"text": MARKED, "options": {"strategy": "paraphrsae@0.8"}}
    ).json()
    assert any("Rewrite strategy" in w for w in body["warnings"])
    assert any("paraphrsae" in w for w in body["warnings"])
    # The scan still ran on the engine's own default rather than being refused.
    assert body["items"][0]["suspicious"] is True


@pytest.mark.parametrize(
    "spec",
    ["paraphrase@0.8,mlm@0.2", "humanize@1", "chunk@0.05"],
)
def test_valid_strategies_parse(spec):
    assert contract.parse_strategy(spec)


@pytest.mark.parametrize(
    "spec",
    ["", "paraphrase", "nonsense@0.5", "paraphrase@0", "paraphrase@1.5", "paraphrase@x"],
)
def test_invalid_strategies_are_rejected_before_the_engine_sees_them(spec):
    with pytest.raises(contract.InvalidStrategy):
        contract.parse_strategy(spec)


def test_a_style_is_forwarded_untouched(client):
    scan = client.post(
        "/api/scan/text",
        json={"text": MARKED, "options": {"style": "plain and direct"}},
    ).json()
    assert scan["items"][0]["suspicious"] is True
    assert scan["warnings"] == []


# -- the rest of the v0.7.0 option surface -----------------------------------


def test_normalize_spaces_is_offered_and_defaults_to_on(client):
    options = {o["name"]: o for o in client.get("/api/formats").json()["options"]}
    assert options["normalize_spaces"]["default"] is True
    assert options["normalize_spaces"]["type"] == "bool"


def test_turning_off_space_normalisation_keeps_the_non_breaking_space(client):
    options = {"normalize_spaces": False}
    scan = client.post(
        "/api/scan/text", json={"text": MARKED, "options": options}
    ).json()
    cleaned = client.post(
        "/api/clean", json={"ids": [scan["items"][0]["id"]], "options": options}
    ).json()
    assert NBSP in cleaned["items"][0]["text"]


def test_audio_options_stay_hidden_rather_than_nagging(client):
    """Audio is out of scope here, so it is hidden -- not reported as missing."""
    names = {o["name"] for o in client.get("/api/formats").json()["options"]}
    assert "remove_audio_watermark" not in names
    contract_status = client.get("/api/status").json()["contract"]
    assert contract_status["unknown_options"] == []
    assert contract_status["dropped_options"] == []


def test_asking_to_score_the_file_turns_on_the_engines_detectors(client):
    body = client.post(
        "/api/scan/text", json={"text": MARKED, "options": {"detect_before": True}}
    ).json()
    # The fake engine only fills this in when /inspect was sent detect: true.
    assert body["items"][0]["report"]["text_detectors"]


def test_detectors_stay_off_unless_asked(client):
    body = client.post("/api/scan/text", json={"text": MARKED}).json()
    assert "text_detectors" not in body["items"][0]["report"]
