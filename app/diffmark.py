"""Derive exact watermark positions by diffing original text against cleaned text.

Why this exists: the engine's inspect report caps positions at ten sample
offsets per (codepoint, kind) bucket, which is not enough to mark every
occurrence in a document. Rather than reimplement the engine's character tables
— which would couple us to a moving target — we ask it to clean the text and
read the positions back out of the difference. Whatever the engine considers a
watermark today, the diff finds it.

The engine's text cleaner classifies each character as keep / strip / replace,
so a linear two-pointer walk aligns the two strings exactly and in O(n). That
walk is self-checking: the derived operations are replayed against the original
and the result compared to the real cleaned text. Only if that check fails (a
non-1:1 transform such as NFKC expanding a ligature) do we fall back to
:mod:`difflib`, which is correct but far slower on large inputs.
"""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

#: How far ahead the aligner peeks to tell "character removed" from
#: "character substituted". Long enough to be unambiguous in real text, short
#: enough to stay cheap.
_LOOKAHEAD = 16

#: Above this many differing characters the slow fallback is skipped and the
#: changed region is reported as one coarse span instead. Only reachable when
#: the linear aligner has already failed, which needs an unusual option set.
MAX_FALLBACK_CHARS = 200_000

#: Kind used for a run of ordinary, visible characters the engine removed —
#: an AI `<meta generator>` tag, an SVG `<metadata>` block, a document
#: property. Those are findings too, but they are regions rather than hidden
#: characters, so they are counted and coloured differently.
BLOCK_KIND = "block"
BLOCK_LABEL = "Marked content"

#: Whitespace that carries no signal on its own. Excluded from the carrier test
#: so that the newlines inside a removed markup block do not chop it into
#: dozens of separate spans.
_ORDINARY_WHITESPACE = frozenset(" \t\r\n")

_DEFAULT_LABELS = {
    "strip": "Invisible character",
    "bidi": "Bidirectional control",
    "tag_chars": "Unicode tag character",
    "variation_selector": "Variation selector",
    "zwj_family": "Zero-width joiner",
    "private_use": "Private-use character",
    "space": "Space lookalike",
    "confusable": "Confusable character",
    "other_cf": "Format character",
}


@dataclass
class Span:
    """One highlighted region, addressed in the *original* string."""

    start: int
    end: int
    action: str  # "removed" | "replaced" | "changed"
    kind: str
    label: str
    text: str
    replacement: str = ""
    #: Number of code points this span covers. Tracked separately because
    #: `end - start` is rewritten into UTF-16 units for the browser, which
    #: would otherwise count every astral character — tag characters included
    #: — twice.
    chars: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "action": self.action,
            "kind": self.kind,
            "label": self.label,
            "text": self.text,
            "replacement": self.replacement,
            "chars": self.chars,
        }


@dataclass
class HighlightResult:
    spans: list[Span] = field(default_factory=list)
    #: True when positions are exact; False when we fell back to a coarse region.
    exact: bool = True

    @property
    def changed_chars(self) -> int:
        return sum(s.chars for s in self.spans)

    @property
    def block_regions(self) -> int:
        return sum(1 for s in self.spans if s.kind == BLOCK_KIND)

    @property
    def carrier_chars(self) -> int:
        return sum(s.chars for s in self.spans if s.kind != BLOCK_KIND)

    def legend(self) -> list[dict[str, Any]]:
        """Per-kind tallies, most significant first, for the UI legend.

        Hidden characters are counted individually; removed blocks of visible
        content are counted as regions, because "148 characters" is a useless
        way to describe one deleted `<metadata>` element.
        """
        buckets: dict[tuple[str, str], int] = {}
        for span in self.spans:
            key = (span.kind, span.label)
            step = 1 if span.kind == BLOCK_KIND else span.chars
            buckets[key] = buckets.get(key, 0) + step
        return [
            {
                "kind": kind,
                "label": label,
                "count": count,
                "unit": "regions" if kind == BLOCK_KIND else "characters",
            }
            for (kind, label), count in sorted(
                buckets.items(), key=lambda kv: (-kv[1], kv[0][0])
            )
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "spans": [s.to_dict() for s in self.spans],
            "exact": self.exact,
            "changed_chars": self.changed_chars,
            "carrier_chars": self.carrier_chars,
            "block_regions": self.block_regions,
            "legend": self.legend(),
        }


def _parse_codepoint(raw: Any) -> int | None:
    """Accept "U+200B", "0x200b", 8203 — the report's spelling may change."""
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    for prefix in ("U+", "u+", "0x", "0X"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    try:
        return int(text, 16)
    except ValueError:
        return None


def build_char_index(hits: Iterable[Any]) -> dict[int, tuple[str, str]]:
    """Map codepoint → (kind, label) from an inspect report's ``hits`` list.

    Unknown or malformed entries are skipped rather than raising: the report is
    upstream's to shape, and a highlight without a pretty label is still useful.
    """
    index: dict[int, tuple[str, str]] = {}
    for hit in hits or ():
        if not isinstance(hit, dict):
            continue
        cp = _parse_codepoint(hit.get("codepoint"))
        if cp is None:
            char = hit.get("char")
            if isinstance(char, str) and len(char) == 1:
                cp = ord(char)
        if cp is None:
            continue
        kind = str(hit.get("kind") or "other")
        label = hit.get("label") or _DEFAULT_LABELS.get(kind) or kind.replace("_", " ")
        index[cp] = (kind, str(label))
    return index


def _is_carrier(char: str) -> bool:
    """True for characters that cannot be seen, and so can only be a carrier.

    This asks the Python Unicode database a question about the character
    itself — is it invisible? — rather than asking which watermark scheme it
    belongs to. That keeps the classification independent of the engine's own
    tables, which are free to change.
    """
    if char in _ORDINARY_WHITESPACE:
        return False
    if unicodedata.category(char) in ("Cf", "Cc", "Co", "Mn"):
        return True
    return char.isspace()


def _describe(char: str, index: dict[int, tuple[str, str]]) -> tuple[str, str]:
    known = index.get(ord(char))
    if known is not None:
        kind, label = known
        return kind, label or _DEFAULT_LABELS.get(kind) or f"U+{ord(char):04X}"
    if _is_carrier(char):
        name = unicodedata.name(char, "unnamed character")
        return "other_cf", f"U+{ord(char):04X} {name}"
    return BLOCK_KIND, BLOCK_LABEL


def _linear_ops(a: str, b: str) -> list[tuple[str, int, str]] | None:
    """Align *a* to *b* assuming per-character keep/strip/replace.

    Returns ``(action, index_in_a, replacement)`` triples, or None when the two
    strings cannot be explained that way.
    """
    ops: list[tuple[str, int, str]] = []
    i = j = 0
    la, lb = len(a), len(b)
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        # `a[i]` was either dropped, or swapped for `b[j]`. Peek ahead: whichever
        # reading leaves the two strings back in step is the right one.
        strip_ok = a[i + 1 : i + 1 + _LOOKAHEAD] == b[j : j + _LOOKAHEAD]
        if strip_ok:
            ops.append(("removed", i, ""))
            i += 1
            continue
        replace_ok = a[i + 1 : i + 1 + _LOOKAHEAD] == b[j + 1 : j + 1 + _LOOKAHEAD]
        if replace_ok:
            ops.append(("replaced", i, b[j]))
            i += 1
            j += 1
            continue
        return None
    if j < lb:
        # Characters appeared out of nowhere: not a per-character transform.
        return None
    for k in range(i, la):
        ops.append(("removed", k, ""))
    return ops


def _apply(a: str, ops: list[tuple[str, int, str]]) -> str:
    out: list[str] = []
    changes = {idx: (action, repl) for action, idx, repl in ops}
    for idx, ch in enumerate(a):
        change = changes.get(idx)
        if change is None:
            out.append(ch)
        elif change[0] == "replaced":
            out.append(change[1])
        # "removed" contributes nothing
    return "".join(out)


def _merge(
    ops: list[tuple[str, int, str]], a: str, index: dict[int, tuple[str, str]]
) -> list[Span]:
    """Turn per-character operations into runs sharing action, kind and label."""
    spans: list[Span] = []
    for action, idx, repl in ops:
        kind, label = _describe(a[idx], index)
        last = spans[-1] if spans else None
        if (
            last is not None
            and last.end == idx
            and last.action == action
            and last.kind == kind
            and last.label == label
        ):
            last.end = idx + 1
            last.chars += 1
            last.text += a[idx]
            last.replacement += repl
        else:
            spans.append(
                Span(
                    start=idx,
                    end=idx + 1,
                    action=action,
                    kind=kind,
                    label=label,
                    text=a[idx],
                    replacement=repl,
                )
            )
    return spans


def _fallback_ops(a: str, b: str) -> tuple[list[tuple[str, int, str]], bool]:
    """difflib path for transforms the linear aligner cannot explain."""
    prefix = 0
    limit = min(len(a), len(b))
    while prefix < limit and a[prefix] == b[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < limit - prefix
        and a[len(a) - 1 - suffix] == b[len(b) - 1 - suffix]
    ):
        suffix += 1

    mid_a = a[prefix : len(a) - suffix]
    mid_b = b[prefix : len(b) - suffix]
    if not mid_a:
        return [], True
    if len(mid_a) > MAX_FALLBACK_CHARS or len(mid_b) > MAX_FALLBACK_CHARS:
        # Too large to align character by character; report the region coarsely.
        return [("changed", prefix + k, "") for k in range(len(mid_a))], False

    ops: list[tuple[str, int, str]] = []
    matcher = difflib.SequenceMatcher(None, mid_a, mid_b, autojunk=False)
    for tag, a1, a2, b1, b2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            ops.extend(("removed", prefix + k, "") for k in range(a1, a2))
        elif tag == "replace":
            replacement = mid_b[b1:b2]
            for offset, k in enumerate(range(a1, a2)):
                repl = replacement[offset] if offset < len(replacement) else ""
                ops.append(("replaced" if repl else "removed", prefix + k, repl))
        elif tag == "insert" and a1 > 0:
            # Nothing in the original to point at; attach to the character before.
            ops.append(("replaced", prefix + a1 - 1, mid_a[a1 - 1] + mid_b[b1:b2]))
    ops.sort(key=lambda op: op[1])
    return ops, True


def to_utf16_offsets(text: str, spans: list[Span]) -> None:
    """Re-address *spans* from Python code points to JavaScript UTF-16 units.

    Python indexes strings by code point, JavaScript by UTF-16 code unit. The
    two agree until a character above the BMP appears — and Unicode tag
    characters (U+E0000–U+E007F), a real watermark carrier, live in plane 14.
    Without this conversion every highlight after the first tag character would
    be drawn one position too early.
    """
    if not spans:
        return
    boundaries = sorted({b for span in spans for b in (span.start, span.end)})
    mapping: dict[int, int] = {}
    extra = 0
    cursor = 0
    for index, char in enumerate(text):
        while cursor < len(boundaries) and boundaries[cursor] == index:
            mapping[boundaries[cursor]] = index + extra
            cursor += 1
        if ord(char) > 0xFFFF:
            extra += 1
    while cursor < len(boundaries):
        mapping[boundaries[cursor]] = boundaries[cursor] + extra
        cursor += 1
    if extra == 0:
        return
    for span in spans:
        span.start = mapping.get(span.start, span.start)
        span.end = mapping.get(span.end, span.end)


def highlight(original: str, cleaned: str, hits: Iterable[Any] = ()) -> HighlightResult:
    """Locate every difference between *original* and *cleaned*.

    *hits* is the inspect report's hit list, used only to label spans.
    """
    if original == cleaned:
        return HighlightResult(spans=[], exact=True)

    index = build_char_index(hits)
    exact = True
    ops = _linear_ops(original, cleaned)
    if ops is None:
        ops, exact = _fallback_ops(original, cleaned)
    elif _apply(original, ops) != cleaned:
        ops, exact = _fallback_ops(original, cleaned)

    return HighlightResult(spans=_merge(ops, original, index), exact=exact)
