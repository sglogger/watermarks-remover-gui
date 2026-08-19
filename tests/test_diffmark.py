from __future__ import annotations

from app import diffmark

ZWSP = "​"
NBSP = " "
TAG_A = "\U000e0041"

HITS = [
    {"codepoint": "U+200B", "char": ZWSP, "label": "ZERO WIDTH SPACE", "kind": "strip"},
    {"codepoint": "U+00A0", "char": NBSP, "label": "NO-BREAK SPACE", "kind": "space"},
]


def test_finds_every_occurrence_not_just_the_reported_samples():
    original = "word" + ZWSP * 30 + "end"
    cleaned = "wordend"
    result = diffmark.highlight(original, cleaned, HITS)
    assert result.exact
    assert result.changed_chars == 30
    assert [(s.start, s.end) for s in result.spans] == [(4, 34)]


def test_distinguishes_removal_from_substitution():
    original = f"a{ZWSP}b{NBSP}c"
    result = diffmark.highlight(original, "ab c", HITS)
    actions = [(s.action, s.kind, s.replacement) for s in result.spans]
    assert actions == [("removed", "strip", ""), ("replaced", "space", " ")]


def test_labels_come_from_the_report():
    result = diffmark.highlight(f"x{ZWSP}", "x", HITS)
    assert result.spans[0].label == "ZERO WIDTH SPACE"
    assert result.legend() == [
        {"kind": "strip", "label": "ZERO WIDTH SPACE", "count": 1, "unit": "characters"}
    ]


def test_an_invisible_character_missing_from_the_report_is_still_a_carrier():
    # U+2062 INVISIBLE TIMES: the engine removed it but did not list it, so the
    # Unicode database has to supply the classification and the label.
    result = diffmark.highlight("a⁢b", "ab", [])
    assert len(result.spans) == 1
    assert result.spans[0].kind == "other_cf"
    assert "INVISIBLE TIMES" in result.spans[0].label


def test_removed_visible_markup_becomes_one_region_not_one_span_per_character():
    # This is what an SVG <metadata> block or an AI <meta generator> tag looks
    # like after cleaning: a long run of ordinary, readable characters.
    original = '<p>hi</p>\n<meta name="generator" content="Claude">\n<p>bye</p>'
    cleaned = original.replace('<meta name="generator" content="Claude">\n', "")
    result = diffmark.highlight(original, cleaned, [])
    assert len(result.spans) == 1
    assert result.spans[0].kind == diffmark.BLOCK_KIND
    # The aligner may settle on an equivalent shifted window (the two `<`
    # characters are interchangeable), so check the extent, not the exact edge.
    assert result.spans[0].end - result.spans[0].start == len(original) - len(cleaned)
    assert 'name="generator"' in result.spans[0].text
    assert result.block_regions == 1
    assert result.carrier_chars == 0
    assert result.legend() == [
        {"kind": "block", "label": diffmark.BLOCK_LABEL, "count": 1, "unit": "regions"}
    ]


def test_blocks_and_hidden_characters_are_tallied_separately():
    original = f'<meta name="generator" content="X">\nText{ZWSP} here{ZWSP}.'
    cleaned = "\nText here."
    result = diffmark.highlight(original, cleaned, HITS)
    assert result.block_regions == 1
    assert result.carrier_chars == 2
    kinds = {entry["kind"]: entry for entry in result.legend()}
    assert kinds["block"]["unit"] == "regions" and kinds["block"]["count"] == 1
    assert kinds["strip"]["unit"] == "characters" and kinds["strip"]["count"] == 2


def test_newlines_inside_a_removed_block_do_not_split_it():
    original = "a<metadata>\n  <dc:creator>AI</dc:creator>\n</metadata>b"
    result = diffmark.highlight(original, "ab", [])
    assert len(result.spans) == 1
    assert result.spans[0].kind == diffmark.BLOCK_KIND


def test_identical_strings_produce_nothing():
    result = diffmark.highlight("same", "same", HITS)
    assert result.spans == []
    assert result.exact


def test_offsets_are_converted_to_utf16_units_for_the_browser():
    # Python code points: A=0, tag char=1, ZWSP=2, B=3.
    # JavaScript UTF-16 units: A=0, tag char=1..2, ZWSP=3, B=4.
    original = f"A{TAG_A}{ZWSP}B"
    result = diffmark.highlight(original, "AB", [])
    assert [(s.start, s.end) for s in result.spans] == [(1, 2), (2, 3)]
    diffmark.to_utf16_offsets(original, result.spans)
    assert [(s.start, s.end) for s in result.spans] == [(1, 3), (3, 4)]


def test_bmp_only_text_is_left_alone_by_the_utf16_conversion():
    original = f"a{ZWSP}b"
    result = diffmark.highlight(original, "ab", HITS)
    before = [(s.start, s.end) for s in result.spans]
    diffmark.to_utf16_offsets(original, result.spans)
    assert [(s.start, s.end) for s in result.spans] == before


def test_falls_back_when_the_transform_is_not_one_to_one():
    # NFKC expands the ffi ligature into three characters, which the linear
    # aligner cannot explain; difflib must take over.
    result = diffmark.highlight(f"oﬃce{ZWSP}", "office", [])
    assert result.exact
    assert result.changed_chars >= 1
    assert any(s.action == "removed" for s in result.spans)


def test_a_huge_unalignable_change_degrades_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(diffmark, "MAX_FALLBACK_CHARS", 2)
    result = diffmark.highlight("aﬃﬃxyzb", "affiffiQb", [])
    assert result.exact is False
    assert result.spans and result.spans[0].action == "changed"
