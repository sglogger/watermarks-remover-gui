"""Stay in step with an engine we do not control.

Two independent checks, both non-blocking by design. This GUI is meant to keep
working across upstream releases, so a surprise never becomes a crash: it
becomes a banner that names exactly what changed.

* :func:`check_contract` reads the engine's own ``/openapi.json`` at startup and
  confirms the routes and clean options we rely on still exist. It also reports
  the option list the engine actually accepts, which is what the UI renders — so
  an option added or dropped upstream shows up without a code change here.
* :func:`fetch_latest_release` asks GitHub for the newest engine release, purely
  so the UI can say "a newer version exists". This is the app's only outbound
  network call and it can be switched off entirely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

#: Routes this GUI calls. Missing ones are reported, not fatal — the affected
#: feature degrades (e.g. no batch endpoint means we fall back to per-file calls).
REQUIRED_PATHS = ("/health", "/capabilities", "/inspect", "/clean")
OPTIONAL_PATHS = ("/inspect/batch", "/clean/batch")

#: Layer B rewrite tactics the engine knows, mirrored from its `rewrite_text`
#: module. Kept here only to reject a typo before it becomes a 400: the engine
#: validates the strategy itself and remains the authority.
KNOWN_TACTICS = frozenset(
    {"paraphrase", "backtranslate", "structural", "humanize", "code", "chunk", "mlm"}
)

#: Tactics that need an LLM behind `WATERMARKS_REWRITE_BACKEND`. `mlm` is the
#: one exception: it infills locally with roberta-large, so it needs
#: transformers inside the engine image instead.
LLM_TACTICS = KNOWN_TACTICS - {"mlm"}


class InvalidStrategy(ValueError):
    """A rewrite strategy the engine would reject."""


def parse_strategy(spec: str) -> list[tuple[str, float]]:
    """Parse ``"paraphrase@0.8,mlm@0.2"`` into ``[(tactic, intensity)]``.

    Mirrors the engine's own parser so a mistyped tactic is a message beside
    the field rather than a failed clean. Intensity is exclusive of 0 and
    inclusive of 1, exactly as upstream has it.
    """
    if not spec or not spec.strip():
        raise InvalidStrategy("A strategy needs at least one tactic@intensity step.")
    steps: list[tuple[str, float]] = []
    for raw in spec.split(","):
        item = raw.strip()
        if "@" not in item:
            raise InvalidStrategy(
                f"{item or spec!r} is not a step; write it as tactic@intensity, "
                "for example paraphrase@0.8."
            )
        tactic, _, raw_level = item.rpartition("@")
        tactic = tactic.strip()
        if tactic not in KNOWN_TACTICS:
            raise InvalidStrategy(
                f"{tactic!r} is not a tactic this engine knows. Available: "
                + ", ".join(sorted(KNOWN_TACTICS))
                + "."
            )
        try:
            level = float(raw_level)
        except ValueError:
            raise InvalidStrategy(
                f"{raw_level!r} is not a number; intensity runs from just above 0 to 1."
            ) from None
        if not 0 < level <= 1:
            raise InvalidStrategy(
                f"intensity {level} is out of range; it must be above 0 and at most 1."
            )
        steps.append((tactic, level))
    return steps


def strategy_needs_llm(spec: str) -> bool:
    """True when *spec* has a step that requires an LLM rewrite backend."""
    try:
        steps = parse_strategy(spec)
    except InvalidStrategy:
        return False
    return any(tactic in LLM_TACTICS for tactic, _ in steps)


#: Clean options the UI offers today, with the safe default and a warning for
#: the ones that can change content beyond the watermark itself.
#:
#: ``type`` is ``"bool"`` for a checkbox, ``"choice"`` for a select backed by
#: ``choices``, or ``"text"`` for a free-text string. The engine has had
#: string-valued options since v0.6.0 (``deep_images``), so a boolean-only
#: pipeline would either drop them or send a value the engine now rejects
#: outright. v0.7.0 added two free-form string options — ``strategy`` and
#: ``style`` — which is what ``"text"`` exists for.
#:
#: ``applies_to`` names the engine ``kind`` an option actually does anything
#: for, so the UI can say so instead of implying it affects every upload.
KNOWN_OPTIONS: dict[str, dict[str, Any]] = {
    "keep_non_ai_metadata": {
        "label": "Keep non-AI metadata",
        "help": "Preserve camera, author and timestamp fields; remove only AI provenance markers.",
        "type": "bool",
        "default": True,
        "risk": None,
    },
    "also_layer_a_text": {
        "label": "Also scan text inside documents",
        "help": "Look for invisible characters in the text parts of PDFs, DOCX, EPUB and friends.",
        "type": "bool",
        "default": True,
        "risk": None,
    },
    "normalize_spaces": {
        "label": "Normalise unusual spaces",
        "help": (
            "Fold non-breaking, thin and other exotic space characters down to "
            "an ordinary space. On by default in the engine."
        ),
        "type": "bool",
        "default": True,
        "risk": (
            "Turn this off for typography that relies on non-breaking spaces — "
            "French punctuation, or a unit kept on the same line as its number."
        ),
    },
    "deep_images": {
        "label": "PDF: reach metadata inside embedded images",
        "help": (
            "A PDF can carry AI and C2PA markers inside the images it embeds, "
            "where a normal metadata strip never looks. Clearing them means "
            "re-distilling the PDF through Ghostscript."
        ),
        "type": "choice",
        "default": "auto",
        "choices": [
            {
                "value": "auto",
                "label": "Auto",
                "help": "Only re-distill when markers survive the ordinary strip.",
            },
            {
                "value": "always",
                "label": "Always",
                "help": "Re-distill every PDF, also clearing non-AI EXIF inside images.",
            },
            {
                "value": "lossless",
                "label": "Lossless",
                "help": "Re-distill without recompressing the embedded images.",
            },
            {
                "value": "never",
                "label": "Never",
                "help": "Skip the pass; markers inside embedded images stay.",
            },
        ],
        "risk": (
            "Always re-encodes embedded images and drops their camera metadata; "
            "Never leaves markers inside images in place."
        ),
        #: Engine capability this option needs to do anything. The published
        #: engine image ships without Ghostscript, and the engine then reports
        #: the pass as skipped rather than failing, so the UI says so up front.
        "requires_tool": "ghostscript",
    },
    "aggressive_homoglyphs": {
        "label": "Aggressive homoglyph replacement",
        "help": "Also replace Latin lookalikes and fullwidth characters.",
        "type": "bool",
        "default": False,
        "risk": "Can alter legitimate non-Latin text and code samples.",
    },
    "nfkc": {
        "label": "Unicode NFKC normalisation",
        "help": "Normalise the whole text to NFKC after cleaning.",
        "type": "bool",
        "default": False,
        "risk": "Rewrites ligatures, fractions and formatting characters throughout the document.",
    },
    "strip_all_metadata": {
        "label": "Strip all metadata",
        "help": "Remove every metadata field, not just the AI provenance ones.",
        "type": "bool",
        "default": False,
        "risk": "Destroys copyright, camera and authorship information permanently.",
    },
    # -- v0.7.0: Layer B statistical-mark rewriting ---------------------------
    # Layer A edits characters; Layer B edits wording. Since v0.7.0 the engine
    # runs Layer B on every plain-text clean and refuses the request outright
    # when no strategy is configured, so this is not an optional extra for
    # `.txt` — it is the thing that decides whether cleaning works at all.
    "strategy": {
        "label": "Rewrite strategy (plain text only)",
        "help": (
            "Statistical watermarks live in word choice, so removing them means "
            "rewriting the prose. Steps are tactic@intensity, comma separated — "
            "for example paraphrase@0.8,mlm@0.2. Leave this empty to use the "
            "strategy the engine itself is configured with."
        ),
        "type": "text",
        "default": "",
        "placeholder": "paraphrase@0.8,mlm@0.2",
        "risk": (
            "Rewrites the wording of the document, not just its invisible "
            "characters. The meaning is preserved; the sentences are not."
        ),
        "applies_to": "text",
        #: Every tactic but `mlm` calls out to an LLM; `mlm` needs transformers
        #: inside the engine image. Neither is present in the published build.
        "tactics": sorted(KNOWN_TACTICS),
    },
    "style": {
        "label": "Writing style for the rewrite",
        "help": (
            "A plain-language style instruction appended to the rewrite prompt, "
            "e.g. 'plain and direct'. Most meaningful with the humanize tactic, "
            "and only used by steps that call an LLM."
        ),
        "type": "text",
        "default": "",
        "placeholder": "plain and direct",
        "applies_to": "text",
    },
    # -- v0.7.0: scoring the text before and after ----------------------------
    "detect_before": {
        "label": "Score the file before cleaning",
        "help": (
            "Run the configured watermark detectors over the original and "
            "record the result in the report."
        ),
        "type": "bool",
        "default": False,
        "risk": None,
        "requires_detector": True,
    },
    "detect_after": {
        "label": "Score the file after cleaning",
        "help": (
            "Run the same detectors over the cleaned output, so the report "
            "shows whether the score actually moved."
        ),
        "type": "bool",
        "default": False,
        "risk": None,
        "requires_detector": True,
    },
}

#: Options we never surface. `remove_pixel` regenerates image pixels through
#: CtrlRegen or MarkDiffusion, neither of which ships in the published engine
#: image; `remove_audio_watermark` drives the v0.7.0 destructive audio chain,
#: and audio is out of scope for this GUI (see `formats.sniff_av`). Listing
#: them here is what stops the contract check from reporting them as options
#: the UI forgot to add.
HIDDEN_OPTIONS = {"remove_pixel", "remove_audio_watermark"}


@dataclass
class ContractStatus:
    ok: bool = True
    checked: bool = False
    missing_paths: list[str] = field(default_factory=list)
    degraded_paths: list[str] = field(default_factory=list)
    #: Option names the engine currently accepts, or None when unknown.
    accepted_options: list[str] | None = None
    unknown_options: list[str] = field(default_factory=list)
    dropped_options: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def batch_supported(self) -> bool:
        return not self.degraded_paths

    def messages(self) -> list[str]:
        """Drift the user should act on."""
        out: list[str] = []
        if self.error:
            out.append(f"Could not read the engine's API contract: {self.error}")
        if self.missing_paths:
            out.append(
                "The engine no longer serves: " + ", ".join(self.missing_paths) + "."
            )
        if self.dropped_options:
            out.append(
                "The engine dropped these options: "
                + ", ".join(self.dropped_options)
                + ". They are hidden from the Advanced panel."
            )
        if self.unknown_options:
            out.append(
                "The engine gained options this UI does not expose yet: "
                + ", ".join(self.unknown_options)
                + "."
            )
        return out

    def notes(self) -> list[str]:
        """Drift that changes nothing the user can see or fix.

        Kept out of `messages` so the UI does not shout about it: a missing
        batch endpoint costs a few extra round trips and nothing else.
        """
        if not self.degraded_paths:
            return []
        return [
            "This engine build has no batch endpoints ("
            + ", ".join(self.degraded_paths)
            + "), so files are processed one at a time."
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "batch_supported": self.batch_supported,
            "missing_paths": self.missing_paths,
            "degraded_paths": self.degraded_paths,
            "unknown_options": self.unknown_options,
            "dropped_options": self.dropped_options,
            "messages": self.messages(),
            "notes": self.notes(),
        }


def extract_clean_options(spec: dict[str, Any]) -> list[str] | None:
    """Read the accepted clean-option names out of an OpenAPI spec.

    Returns None when the spec does not describe them in the shape we know; the
    caller then falls back to the built-in list rather than hiding everything.
    """
    try:
        schema = spec["paths"]["/clean"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        options = schema["properties"]["options"]["properties"]
    except (KeyError, TypeError):
        return None
    if not isinstance(options, dict):
        return None
    return sorted(options)


def check_contract(spec: dict[str, Any] | None, error: str | None = None) -> ContractStatus:
    """Compare the engine's advertised API against what this GUI needs."""
    status = ContractStatus()
    if spec is None:
        status.error = error or "no specification returned"
        status.ok = False
        return status

    status.checked = True
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        status.error = "the specification has no usable `paths` section"
        status.ok = False
        return status

    status.missing_paths = [p for p in REQUIRED_PATHS if p not in paths]
    status.degraded_paths = [p for p in OPTIONAL_PATHS if p not in paths]

    accepted = extract_clean_options(spec)
    status.accepted_options = accepted
    if accepted is not None:
        offered = set(KNOWN_OPTIONS)
        status.dropped_options = sorted(offered - set(accepted))
        status.unknown_options = sorted(set(accepted) - offered - HIDDEN_OPTIONS)

    status.ok = not status.missing_paths
    return status


def ui_options(status: ContractStatus) -> list[dict[str, Any]]:
    """The Advanced-panel option list, filtered by what the engine accepts."""
    accepted = status.accepted_options
    out: list[dict[str, Any]] = []
    for name, meta in KNOWN_OPTIONS.items():
        if accepted is not None and name not in accepted:
            continue
        out.append({"name": name, **meta})
    return out


def default_options(status: ContractStatus) -> dict[str, Any]:
    """The value every option starts at — the conservative choice throughout."""
    return {opt["name"]: opt["default"] for opt in ui_options(status)}


def coerce_option(spec: dict[str, Any], value: Any) -> Any:
    """Coerce *value* to something the engine will accept for *spec*.

    Anything unusable falls back to the option's default rather than being
    forwarded. That matters for the choice options: since v0.6.0 the engine
    rejects the whole request when it sees a value outside the enum, where it
    used to quietly substitute its own default.
    """
    kind = spec.get("type")
    if kind == "choice":
        allowed = {choice["value"] for choice in spec.get("choices") or ()}
        if isinstance(value, str) and value in allowed:
            return value
        return spec["default"]
    if kind == "text":
        return value.strip() if isinstance(value, str) else spec["default"]
    return bool(value)


def validate_options(values: dict[str, Any]) -> list[tuple[str, str]]:
    """Problems with *values* the user can fix, as ``(option name, message)``.

    Only the strategy has a grammar worth checking here. Everything else is a
    checkbox or a closed enum, and `coerce_option` has already made those safe.
    The option name comes back with the message so the caller can reset exactly
    the field that failed, without matching on prose.
    """
    problems: list[tuple[str, str]] = []
    strategy = values.get("strategy")
    if isinstance(strategy, str) and strategy.strip():
        try:
            parse_strategy(strategy)
        except InvalidStrategy as exc:
            problems.append(("strategy", f"Rewrite strategy: {exc}"))
    return problems


def engine_options(values: dict[str, Any]) -> dict[str, Any]:
    """The subset of *values* to actually put on the wire.

    An empty text option means "leave it to the engine", but the engine does
    not read it that way: it validates `strategy` whenever the key is present,
    and an empty string fails that check with a 400. So empty strings are
    dropped rather than sent.
    """
    return {
        name: value
        for name, value in values.items()
        if not (isinstance(value, str) and not value.strip())
    }


@dataclass
class ReleaseInfo:
    current: str | None = None
    latest: str | None = None
    url: str | None = None
    published: str | None = None
    outdated: bool = False
    checked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "url": self.url,
            "published": self.published,
            "outdated": self.outdated,
            "checked": self.checked,
        }


class ReleaseChecker:
    """Caches the newest upstream release for a day; failures are silent."""

    def __init__(self, url: str, *, enabled: bool = True, ttl: float = 86400.0) -> None:
        self._url = url
        self._enabled = enabled
        self._ttl = ttl
        self._cached: ReleaseInfo | None = None
        self._fetched_at = 0.0

    async def get(self, current_version: str | None) -> ReleaseInfo:
        if not self._enabled:
            return ReleaseInfo(current=current_version, checked=False)

        now = time.monotonic()
        if self._cached is None or now - self._fetched_at > self._ttl:
            self._cached = await self._fetch()
            self._fetched_at = now

        info = ReleaseInfo(
            current=current_version,
            latest=self._cached.latest,
            url=self._cached.url,
            published=self._cached.published,
            checked=self._cached.checked,
        )
        info.outdated = bool(
            info.latest
            and current_version
            and _normalise(current_version) != _normalise(info.latest)
        )
        return info

    async def _fetch(self) -> ReleaseInfo:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    self._url, headers={"Accept": "application/vnd.github+json"}
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError):
            return ReleaseInfo(checked=False)
        return ReleaseInfo(
            latest=data.get("tag_name"),
            url=data.get("html_url"),
            published=data.get("published_at"),
            checked=True,
        )


def _normalise(version: str) -> str:
    return version.strip().lstrip("vV")
