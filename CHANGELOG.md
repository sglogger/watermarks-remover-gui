# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file tracks **this frontend only**. The engine it drives,
[watermarks-remover](https://github.com/guillaumemeyer/watermarks-remover), has
its own releases and its own changelog; the version this stack runs is pinned by
`WR_CORE_TAG` in `.env` and shown in the application footer.

## [Unreleased]

### Fixed

- **The local metadata check was blind to every Office document.** DOCX, XLSX,
  PPTX, ODT and EPUB are ZIP containers that keep their index at the end of the
  file, and the bytes were handed to exiftool on stdin, which it reads strictly
  forwards. Every one of them came back as `FileType: ZIP` with a dozen archive
  fields and none of the document metadata — so a file whose engine report named
  `XMP-dc:Creator` was reported here as having "no identifying metadata", which
  is worse than saying nothing. The bytes now go through an anonymous `memfd`,
  which is seekable and still never touches a filesystem, and the image installs
  `Archive::Zip` — a module exiftool only recommends, and without which it will
  not open a ZIP container at all. Off Linux the stdin fallback still applies.
- **An author name was treated as evidence of AI.** This is a watermark
  remover, and it was calling a Word document watermarked because someone's
  name sat in its properties. Flagged tags are now classified as either an
  **AI marker** — C2PA, `DigitalSourceType: trainedAlgorithmicMedia`, a
  generation-parameter blob, an authoring tool whose value names a known
  generator — or a **privacy tag**: author, company, GPS, device serial,
  rights, document ID. Only the first kind moves the watermark verdict. The
  second is still reported, still badged, still offered for removal, and no
  longer claims to be something it is not. Two rules now read the tag's value
  rather than its name, because the name cannot settle it: `PDF:Producer` is
  `Microsoft Word` as often as anything else, and `DigitalSourceType` says
  `digitalCapture` for a photograph in the same field where generated media
  says `trainedAlgorithmicMedia` — the old rule flagged both.
- **A removal was failed for doing exactly what it was told.**
  `keep_non_ai_metadata` is on by default, so the engine deliberately preserves
  author and camera fields — and the post-removal re-check then reported those
  same fields as survivors and marked the file "still flagged". The re-check now
  judges against the options that were chosen; only tags that were supposed to
  be stripped can fail a removal.
- **`PNG:Parameters` was not recognised.** Automatic1111 and ComfyUI write the
  whole prompt, seed and sampler blob into that tag, which makes it the most
  common AI marker on a locally generated image, and no rule matched it.
- **"Watermarks found" was reported for files that had none.** An identifying
  metadata tag has always counted as a finding in its own right, and it should:
  an author name left in a document is exactly what people run this tool to get
  rid of. But it was then announced as a watermark. With Office documents newly
  readable (above), a Word file whose only sin is a `dc:creator` started coming
  back as "1 file contain watermarks", which is both wrong and ungrammatical.
  The scan now says which pass raised the flag — the server reports it in a new
  `flagged_by` field — so a metadata-only finding reads "No watermarks found,
  but 1 file carries identifying metadata", carries a `metadata` badge instead
  of `found`, and offers a "Remove identifying metadata" button.
- **The metadata section claimed the engine had missed tags it had reported.**
  "N identifying tags found that the engine report does not list" was printed
  unconditionally. The engine names some of them in its own exiftool lines, so
  the two are now actually compared — by tag name, since the engine reports
  group family 1 (`XMP-dc:Creator`) and the local pass family 0 (`XMP:Creator`).
- **Office identity tags were not flagged as identifying.** `LastModifiedBy`
  (whoever last saved the document), `Company` and `Application` now count as
  findings. They were never reachable before this fix, so no rule had ever been
  written for them. `IPTC:ApplicationRecordVersion`, a format version number
  rather than a program, stays unflagged.

### Added

- **A batch-wide metadata dropdown in the file results.** Directly under the
  scan verdict, "All metadata found" lists every metadata tag either pass read,
  grouped by file: the engine's own exiftool lines, carried in its report as
  pre-formatted console text and split back into tag and value here, and — when
  `GUI_EXIFTOOL` is on — the fuller local pass, whose identifying tags are
  marked with the reason they were flagged. The two are kept apart and labelled,
  because they do not always agree: a DOCX whose XMP names a creator can come
  back with that tag from one pass and not the other. The same data was already
  in each file row, but only for the row you opened — and a "no watermarks
  found" verdict still leaves the question of what the files actually say about
  their author, tooling and origin.

## [1.2.0] — 2026-09-05

Follows engine [v0.7.0](https://github.com/guillaumemeyer/watermarks-remover/releases/tag/v0.7.0).
`WR_CORE_TAG` now pins `v0.7.0`.

v0.7.0 changed two things this GUI had been reading directly, and both were
silent failures rather than errors — the app kept running and kept answering
wrongly. It also made cleaning plain text conditional on a rewrite backend for
the first time, which is a change in what the tool can do out of the box rather
than in how it looks.

### Fixed

- **Every file was being reported as watermarked.** v0.7.0 replaced the
  `suspicious` boolean with an evidence object — `{"verdict": …, "classes":
  {…}}` — so that four incomparable signals stop being flattened into one bit.
  A dict is always truthy in Python, so reading it the old way marked every
  scanned file suspicious, including files with nothing in them. The verdict is
  now read from its own field, and a bare boolean from an older engine still
  works.
- **Every removal was being reported as unverified.** The same shape reaches
  the post-clean re-inspection, where it inverted the result: a file cleaned
  perfectly came back *still flagged*.

### Added

- **Evidence classes are shown, not collapsed.** A scan now lists which kinds
  of evidence fired, strongest first, with the engine's own description of
  each: observable provenance metadata, invisible Unicode carriers, a
  scheme-specific detector hit, a stylometric score. An embedded C2PA manifest
  is a fact about the file and a stylometry score is a guess about its author;
  the badge above them cannot say that, so the list does.
- **Layer B rewrite options — `strategy` and `style`.** Layer A edits
  characters; Layer B edits wording, because a statistical watermark lives in
  word choice and survives any amount of character scrubbing. The Advanced
  panel gained its first free-text controls for these, with the tactic grammar
  (`paraphrase@0.8,mlm@0.2`) validated here so a typo is a message beside the
  field rather than a failed clean. Both are marked as affecting plain text
  only, which is the only pipeline the engine runs Layer B in.
- **`normalize_spaces`**, so a document whose typography depends on
  non-breaking spaces — French punctuation, a unit kept with its number — can
  keep them instead of having them folded to ordinary spaces.
- **`detect_before` and `detect_after`**, which run the engine's configured
  watermark detectors over the file and record the score in the report. Turning
  on the "before" scoring also sets v0.7.0's new `detect` flag on `/inspect`, so
  the detectors run during a scan too. The published engine image configures no
  detectors, and the panel says so beside the control rather than letting the
  option look effective.
- **Rewrite-backend configuration in `docker-compose.yml` and `.env.example`**,
  plus a `config/clean_strategy.json` mounted into the engine read-only. The
  published engine image ships no strategy config, no `transformers` and no LLM,
  so cleaning plain text fails there by default; these are the settings that fix
  it. `WATERMARKS_REWRITE_ALLOW_REMOTE` is documented prominently, because from
  inside a container the host is not loopback and the engine refuses a
  non-loopback rewrite endpoint without it.

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

### Changed

- **Scanning plain text no longer calls `/clean`.** Positions used to come from
  diffing the original against the cleaned bytes. v0.7.0 made the Layer B
  rewrite a mandatory part of cleaning text, so that request would now either be
  refused or spend an LLM paraphrase of the whole document on a preview — and
  return a diff of the entire file instead of the watermark. Plain-text
  positions are read out of the inspect report instead, which is exact, free,
  and works with no rewrite backend at all. Markdown, HTML and SVG still use the
  diff: their pipeline has no Layer B. A side effect is that a scan of pasted
  text now survives a `/clean` outage entirely.
- **A refused clean explains what to configure.** The engine's 400 is unwrapped
  from its JSON envelope and its transport prefix, and a Layer B refusal is
  extended with the environment variables to set and the note that Markdown,
  HTML, PDF, Office and image files clean without any of it.
- **An empty strategy is dropped rather than sent.** An empty text option means
  "use the engine's own default", but the engine validates `strategy` whenever
  the key is present and rejects the empty string — sending it would 400 every
  clean.

### Notes

- **Audio and video stay out of scope.** v0.7.0 added a destructive audio
  cleaning chain and per-frame video purification, and this GUI still refuses
  audio and video before anything reaches the engine. `remove_audio_watermark`
  is therefore hidden deliberately rather than missing: the contract check knows
  about it and stays quiet instead of reporting an option the UI forgot.
- **C2PA in AVIF and HEIC** now surfaces through the ordinary image report, with
  no change here: v0.7.0 learned to recognise the provenance UUID box, and the
  report is rendered structurally.

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
