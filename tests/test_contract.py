from __future__ import annotations

from app import contract
from tests.fake_engine import openapi_spec


def test_a_matching_engine_passes():
    status = contract.check_contract(openapi_spec())
    assert status.ok and status.checked
    assert status.batch_supported
    assert status.messages() == []


def test_a_missing_route_is_reported_but_not_fatal():
    spec = openapi_spec()
    del spec["paths"]["/clean"]
    status = contract.check_contract(spec)
    assert status.ok is False
    assert status.missing_paths == ["/clean"]
    assert "/clean" in status.messages()[0]


def test_missing_batch_routes_only_degrade():
    spec = openapi_spec()
    del spec["paths"]["/clean/batch"]
    status = contract.check_contract(spec)
    assert status.ok is True
    assert status.batch_supported is False


def test_an_option_dropped_upstream_disappears_from_the_ui():
    spec = openapi_spec(options=["keep_non_ai_metadata", "also_layer_a_text"])
    status = contract.check_contract(spec)
    assert "nfkc" in status.dropped_options
    names = [opt["name"] for opt in contract.ui_options(status)]
    assert names == ["keep_non_ai_metadata", "also_layer_a_text"]
    assert "nfkc" not in contract.default_options(status)


def test_an_option_added_upstream_is_surfaced_as_a_message():
    spec = openapi_spec(
        options=[
            "nfkc",
            "aggressive_homoglyphs",
            "keep_non_ai_metadata",
            "also_layer_a_text",
            "strip_all_metadata",
            "brand_new_knob",
        ]
    )
    status = contract.check_contract(spec)
    assert status.unknown_options == ["brand_new_knob"]
    assert any("brand_new_knob" in m for m in status.messages())


def test_hidden_options_are_never_offered():
    status = contract.check_contract(openapi_spec())
    names = {opt["name"] for opt in contract.ui_options(status)}
    assert names.isdisjoint(contract.HIDDEN_OPTIONS)
    assert status.unknown_options == []


def test_an_unreadable_spec_is_survivable():
    status = contract.check_contract(None, error="connection refused")
    assert status.ok is False and status.checked is False
    assert "connection refused" in status.messages()[0]
    # With no spec we still offer the built-in defaults rather than nothing.
    assert contract.default_options(status)


def test_an_unfamiliar_spec_shape_falls_back_to_the_builtin_options():
    assert contract.extract_clean_options({"paths": {"/clean": {"post": {}}}}) is None


def test_a_harmless_batch_gap_is_a_quiet_note_not_a_warning():
    spec = openapi_spec()
    del spec["paths"]["/inspect/batch"]
    del spec["paths"]["/clean/batch"]
    status = contract.check_contract(spec)
    assert status.messages() == []
    assert len(status.notes()) == 1
    assert "one at a time" in status.notes()[0]
    assert status.to_dict()["notes"] == status.notes()


def test_a_string_option_is_offered_as_a_choice_not_a_checkbox():
    status = contract.check_contract(openapi_spec())
    spec = next(o for o in contract.ui_options(status) if o["name"] == "deep_images")
    assert spec["type"] == "choice"
    assert [c["value"] for c in spec["choices"]] == ["auto", "always", "lossless", "never"]
    # The default reaches the request as the engine's own enum value, not True.
    assert contract.default_options(status)["deep_images"] == "auto"


def test_a_choice_keeps_a_valid_value_and_rejects_anything_else():
    status = contract.check_contract(openapi_spec())
    spec = next(o for o in contract.ui_options(status) if o["name"] == "deep_images")
    assert contract.coerce_option(spec, "lossless") == "lossless"
    # v0.6.0 fails the whole request on an out-of-enum value, so a bad one has
    # to become the default here rather than travel to the engine.
    for bad in ("sideways", "", True, 1, None, ["always"]):
        assert contract.coerce_option(spec, bad) == "auto"


def test_a_boolean_option_still_coerces_to_a_boolean():
    status = contract.check_contract(openapi_spec())
    spec = next(o for o in contract.ui_options(status) if o["name"] == "nfkc")
    assert contract.coerce_option(spec, "yes") is True
    assert contract.coerce_option(spec, 0) is False


def test_the_engines_new_options_are_all_accounted_for():
    """Every option v0.6.0 accepts is either offered or deliberately hidden."""
    status = contract.check_contract(openapi_spec())
    assert status.unknown_options == []
    assert status.dropped_options == []
    assert status.messages() == []
