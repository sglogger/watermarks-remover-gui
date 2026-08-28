# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file tracks **this frontend only**. The engine it drives,
[watermarks-remover](https://github.com/guillaumemeyer/watermarks-remover), has
its own releases and its own changelog; the version this stack runs is pinned by
`WR_CORE_TAG` in `.env` and shown in the application footer.

## [Unreleased]

### Added

- **Optional local metadata check (`GUI_EXIFTOOL=1`).** The GUI can now run its
  own `exiftool` over images, PDF, OOXML, ODT and EPUB after the engine's
  inspection, and report identity-bearing tags beside the engine's report rather
  than merged into it. Off by default: it runs a third-party binary over
  untrusted uploads, which is work the engine otherwise isolates in its own
  container. Bytes are piped in on stdin, so nothing is written to disk.

  It exists because of a measured gap. The engine ships exiftool and calls it,
  but reports back only a couple of lines it judged interesting — against a PDF
  whose Info dictionary held `/Producer`, `/Author` and `/Keywords`, the engine
  returned `findings: []` and `suspicious: false` while exiftool named all
  three. This is the note the engine attaches to every PDF made actionable:
  *"PDF inspection is best-effort; exiftool/c2patool give more reliable metadata
  detection."*

  A flagged tag now makes a file suspicious in its own right, so a PDF whose
  only mark is an author name is offered for removal instead of showing as
  clean. Only identity-bearing tags count — author, producer, GPS, C2PA,
  document IDs, device serials, the IPTC AI marker, free-text fields; a photo's
  exposure time is listed but changes no verdict.
- **Post-removal metadata verification.** The same check re-runs on the cleaned
  bytes, and a surviving tag now marks the result *still flagged*, naming each
  one. Measured against engine v0.6.0: a PNG carrying EXIF, XMP and PNG text
  chunks comes back with EXIF and GPS gone but `PNG:Author`, `PNG:Artist`,
  `PNG:Copyright`, `PNG:Software`, `XMP:CreatorTool` and `XMP:XMPToolkit`
  intact — while the engine's own re-inspection reports it verified clean.
- **`exiftool` in the GUI image**, and `GUI_EXIFTOOL_PATH`,
  `GUI_EXIFTOOL_TIMEOUT`, `GUI_EXIFTOOL_MAX_MB` to tune the check. A check that
  is switched on but cannot run says so in a banner instead of failing quietly.

## [1.1.0] — 2026-08-27

Follows engine [v0.6.0](https://github.com/guillaumemeyer/watermarks-remover/releases/tag/v0.6.0).
`WR_CORE_TAG` now pins `v0.6.0`.

### Added

- **String-valued clean options.** The engine's option list had been boolean
  throughout; v0.6.0 introduced `deep_images`, a four-way enum. The Advanced
  panel now renders a picker for an option the engine types as a string, with
  the consequence of the selected value spelled out beneath it, and sends the
  enum value rather than a coerced boolean.
- **PDF: reach metadata inside embedded images**, the `deep_images` option
  itself. A PDF can carry AI and C2PA markers inside the images it embeds, where
  an ordinary metadata strip never looks.
- **Capability notes on options.** An option that needs a tool the engine image
  does not ship now says so beside the control. The published engine image has
  no Ghostscript, so the deep-image pass reports itself as skipped whichever
  value is picked — worth knowing before choosing one.

### Fixed

- **Invisible characters in Markdown, HTML and SVG are labelled again.** v0.6.0
  gave container reports a `layer_a_hits` list; only the text reports use
  `hits`, which is the single key this app read. Findings in those formats were
  therefore labelled from the Unicode database rather than by the engine.
- **The characters v0.6.0 added to Layer A are no longer counted as deleted
  content.** Noncharacters, reserved default-ignorables (`U+2065`,
  `U+FFF0`–`U+FFF8`, `U+E0000`, `U+E0080`–`U+E0FFF`) and the blank-rendering
  Hangul fillers (`U+115F`, `U+1160`, `U+3164`, `U+FFA0`) are `Cn` and `Lo` —
  outside every Unicode category this app treated as invisible. Each was
  highlighted as a removed region of visible content, with the wrong colour and
  counted in regions rather than characters.
- **An out-of-enum option value can no longer fail a whole request.** v0.6.0
  rejects one where earlier versions substituted their own default, so an
  unusable value now falls back to the option's default here instead.

### Changed

- The engine's batch endpoints (`/inspect/batch`, `/clean/batch`) exist as of
  v0.6.0, so the app uses them instead of falling back to one call per file. No
  code change was needed — the startup contract check picked them up.
- The README's engine-limitations section and `examples/README.md` were
  re-measured against a running v0.6.0 engine rather than updated from the
  release notes. DOCX is fixed — `sample-marked.docx` verifies clean where it
  used to stay flagged with nothing removed. Two limitations remain and are now
  documented as current rather than historical: the SVG pipeline still runs no
  Layer A pass, so invisible characters there are neither reported nor removed;
  and bidirectional marks are reported but deliberately preserved, so a file
  whose only finding is a `U+200E` comes back still flagged by design.
- Test suite is now 76 tests.

### Unchanged, deliberately

- **Audio and video stay refused.** v0.6.0 can strip AI and C2PA metadata from
  MP4/MOV, WAV, MP3 and FLAC. This frontend still refuses them by extension and
  by content sniffing; the README now makes clear that this is a scope decision
  here rather than a limit of the engine.
- **The detection layers stay out.** v0.6.0 added `/detect`, `/detect/batch` and
  a keyed-Gumbel detector. None is surfaced: this app finds and removes marks,
  and `detect_before` / `detect_after` remain hidden options.

## [1.0.0] — 2026-08-19

First release. A web frontend for the watermarks-remover engine: paste text or
drop files in, see where the marks are, remove them.

### Added

- **Text tab** with in-place highlighting of every watermark position, a
  colour-coded legend, and verified removal.
- **Files tab** with multi-file drag & drop, a per-file verdict, expandable
  findings, and download of cleaned copies individually or as a ZIP.
- **Supported formats**: PNG, JPEG, WebP, AVIF, HEIC/HEIF, BMP, GIF, TIFF, SVG,
  PDF, DOCX, XLSX, PPTX, EPUB, ODT, HTML, Markdown and plain text.
- **Audio and video are refused** by extension and by content sniffing, so an
  MP3 renamed to `.png` is rejected before anything reaches the engine.
- **Diff-based position finding.** The engine's report caps positions at ten
  samples per character type, so positions are derived from the difference
  between original and cleaned content instead. This also catches what the
  report misses entirely: the engine's container inspector reports metadata
  findings but does not scan Markdown or HTML for invisible characters.
- **Findings you can navigate.** Each legend entry is a button that walks
  through its own occurrences, with previous/next controls for all of them in
  order. Invisible characters render a visible stand-in glyph; removed blocks of
  markup get a dashed outline and are counted as regions rather than as
  characters.
- **Rich-text paste.** A textarea keeps only the plain-text clipboard flavour,
  which drops markup-level markers. When a paste carries formatting, the
  **Rich text (as pasted)** option unlocks and scans the HTML flavour instead —
  on a Word paste that is one finding more than the plain path returns.
- **Verified removal.** Every cleaned file is run back through the engine and
  the result reported honestly, including the cases where the engine still
  flags it.
- **Confidentiality notice.** Nothing is stored, and the app says plainly that
  this is not the same as privacy.
- **Advanced options** driven by the engine's live option list, with a warning
  on each one that can change content beyond the watermark.
- **Optional protection**, off by default: shared-secret login, per-address rate
  limiting, security headers and a strict Content-Security Policy.
- **Docker Compose stack**: the engine as its published image, this app built
  from source, both with read-only filesystems.
- **Test suite**: 64 tests against a stand-in engine, needing no container.
- **Examples** and an end-to-end smoke test (`examples/demo.sh`).

### Decoupling from the engine

The engine is consumed as a pinned container image over its published HTTP API.
No upstream code is vendored, forked or reimplemented, so an engine upgrade is a
tag bump in `.env`. Three mechanisms keep that honest:

- a **startup contract check** against the engine's own `/openapi.json`, which
  reports drift as a banner instead of failing;
- **option lists read from the engine**, so an option added or dropped upstream
  is reflected without a code change here;
- a **daily release check**, the app's only outbound connection, switchable off.

The contract check earned its place immediately: the released `v0.5.0` image
serves no batch endpoints — those exist only on upstream `main` — and the app
detected that at startup and fell back to per-file calls on its own.

### Known engine limitations

Reported by the app rather than hidden, and documented in the README:

- **DOCX**: the engine detects `docProps/core.xml: ai:Generated by` but removes
  it under no option, and does not apply invisible-character cleaning to the
  document body. Such files are shown as "still flagged".
- **SVG**: the metadata block is removed, but invisible characters in the markup
  are left in place.
- Several invisible characters pass through untouched, among them `U+000B`,
  `U+2028`, `U+FFFC` and the legacy Word hyphens `U+001E` / `U+001F`.

### Notes

- The engine image is published for `linux/amd64` only, so arm64 hosts run it
  emulated. `WR_CORE_PLATFORM` in `.env` handles this and can be cleared once a
  native image exists.
- Upstream keeps only the newest version tag plus `latest`. If a pinned tag is
  pruned, `docker compose pull` fails loudly rather than upgrading silently.

[1.1.0]: https://github.com/sglogger/watermarks-remover-gui/releases/tag/v1.1.0
[1.0.0]: https://github.com/sglogger/watermarks-remover-gui/releases/tag/v1.0.0
