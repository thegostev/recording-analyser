# MeetingTranscriber

Automated transcription and analysis of audio recordings using Google's Gemini API. Runs as a macOS background service, processes audio from [Just Press Record](https://www.openplanetsoftware.com/just-press-record/), classifies content into categories, and outputs structured Markdown notes to Obsidian vaults.

## What it does

1. Watches a folder for new `.m4a` audio recordings
2. Transcribes audio using Gemini Flash (fast transcription + speaker diarization)
3. Classifies each recording into a configurable category based on content keywords
4. Analyzes the transcript using Gemini Pro (extracts insights, decisions, action items)
5. Saves transcript and analysis as Markdown to category-specific folders

## Prerequisites

- macOS (uses `mdls` for audio metadata, `launchd` for service management)
- Python 3.9+
- Google Gemini API key ([get one here](https://aistudio.google.com/apikey))
- [Just Press Record](https://www.openplanetsoftware.com/just-press-record/) (or any app that saves `.m4a` files in date-based subfolders)

## Setup

```bash
git clone <repo-url> MeetingTranscriber
cd MeetingTranscriber

python3 -m venv venv
source venv/bin/activate
pip install -e .

cp config.example.yaml config.yaml
```

Edit `config.yaml`:
- Add your Gemini API key (or set `GEMINI_API_KEY` environment variable)
- Set `watch_folder` to where your audio files are saved
- Define your categories and output folder paths
- Customize the transcription and analysis prompts

## Running

### As a background service (recommended)

Create a launchd plist at `~/Library/LaunchAgents/com.meetingtranscriber.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.meetingtranscriber</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/MeetingTranscriber/venv/bin/python</string>
        <string>/path/to/MeetingTranscriber/auto_transcribe.py</string>
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
launchctl load ~/Library/LaunchAgents/com.meetingtranscriber.plist    # start
launchctl unload ~/Library/LaunchAgents/com.meetingtranscriber.plist  # stop
launchctl list | grep meetingtranscriber                              # status
tail -f /tmp/transcriber.out                                          # logs
```

### In the foreground

```bash
source venv/bin/activate
python auto_transcribe.py
```

### On-demand processing

```bash
python ondemand_transcribe.py --catchup --dry-run           # preview last 7 days
python ondemand_transcribe.py --catchup                     # process last 7 days
python ondemand_transcribe.py --catchup 14                  # process last 14 days
python ondemand_transcribe.py --catchup --reprocess-partial # regenerate missing analysis
```

### Shell wrapper

```bash
./run_transcriber.sh start           # launch in background
./run_transcriber.sh stop            # stop
./run_transcriber.sh status          # check status
./run_transcriber.sh catchup         # process last 7 days
./run_transcriber.sh catchup-preview # dry run
```

## Maintenance

Fix misclassified or incomplete files:

```bash
./run_transcriber.sh fix-analysis --dry-run     # preview missing analysis generation
./run_transcriber.sh fix-analysis               # generate missing analysis files
./run_transcriber.sh fix-categories --dry-run   # preview reclassification
./run_transcriber.sh fix-categories             # reclassify and move to correct folders
./run_transcriber.sh fix-all --dry-run          # preview all fixes
```

## Configuration

All settings live in `config.yaml`. See `config.example.yaml` for the full structure.

| Setting | Description |
|---|---|
| `api_key` | Gemini API key (or use `GEMINI_API_KEY` env var) |
| `watch_folder` | Where audio files are saved (date-based subfolders: `YYYY-MM-DD/*.m4a`) |
| `folders` | Map of category names to output paths. Each gets `transcripts/` and `analysis/` subdirectories |
| `transcription_prompt` | Prompt sent with audio to Flash model. Define your categories and keywords here |
| `analysis_prompt` | Prompt sent with transcript text to Pro model |
| `scan_interval` | Seconds between scan cycles (default: 30) |
| `delay_between_files` | Seconds between processing files to avoid API rate limits (default: 90) |

## How classification works

The Flash model receives your audio file along with the transcription prompt. The prompt defines your categories and associated keywords. The model transcribes the audio, matches content against the keywords, and outputs a `CATEGORY:` tag. The tool uses this tag to route the output files to the correct folder.

To customize: edit the category list and keywords in the `transcription_prompt` section of your `config.yaml`. The category names must match the keys in your `folders` mapping.

## Processing pipeline

```
Audio file (.m4a)
    |
    v
[Gemini Flash] --> Transcript + Category + Filename
    |
    v
Save transcript to <category>/transcripts/
    |
    v
[Gemini Pro] --> Analysis (insights, decisions, action items)
    |
    v
Save analysis to <category>/analysis/
```

Key design choices:
- Transcript is saved before analysis starts — analysis failure never loses the transcript
- 3-tier error handling: fatal (bad API key stops service), permanent (bad file skipped), transient (quota exhausted retries with backoff)
- Duplicate prevention via timestamp-based deduplication
- State tracked in `~/.meeting_transcriber_state.json`

## Project structure

```
auto_transcribe.py     Long-running daemon service
ondemand_transcribe.py Manual/batch processing CLI
reclassify_and_fix.py  Maintenance: fix missing analysis, reclassify files
run_transcriber.sh     Shell wrapper for common operations
config.py              Configuration loader
config.example.yaml    Configuration template (copy to config.yaml)
pyproject.toml         Python project metadata and dependencies
tests/                 Test suite
Post-mortems/          Incident documentation
ARCHITECTURE.md        System decomposition (WBS)
CLAUDE.md              AI assistant conventions for this project
```

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
```
