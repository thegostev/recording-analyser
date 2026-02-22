# MeetingTranscriber — Architecture & WBS Decomposition

Adapted from NASA/SP-20210023927 Work Breakdown Structure standard.

Three rules at every level:
- **100% Rule**: every piece of work appears somewhere in the decomposition
- **Mutual Exclusion Rule**: no work appears in two places
- **80-Hour Rule**: no atomic task (L4) exceeds ~30 minutes of AI execution time

---

## Level 0 — System

**MeetingTranscriber**: Automated audio transcription and analysis service
that ingests recordings from Just Press Record, transcribes and classifies
them via Gemini API, and outputs structured Markdown notes to categorized
Obsidian vaults.

**System boundary**:
- Inside: audio discovery, transcription, classification, analysis,
  file output, state tracking, maintenance/repair tools
- Outside: Just Press Record app (audio capture), Gemini API (ML models),
  Obsidian (rendering/search), iCloud (file sync), launchd (process
  management)

**Stakeholders**:
- Owner/operator: single user (TPM) who records meetings and reviews
  outputs in Obsidian
- Maintainer: same user, assisted by Claude Code

**Quality goals** (from ISO 25010, cascading to all levels):
1. **Reliability** — service runs 24/7 via launchd; must recover from
   crashes, API failures, and iCloud sync issues without data loss
2. **Observability** — all operations produce timestamped structured logs;
   failures must be diagnosable from log files alone
3. **Data integrity** — no audio recording is silently skipped; every file
   is either processed successfully or explicitly logged as failed with
   reason
4. **Functional correctness** — transcription output faithfully represents
   audio content; classification places notes in correct category
5. **Maintainability** — single-developer codebase must remain
   understandable; changes must not require understanding unrelated modules

**Architectural style**: long-running daemon (auto-transcription) +
CLI tools (on-demand processing, maintenance) sharing a common library

> **Quality coverage checkpoint (L0)**: Measurable targets —
> Availability: service auto-restarts within 30s of crash (launchd).
> Data loss: 0 silently dropped recordings per month.
> Log coverage: every file processing attempt produces ≥1 log entry.
> Classification accuracy: <5% files in DEFAULT (miscategorized).

---

## Level 1 — Subsystems

### S1: Transcription Pipeline

Core processing engine shared by all entry points. Takes an audio file
path as input and produces a categorized transcript + analysis in the
correct Obsidian vault folder.

- **Interface**: `process_audio(file_path, timestamp, state)` →
  writes transcript + analysis files, updates state
- **Data ownership**: transcription/analysis Markdown files in
  `{category}/transcripts/` and `{category}/analysis/`

### S2: Service Orchestration

Controls when and how the transcription pipeline runs. Includes the
continuous daemon loop and the CLI entry points for catchup/maintenance.

- **Interface**: launchd plist → `auto_transcribe.py` main loop;
  CLI args → `ondemand_transcribe.py`, `reclassify_and_fix.py`,
  `fix_filenames.py`, `fix_unknown_meetings.py`
- **Data ownership**: service lifecycle, scan scheduling, batch
  coordination

### S3: State & Persistence

Tracks which files have been processed, their status, and provides
deduplication to prevent re-processing.

- **Interface**: `load_state()` / `save_state(state)`;
  `build_transcript_index(folders)` for deduplication
- **Data ownership**: `~/.meeting_transcriber_state.json`,
  `failed_analysis.log`

> **Quality coverage checkpoint (L1)**:
> S1: Error handling via 3-tier classification (fatal/permanent/transient).
> S2: Failover via launchd respawn + per-file error isolation.
> S3: Atomic state writes prevent corruption on crash.

---

## Level 2 — Modules

### S1.M1: Audio Discovery

Scans date-based folder structure under the Just Press Record watch
folder to find unprocessed `.m4a` files.

- **Public interface**: `discover_recent_folders(watch_folder, days_back)`,
  `discover_audio_files(watch_folder, state, transcript_index)`
- **Internal data model**: list of `(file_path, timestamp)` tuples
- **Dependencies**: S3 (state + transcript index for filtering)

### S1.M2: Gemini API Integration

Manages all communication with the Gemini API: file upload, model
invocation, response parsing, and retry logic.

- **Public interface**: `upload_to_gemini(file_path)`,
  `transcribe_with_retry(audio_file)`,
  `analyze_with_retry(transcript_content)`,
  `extract_response_text(response)`
- **Internal data model**: Gemini `File` objects, `GenerateContentResponse`
- **Dependencies**: `google.generativeai` SDK, API key configuration

### S1.M3: Classification & Parsing

Parses the structured response from the Flash model to extract
category, filename, and transcript content.

- **Public interface**: `parse_transcription_response(response_text)` →
  `(category, filename, transcript)`
- **Internal data model**: category keyword map (`FOLDERS` dict)
- **Dependencies**: none (pure parsing logic)

### S1.M4: File Output

Writes transcript and analysis Markdown to the correct Obsidian vault
folder based on category.

- **Public interface**: `save_transcript(category, filename, content)`,
  `save_analysis(category, filename, content)`,
  `log_failed_analysis(transcript_path, category, filename)`
- **Internal data model**: category-to-path mapping in `FOLDERS`
- **Dependencies**: S1.M3 (category output), iCloud-synced filesystem

### S2.M1: Daemon Loop

Infinite scan-process-sleep cycle with rate limiting and per-cycle caps.

- **Public interface**: `main()` → runs until killed;
  `run_scan_cycle(state, transcript_index, cycle_number)`
- **Internal data model**: cycle counter, scan interval (30s),
  max files per cycle (5), inter-file delay (90s)
- **Dependencies**: S1.M1, S1.M2, S1.M3, S1.M4, S3.M1

### S2.M2: CLI Entry Points

argparse-based CLIs for on-demand processing and maintenance tasks.

- **Public interface**: `ondemand_transcribe.py` CLI
  (`--catchup`, `--reprocess-partial`, `--dry-run`);
  `reclassify_and_fix.py` CLI (`--generate-missing-analysis`,
  `--reclassify`). Additional utility scripts in `utils/` (not committed):
  `fix_filenames.py`, `fix_unknown_meetings.py`
- **Internal data model**: CLI argument namespaces
- **Dependencies**: S1 pipeline functions (imported from auto_transcribe)

### S2.M3: Maintenance & Repair

Specialized logic for fixing misclassified files, renaming incorrectly
dated files, and reprocessing failed recordings.

- **Public interface**: `reclassify_transcript()`, `move_transcript_and_analysis()`,
  `find_missing_analysis()`, `generate_missing_analysis()`,
  `match_transcripts_to_audio()`, `match_audio_to_unknown_meetings()`
- **Internal data model**: match result tuples, file move plans
- **Dependencies**: S1.M2 (API for re-transcription), S1.M4 (file I/O)

### S3.M1: Processing State

JSON-backed persistent state tracking which files have been processed
and their outcome.

- **Public interface**: `load_state()`, `save_state(state)`
- **Internal data model**: `{"processed": {path: {status, category, timestamp, attempts, error}}}`
- **Dependencies**: filesystem (`~/.meeting_transcriber_state.json`)

### S3.M2: Transcript Index

In-memory deduplication index built from existing transcript files to
prevent re-processing.

- **Public interface**: `build_transcript_index(folders)` → dict keyed by
  timestamp
- **Internal data model**: `{timestamp_key: transcript_path}`
- **Dependencies**: S1.M4 output directories

> **Quality coverage checkpoint (L2)**:
> S1.M1: Filters `.icloud`, `.tmp`, hidden files; stability check (2s).
> S1.M2: 3-tier error classification; retry with backoff [10,30,60]s
> transcription, [60,180,300]s analysis.
> S1.M3: Falls back to DEFAULT on parse failure.
> S1.M4: Creates directories on demand; handles filename collisions
> with `(2)`, `(3)` suffix.
> S2.M1: Rate-limited (90s between files, 5 files/cycle cap).
> S2.M3: All operations support `--dry-run`.
> S3.M1: Atomic write pattern (write → verify).
> S3.M2: Rebuilt from filesystem each startup (self-healing).

---

## Level 3 — Components

### S1.M1.C1: Folder Scanner
- **File(s)**: `auto_transcribe.py` — `discover_recent_folders()`
- **Interface contract**: `(watch_folder, days_back)` → list of
  date folder paths sorted chronologically
- **Error taxonomy**: `FileNotFoundError` if watch folder missing;
  silently skips non-date folders

### S1.M1.C2: File Filter
- **File(s)**: `auto_transcribe.py` — `discover_audio_files()`,
  `is_file_stable()`
- **Interface contract**: `(watch_folder, state, transcript_index)` →
  list of unprocessed, stable `.m4a` file paths
- **Error taxonomy**: `OSError` on inaccessible files; logs and skips

### S1.M1.C3: Timestamp Extractor
- **File(s)**: `ondemand_transcribe.py` — `get_audio_timestamp()`;
  also duplicated in utility scripts (`utils/`)
- **Interface contract**: `(audio_path)` → `datetime` via 3-strategy
  fallback: macOS `mdls` → directory/filename parse → `os.path.getctime`
- **Error taxonomy**: returns `None` if all strategies fail

### S1.M2.C1: File Uploader
- **File(s)**: `auto_transcribe.py` — `upload_to_gemini()`
- **Interface contract**: `(file_path)` → Gemini `File` object (active
  state); polls until processing complete
- **Error taxonomy**: `FatalAPIError` (auth), transient API errors
  (retried by caller)

### S1.M2.C2: Transcription Caller
- **File(s)**: `auto_transcribe.py` — `transcribe_with_retry()`
- **Interface contract**: `(audio_file)` → raw response text from
  Flash model; retries 3× with [10,30,60]s backoff
- **Error taxonomy**: `FatalAPIError` (stops service),
  `PermanentFileError` (skip file), transient (retry then raise)

### S1.M2.C3: Analysis Caller
- **File(s)**: `auto_transcribe.py` — `analyze_with_retry()`
- **Interface contract**: `(transcript_content)` → analysis text from
  Pro model; retries 3× with [60,180,300]s backoff
- **Error taxonomy**: same as C2 but with pro-tier quota recovery
  timing; on exhaustion returns best partial result or raises

### S1.M3.C1: Response Parser
- **File(s)**: `auto_transcribe.py` — `parse_transcription_response()`
- **Interface contract**: `(response_text)` →
  `(category, filename, transcript)` tuple; expects
  `CATEGORY: ...\nFILENAME: ...\n---TRANSCRIPT---\n...`
- **Error taxonomy**: returns `("DEFAULT", "Unknown Meeting", raw_text)`
  on parse failure

### S1.M4.C1: Markdown Writer
- **File(s)**: `auto_transcribe.py` — `save_transcript()`,
  `save_analysis()`
- **Interface contract**: `(category, filename, content)` → writes
  `.md` file to `FOLDERS[category]/transcripts/` or `/analysis/`;
  creates directories if missing
- **Error taxonomy**: `OSError` on write failure; `KeyError` if
  category unknown (falls back to DEFAULT)

### S3.M1.C1: State File Handler
- **File(s)**: `auto_transcribe.py` — `load_state()`, `save_state()`
- **Interface contract**: `load_state()` → dict (empty dict if file
  missing or corrupt); `save_state(state)` → writes JSON atomically
- **Error taxonomy**: `json.JSONDecodeError` → returns empty state
  (self-healing); `OSError` on write → logged, not fatal

### S2.M3.C1: Reclassifier
- **File(s)**: `reclassify_and_fix.py` — `reclassify_transcript()`,
  `move_transcript_and_analysis()`
- **Interface contract**: `(transcript_path, dry_run)` → re-calls
  Flash model for classification, moves files to new category folder
- **Error taxonomy**: API errors (same as S1.M2); file collision
  handled with `(2)` suffix

### S2.M3.C2: Filename Fixer
- **File(s)**: `utils/fix_filenames.py` (not committed) —
  `match_transcripts_to_audio()`, `generate_new_filename()`
- **Interface contract**: scans transcript files, matches to audio
  by chronological order, renames with correct timestamp
- **Error taxonomy**: unmatched files logged; `--dry-run` default

### S2.M3.C3: Unknown Meeting Resolver
- **File(s)**: `utils/fix_unknown_meetings.py` (not committed) —
  `match_audio_to_unknown_meetings()`, `reprocess_and_cleanup()`
- **Interface contract**: finds "Unknown Meeting" files, matches to
  audio via log file or ±2min timestamp window, reprocesses
- **Error taxonomy**: unmatched files reported; old files only deleted
  after verifying new files exist

> **Quality coverage checkpoint (L3)**:
> S1.M1.C3: Timestamp extraction duplicated across 3 files — candidate
> for extraction into shared module.
> S1.M2.C1-C3: Retry/backoff logic fully specified per component.
> S1.M3.C1: Fallback to DEFAULT prevents data loss on parse failure.
> S3.M1.C1: Self-healing on corrupt state (returns empty dict).
> S2.M3.C1-C3: All support `--dry-run`; destructive ops verified first.

---

## Dependency Map

```
                    ┌──────────────────────────────────────────┐
                    │           S2: Service Orchestration       │
                    │                                          │
                    │  S2.M1 Daemon Loop                       │
                    │  S2.M2 CLI Entry Points                  │
                    │  S2.M3 Maintenance & Repair              │
                    └──────────┬───────────────────────────────┘
                               │ invokes
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                  S1: Transcription Pipeline                   │
│                                                              │
│  S1.M1 Audio       S1.M2 Gemini API    S1.M3 Classification │
│  Discovery    ───► Integration     ───► & Parsing            │
│                                              │               │
│                                              ▼               │
│                                    S1.M4 File Output         │
└──────────────────────┬───────────────────────────────────────┘
                       │ reads/writes
                       ▼
              ┌─────────────────┐
              │ S3: State &     │
              │ Persistence     │
              │                 │
              │ S3.M1 State     │
              │ S3.M2 Index     │
              └─────────────────┘
```

---

## Notes

- **Level 4 (Atomic Tasks)** are generated from this decomposition and tracked in `TASKS.md`, not in this file
- Update this document when modules are added, split, or merged
- Cross-reference ADRs in `docs/adr/` for technology decisions underlying this architecture
- **Known debt**: S1.M1.C3 (Timestamp Extractor) is duplicated across `ondemand_transcribe.py` and utility scripts — should be extracted into a shared module
- **Known debt**: Code duplication between `auto_transcribe.py` and `ondemand_transcribe.py` — shared functions are copy-pasted rather than imported from a common module
