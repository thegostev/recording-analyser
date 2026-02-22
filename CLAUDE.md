# RecordingAnalyser

Automated audio transcription and analysis service using Gemini API, running as a macOS launchd daemon with output to Obsidian vaults.

## Constitution

See `.specify/memory/constitution.md` — use `/speckit.constitution` to create or update.

The constitution contains immutable project principles that no atomic task can violate. It is versioned and governed by Spec Kit. All downstream artifacts (specs, plans, tasks) must comply.

## Architecture

Shared pipeline module (`pipeline.py`) plus thin entry points: daemon (`auto_transcribe.py`), CLI catchup (`ondemand_transcribe.py`), and maintenance (`reclassify_and_fix.py`). Audio files are discovered from iCloud-synced Just Press Record folders, processed through Gemini Flash (transcription + classification) and Gemini Pro (analysis), then output as Markdown to categorized Obsidian vault folders.

- See `ARCHITECTURE.md` for WBS decomposition (S1-S3, modules, components)
- See `docs/adr/` for architectural decisions affecting this project

## Key patterns

- **Two-stage pipeline**: Flash model transcribes + classifies, then Pro model analyzes. Stages are independent — transcript is saved before analysis starts, so analysis failure doesn't lose transcript
- **State-backed deduplication**: `~/.meeting_transcriber_state.json` tracks every processed file by path. On startup, a transcript index is also rebuilt from filesystem to catch files processed outside the state file
- **Category routing**: categories are defined in `config.yaml` and mapped to Obsidian vault paths via the `FOLDERS` dict. Classification is keyword-based in the Gemini prompt
- **3-tier error handling**: fatal (auth → stop service), permanent (bad file → skip forever), transient (quota → retry with backoff)

## Commands

```bash
# Development
source venv/bin/activate
python auto_transcribe.py                        # run daemon in foreground
python ondemand_transcribe.py --catchup --dry-run # preview catchup

# Production / Service
launchctl load ~/Library/LaunchAgents/com.meetingtranscriber.plist
launchctl unload ~/Library/LaunchAgents/com.meetingtranscriber.plist
launchctl list | grep meetingtranscriber

# Logs
tail -f /tmp/transcriber.out                     # stdout
tail -f /tmp/transcriber.err                     # stderr

# Maintenance
./run_transcriber.sh fix-analysis --dry-run      # preview missing analysis
./run_transcriber.sh fix-categories --dry-run    # preview reclassification
./run_transcriber.sh fix-all --dry-run           # preview all fixes

# Testing
python -m pytest tests/ -v
```

## Known technical debt

- **Minimal test suite** — infrastructure smoke tests exist but no tests for core pipeline functions (see RA-003, RA-004, RA-005)

## Constraints and gotchas

- **Pro-tier API quota**: Gemini Pro has strict rate limits. Inter-file delay is 90s, analysis retries use [60, 180, 300]s backoff. Never reduce these without testing
- **iCloud sync latency**: output paths are iCloud-synced. The 2-second stability check (`is_file_stable`) may be insufficient on slow connections. Burst writes can cause sync conflicts
- **Service runs 24/7**: any change to `auto_transcribe.py` or the launchd plist requires stopping the service first. Test changes with `ondemand_transcribe.py --dry-run` before modifying the daemon
- **5 files per cycle cap**: `MAX_FILES_PER_CYCLE=5` prevents API stampede. Large backlogs take multiple scan cycles (30s apart) to clear
- **State file corruption**: if `~/.meeting_transcriber_state.json` is corrupted, `load_state()` returns empty dict and all files get re-processed. The transcript index deduplication prevents duplicate output files

## Quality coverage matrix

| Domain | Measurable target |
|---|---|
| Functional correctness | Transcripts match audio content; <5% files classified as DEFAULT (miscategorized) |
| Error handling | Zero data loss from transient API failures; all errors classified into 3 tiers with appropriate action |
| Reliability | Service auto-restarts within 30s of crash via launchd; state file survives unclean shutdown |
| Observability | Every file processing attempt produces ≥1 timestamped log entry; failures include file path + error context |
| Data integrity | Zero silently dropped recordings per month; every audio file is either "complete" or "failed_*" in state |
| Configuration | All timing constants (scan interval, retry backoff, delays) defined as module-level constants, not magic numbers |
| Maintainability | Single-responsibility modules per ARCHITECTURE.md; no function exceeds ~50 lines |
| Testing | Core pipeline functions have ≥1 happy-path + ≥1 error-case test; all maintenance scripts support `--dry-run` |

## File reference when stuck

- **Processing pipeline**: `pipeline.py` — `process_audio()` orchestrates the full flow, all shared functions live here
- **Error classification**: `pipeline.py` — `classify_api_error()`, `FatalAPIError`, `PermanentFileError`
- **State management**: `pipeline.py` — `load_state()`, `save_state()`, `build_transcript_index()`
- **Gemini API**: `pipeline.py` — `configure_gemini()`, `upload_to_gemini()`, `transcribe_with_retry()`, `analyze_with_retry()`
- **Category definitions**: `config.yaml` — `folders` mapping; loaded via `config.py`
- **Prompt engineering**: `config.yaml` — `transcription_prompt` and `analysis_prompt`; loaded via `config.py`
- **State format**: `~/.meeting_transcriber_state.json` — JSON dict keyed by file path
- **Daemon loop**: `auto_transcribe.py` — `discover_audio_files()`, `run_scan_cycle()`, `main()`
- **CLI options**: `ondemand_transcribe.py` — argparse at bottom of file
- **Maintenance ops**: `reclassify_and_fix.py` — `--generate-missing-analysis` and `--reclassify`
- **WBS decomposition**: `ARCHITECTURE.md` — L0-L3 module/component mapping
