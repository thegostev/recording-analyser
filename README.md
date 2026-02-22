# RecordingAnalyser

[![Build Status](https://img.shields.io/github/actions/workflow/status/thegostev/recording-analyser/tests.yml?logo=github&label=build)](https://github.com/thegostev/recording-analyser/actions)
[![License: MIT](https://img.shields.io/github/license/thegostev/recording-analyser?color=green)](https://github.com/thegostev/recording-analyser/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Google Gemini](https://img.shields.io/badge/API-Google%20Gemini-8E75B2?logo=googlegemini&logoColor=white)](https://ai.google.dev)
[![macOS](https://img.shields.io/badge/platform-macOS-000000?logo=apple&logoColor=white)](https://support.apple.com/guide/launchd)
[![Obsidian](https://img.shields.io/badge/output-Obsidian%20Markdown-7C3AED?logo=obsidian&logoColor=white)](https://obsidian.md)

Automated transcription and analysis of audio recordings using Google's Gemini API. Runs as a macOS background service, processes audio from [Just Press Record](https://www.openplanetsoftware.com/just-press-record/), classifies content into categories, and outputs structured Markdown notes to Obsidian vaults.

## What it does

1. Watches a folder for new `.m4a` audio recordings
2. Transcribes audio using Gemini Flash (verbatim + speaker diarization + timestamps)
3. Classifies each recording into a category based on keywords in the prompt
4. Analyzes the transcript using Gemini Pro (extracts insights, decisions, action items)
5. Saves transcript and analysis as Markdown to category-specific folders

State is tracked in `~/.meeting_transcriber_state.json` — processed files are never reprocessed.

## Prerequisites

- macOS (uses `mdls` for audio duration metadata, `launchd` for service management)
- Python 3.9+
- Google Gemini API key ([get one](https://aistudio.google.com/apikey))
- [Just Press Record](https://www.openplanetsoftware.com/just-press-record/) or any app that saves `.m4a` files in `YYYY-MM-DD/` subfolders

## Setup

```bash
git clone <repo-url> RecordingAnalyser
cd RecordingAnalyser

python3 -m venv venv
source venv/bin/activate
pip install -e .

cp config.example.yaml config.yaml
# Edit config.yaml — set watch_folder, folders, and optionally the model names
```

Set your API key in `~/.zshrc`:

```bash
export GEMINI_API_KEY="your-gemini-api-key"
```

## Running

### As a background service (recommended)

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
    <key>EnvironmentVariables</key>
    <dict>
        <key>GEMINI_API_KEY</key>
        <string>your-gemini-api-key</string>
    </dict>
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

> The `EnvironmentVariables` block in the plist is required — launchd daemons don't inherit your shell environment.

```bash
launchctl load ~/Library/LaunchAgents/com.necessaire.transcriber.plist    # start
launchctl unload ~/Library/LaunchAgents/com.necessaire.transcriber.plist  # stop
launchctl list | grep transcriber                                          # status
tail -f /tmp/transcriber.out                                               # logs
tail -f /tmp/transcriber.err                                               # errors
```

### In the foreground

```bash
source venv/bin/activate
python auto_transcribe.py
```

### On-demand / catchup

```bash
python ondemand_transcribe.py --catchup --dry-run      # preview what would run
python ondemand_transcribe.py --catchup                # process last 7 days
python ondemand_transcribe.py --catchup 14             # process last 14 days
python ondemand_transcribe.py --catchup --reprocess-partial  # regenerate missing analysis only
```

### Shell wrapper

```bash
./run_transcriber.sh start           # launch in background
./run_transcriber.sh stop            # stop
./run_transcriber.sh status          # check status
./run_transcriber.sh catchup         # process last 7 days
./run_transcriber.sh catchup-preview # dry run
```

### Maintenance

```bash
# Generate analysis for transcripts that don't have one
python reclassify_and_fix.py --generate-missing-analysis --dry-run
python reclassify_and_fix.py --generate-missing-analysis

# Re-classify "Unknown Meeting" files and move them to the correct folder
python reclassify_and_fix.py --reclassify --dry-run
python reclassify_and_fix.py --reclassify

# Both at once
python reclassify_and_fix.py --generate-missing-analysis --reclassify --dry-run
```

## Configuration

All settings live in `config.yaml` (gitignored). `config.example.yaml` is the template.

| Setting | Default | Description |
|---|---|---|
| `transcription_model` | `gemini-3-flash-preview` | Flash model for transcription + classification |
| `analysis_model` | `gemini-3-pro-preview` | Pro model for analysis |
| `watch_folder` | — | Where audio files land (date-based subfolders: `YYYY-MM-DD/*.m4a`) |
| `folders` | — | Category name → output path. Each gets `transcripts/` and `analysis/` subdirs |
| `state_file` | `~/.meeting_transcriber_state.json` | Tracks processed files |
| `failed_analysis_log` | `failed_analysis.log` | Log for files where analysis failed |
| `scan_interval` | `30` | Seconds between scan cycles |
| `scan_days_back` | `7` | How many days back to scan |
| `delay_between_files` | `90` | Seconds between files (rate limiting) |
| `max_files_per_cycle` | `5` | Files processed per scan cycle |
| `max_retries` | `3` | API retry attempts |
| `retry_backoff` | `[10, 30, 60]` | Seconds between transcription retries |
| `analysis_retry_backoff` | `[60, 180, 300]` | Seconds between analysis retries |
| `api_timeout` | `300` | API call timeout in seconds |

### API key

The API key is **never** read from `config.yaml`. It comes from `GEMINI_API_KEY` env var only — set it in `~/.zshrc` for foreground use, and in the launchd plist `EnvironmentVariables` block for the daemon.

### Classification

The Flash model receives your audio and the `transcription_prompt`. The prompt defines your categories and associated keywords. The model outputs a `CATEGORY:` tag, which routes output files to the matching folder key in `folders`.

To add or change categories: edit the category list in `transcription_prompt` and add the matching key to `folders`. Category names must match exactly.

## Processing pipeline

```
Audio file (.m4a)
    │
    ▼
[Gemini Flash] ── Transcript + CATEGORY + FILENAME
    │
    ▼
Save to <category>/transcripts/<date>-<filename>.md
    │
    ▼
[Gemini Pro] ── Analysis (insights, decisions, action items)
    │
    ▼
Save to <category>/analysis/<date>-<filename>.md
```

Key behaviours:
- Transcript is saved before analysis runs — analysis failure never loses the transcript
- Three error tiers: **fatal** (bad API key → service stops), **permanent** (bad file → skipped), **transient** (quota → retries with backoff)
- Duplicate prevention via state file — files are never reprocessed

## Project structure

```
auto_transcribe.py     Long-running daemon
ondemand_transcribe.py Manual/batch processing CLI
reclassify_and_fix.py  Maintenance: fix missing analysis, reclassify files
pipeline.py            Shared transcription pipeline (API, parsing, file I/O, state)
run_transcriber.sh     Shell wrapper for common operations
config.py              Configuration loader (injects env vars, expands paths)
config.yaml            Active configuration (gitignored)
config.example.yaml    Configuration template
pyproject.toml         Dependencies: google-generativeai, PyYAML
tests/                 Test suite
Post-mortems/          Incident documentation
ARCHITECTURE.md        System decomposition (WBS)
```

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
python -m pytest tests/ -v -m "not slow"   # skip slow tests
```
