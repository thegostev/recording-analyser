<img width="2816" height="1536" alt="Gemini_Generated_Image_dlm2otdlm2otdlm2" src="https://github.com/user-attachments/assets/a41ea4c2-a7a0-4944-8df5-cd98a9d35e61" />

# RecordingAnalyser

[![Build Status](https://img.shields.io/github/actions/workflow/status/thegostev/recording-analyser/tests.yml?logo=github&label=build)](https://github.com/thegostev/recording-analyser/actions)
[![License: MIT](https://img.shields.io/github/license/thegostev/recording-analyser?color=green)](https://github.com/thegostev/recording-analyser/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.9+-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![macOS](https://img.shields.io/badge/platform-macOS-000000?logo=apple&logoColor=white)](https://support.apple.com/guide/launchd)
[![Obsidian](https://img.shields.io/badge/output-Obsidian%20Markdown-7C3AED?logo=obsidian&logoColor=white)](https://obsidian.md)

Automated transcription and analysis of audio recordings. Runs as a macOS background service, picks up recordings from [Just Press Record](https://www.openplanetsoftware.com/just-press-record/), transcribes locally with Whisper, classifies and analyses with a local Ollama model (or Gemini Pro), and outputs structured Markdown notes to Obsidian vaults.

## What it does

1. Watches a folder for new `.m4a` audio recordings
2. Transcribes audio locally using Whisper (verbatim, timestamped)
3. Classifies each recording into a category and generates a filename via the analysis model
4. Analyzes the transcript (insights, decisions, action items) — fully local with Ollama
5. Saves transcript and analysis as Markdown to category-specific Obsidian vault folders

State is tracked in `~/.meeting_transcriber_state.json` — processed files are never reprocessed.

## Prerequisites

- macOS (uses `mdls` for file metadata, `launchd` for service management)
- Python 3.9+
- [Ollama](https://ollama.com) running locally with a model pulled — e.g. `ollama pull qwen3.5:9b`
- [Just Press Record](https://www.openplanetsoftware.com/just-press-record/) or any app that saves `.m4a` files into `YYYY-MM-DD/` subfolders

Optional: Google Gemini API key (only if you set `analysis_provider: gemini` in config).

## Setup

```bash
git clone <repo-url> RecordingAnalyser
cd RecordingAnalyser

python3 -m venv venv
source venv/bin/activate
pip install -e .

cp config.example.yaml config.yaml
# Edit config.yaml:
#   - set watch_folder (where your .m4a files land)
#   - set folders (category name → output path)
#   - set ollama_model to match: ollama list
```

No API key is needed in Ollama mode. The service will start without any environment variables set.

## Running

### As a background service (recommended for daily use)

Use `run_transcriber.sh` — it manages a PID file and logs to `transcriber.log` in the project folder.

First, navigate to the project and activate the venv:

```bash
cd "/Users/Necessaire/Documents/Koding - Obsidian/1 - Code/RecordingAnalyser"
source venv/bin/activate
```

Then use the shell wrapper (the venv activation is only needed once per terminal session):

```bash
./run_transcriber.sh start     # launch in background
./run_transcriber.sh stop      # stop
./run_transcriber.sh restart   # stop + start
./run_transcriber.sh status    # check if running + last 5 log lines
./run_transcriber.sh logs      # tail the log (Ctrl+C to exit)
```

After starting, follow the log to watch it work in real time:

```bash
./run_transcriber.sh logs
```

Example output when the service is processing files:

```
✅ Ollama configured (qwen3.5:9b @ http://localhost:11434, thinking=False)
🎙️  Loading Whisper model (large-v3)...
✅ Whisper model loaded
============================================================
🎙️  Auto-Transcription Service
============================================================
Scan interval: 30s | Delay between files: 5s
📚 Loaded state: 357 completed, 0 permanently failed
🔄 Starting scan loop (every 30s)...

[Cycle 1] 06:56:07 - Found 5 file(s) to process

📂 [1/5] 2026-04-19/11-11-03.m4a
   🧠 Transcribing locally (Whisper)...
   ✅ Transcript saved: .../transcripts/26-04-19 11.11 - Team Sync - Planning.md
   📊 Analyzing transcript [Ollama] (attempt 1/4)...
   ✅ Analysis succeeded via Ollama
   ✅ Analysis saved: .../analysis/26-04-19 11.11 - Team Sync - Planning - Analysis.md
   ⏸️  Pausing 5s before next file...

[Cycle 8] 07:03:30 - No new files
```

When idle, it logs `No new files` every 30 seconds. Press `Ctrl+C` to stop tailing (the service keeps running).

Alternatively, register as a launchd agent so it starts automatically on login:

Create `~/Library/LaunchAgents/com.necessaire.transcriber.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.necessaire.transcriber</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/RecordingAnalyser/venv/bin/python</string>
        <string>/path/to/RecordingAnalyser/auto_transcribe.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/transcriber.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/transcriber.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.necessaire.transcriber.plist    # start
launchctl unload ~/Library/LaunchAgents/com.necessaire.transcriber.plist  # stop
launchctl list | grep transcriber                                          # status
tail -f /tmp/transcriber.out                                               # logs
tail -f /tmp/transcriber.err                                               # errors
```

> If using Gemini (`analysis_provider: gemini`), add an `EnvironmentVariables` block to the plist with `GEMINI_API_KEY` — launchd daemons don't inherit your shell environment.

### In the foreground

```bash
source venv/bin/activate
python auto_transcribe.py
```

### On-demand / catchup

Process recordings that the daemon missed (e.g. after downtime):

```bash
python ondemand_transcribe.py --catchup --dry-run       # preview last 7 days
python ondemand_transcribe.py --catchup                 # process last 7 days
python ondemand_transcribe.py --catchup 14              # process last 14 days
python ondemand_transcribe.py --catchup --reprocess-partial  # regenerate missing analysis only
```

Or via the shell wrapper:

```bash
./run_transcriber.sh catchup           # last 7 days
./run_transcriber.sh catchup 14        # last 14 days
./run_transcriber.sh catchup-preview   # dry-run preview
./run_transcriber.sh reprocess         # regenerate missing analysis
```

## Maintenance

Fix analysis or classification issues without reprocessing audio:

```bash
# Generate analysis files that are missing (transcript exists but no analysis)
./run_transcriber.sh fix-analysis
./run_transcriber.sh fix-analysis --dry-run    # preview first

# Reclassify "Unknown Meeting" files and move them to the correct folder
./run_transcriber.sh fix-categories
./run_transcriber.sh fix-categories --dry-run

# Both at once
./run_transcriber.sh fix-all
./run_transcriber.sh fix-all --dry-run

# Direct: same operations with verbose output
python reclassify_and_fix.py --generate-missing-analysis --verbose
python reclassify_and_fix.py --reclassify --dry-run --verbose
python reclassify_and_fix.py --generate-missing-analysis --reclassify
```

## Configuration

All settings live in `config.yaml` (gitignored). `config.example.yaml` is the template.

### Analysis provider

| Setting | Value | Description |
|---|---|---|
| `analysis_provider` | `ollama` (default) | Use local Ollama model — no API key needed |
| `analysis_provider` | `gemini` | Use Gemini Pro cloud model — requires `GEMINI_API_KEY` env var |

### Ollama settings

| Setting | Default | Description |
|---|---|---|
| `ollama_model` | `qwen3.5:9b` | Model tag as shown in `ollama list` |
| `ollama_host` | `http://localhost:11434` | Ollama server address |
| `ollama_thinking` | `false` | Enable extended reasoning (adds ~2–3 min per file) |

### Service behavior

| Setting | Default | Description |
|---|---|---|
| `watch_folder` | — | Where audio files land (`YYYY-MM-DD/*.m4a` subfolders) |
| `folders` | — | Category name → output path. Gets `transcripts/` and `analysis/` subdirs |
| `state_file` | `~/.meeting_transcriber_state.json` | Tracks processed files |
| `failed_analysis_log` | `failed_analysis.log` | Log for files where analysis failed |
| `scan_interval` | `30` | Seconds between scan cycles |
| `scan_days_back` | `7` | How many days back to scan |
| `delay_between_files` | `5` | Seconds between files (no rate limiting needed with Ollama) |
| `max_files_per_cycle` | `5` | Files processed per scan cycle |
| `max_retries` | `3` | Retry attempts per processing stage |
| `retry_backoff` | `[10, 30, 60]` | Seconds between transcription retries |
| `analysis_retry_backoff` | `[60, 180, 300]` | Seconds between analysis retries |
| `api_timeout` | `300` | API call timeout in seconds |

### Classification

The analysis model receives the transcript and `analysis_prompt`. The prompt defines your categories and associated keywords. The model outputs a `CATEGORY:` tag that routes output files to the matching key in `folders`.

To add or change categories: edit the category list in `analysis_prompt` and add the matching key to `folders`. Category names must match exactly.

## Processing pipeline

```
Audio file (.m4a)
    │
    ▼
[Whisper] ── Timestamped transcript
    │
    ▼
[Ollama / Gemini Pro] ── CATEGORY + FILENAME + Analysis
    │
    ├── Save to <category>/transcripts/<date> - <filename>.md
    │
    └── Save to <category>/analysis/<date> - <filename> - Analysis.md
```

Key behaviours:
- Transcript is saved before analysis runs — analysis failure never loses the transcript
- Three error tiers: **fatal** (bad API key → service stops), **permanent** (bad file → skipped forever), **transient** (connection error → retries with backoff)
- All Ollama errors are transient — if the server is temporarily down, the file retries on the next cycle
- Duplicate prevention via state file — files are never reprocessed

## Project structure

```
auto_transcribe.py     Long-running daemon (scan loop)
ondemand_transcribe.py Manual/batch processing CLI
reclassify_and_fix.py  Maintenance: fix missing analysis, reclassify files
pipeline.py            Shared pipeline (Whisper, Ollama/Gemini, parsing, file I/O, state)
run_transcriber.sh     Shell wrapper for all common operations
config.py              Configuration loader (reads config.yaml, injects env vars)
config.yaml            Active configuration (gitignored)
config.example.yaml    Configuration template
pyproject.toml         Dependencies and project metadata
tests/                 Test suite
ARCHITECTURE.md        System decomposition (WBS, L0–L3)
docs/adr/              Architectural decision records
```

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
python -m pytest tests/ -v -m "not slow"   # skip slow tests
```
