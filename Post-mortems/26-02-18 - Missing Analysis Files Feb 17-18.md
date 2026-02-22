26-02-18 / tags: post-mortem, transcriber, bug

---

# Post-Mortem: Missing Meeting Analysis — Feb 17–18, 2026

## Summary

User observed far fewer transcribed files than expected across the Minnesotere and Mus vaults for Feb 17–18. JustPress had 6 recordings on Feb 17 and 7 on Feb 18. Vaults appeared to show only 1–3 files.

**Verdict:** No transcripts are missing. All 13 recordings were transcribed (15 transcript files total). The actual failure: 13 of 15 *analysis* files were silently dropped due to Gemini API rate limiting during burst processing. The failure was invisible because state tracking marked files complete even when only the transcript saved.

---

## What Actually Exists

| Layer | Feb 17 | Feb 18 | Status |
|---|---|---|---|
| Recordings | 6 files | 7 files | ✅ All present |
| Transcripts (Minn) | 5 | 4 | ✅ All created |
| Transcripts (Mus) | 0 | 6 | ✅ All created |
| **Analysis (Minn)** | **1** | **0** | ❌ 7 missing |
| **Analysis (Mus)** | n/a | **1** | ❌ 5 missing |

**Total:** 13 recordings → 15 transcripts → 2 analyses (expected 15)

---

## Timeline

- **Feb 17 08:32–17:21**: 6 recordings captured. First 3 processed during the day; last 3 processed Feb 18 morning due to file size (58 MB largest)
- **Feb 18 08:01–18:36**: 7 recordings captured. Processing ran through day in batches of 5
- **Feb 18 ~15:00**: Gemini pro-tier quota exhausted. Analysis API returns `503 high demand` and `504 Deadline Exceeded` on all subsequent calls
- **Feb 18 18:41**: Last state file update. 13 meetings marked "complete" without analysis

---

## Root Causes

### RC-1: API rate limiting (primary)
Transcription uses `gemini-3-flash-preview` (high quota tier). Analysis uses `gemini-3-pro-preview` (low quota tier). Processing 13 meetings across 2 days exceeded the pro-tier quota. All errors after the 2nd analysis were:
- `503 This model is currently experiencing high demand`
- `504 Deadline Exceeded`
- `499 The operation was cancelled`

The retry backoff `[10s, 30s, 60s]` is insufficient — Gemini's quota recovery window is ~1–5 minutes.

### RC-2: Silent failure (contributing)
`~/.meeting_transcriber_state.json` marks a file `status: complete` after transcript saves, regardless of whether analysis succeeded. Analysis failure is only logged to `/tmp/transcriber.out`, which is not persistent and not surfaced in any dashboard. The backlog was completely invisible.

### RC-3: One duplicate transcript
`Minnesotere/3 - Meetings/transcripts/` has two variants of the Feb 17 13:00 meeting:
- `26-02-17 13.00 - Board Sync - Jenkins Migration, ECR, Infrastructure.md`
- `26-02-17 13.00 - [Infrastructure Sync] - [Board Review, Jenkins Migration, Cloud Infrastructure].md`

Caused by: State file de-duplication key uses `YY-MM-DD HH.MM` minute-level precision. A retry within the same minute produced a second transcript before the first was indexed.

---

## What Did NOT Happen

- No recordings were lost or skipped
- No files went undetected by the watch service
- No routing errors (Minnesotere/Mus classification worked correctly)
- No permanent API auth failures
- No iCloud sync issues

---

## Immediate Recovery

Run `--reprocess-partial` to regenerate 13 missing analyses:

```bash
cd "6 - Code/RecordingAnalyser"
./venv/bin/python3 ondemand_transcribe.py --reprocess-partial
```

This mode detects transcripts without a matching analysis file and resubmits only the analysis stage.

Also manually remove the duplicate in Minnesotere vault:
- **Keep:** `26-02-17 13.00 - [Infrastructure Sync] - [Board Review, Jenkins Migration, Cloud Infrastructure].md`
- **Delete:** `26-02-17 13.00 - Board Sync - Jenkins Migration, ECR, Infrastructure.md`

---

## Structural Fixes Required

**File:** `6 - Code/RecordingAnalyser/auto_transcribe.py`

| Fix | Change | Why |
|-----|--------|-----|
| Increase analysis retry delays | `[10, 30, 60]s` → `[60, 180, 300]s` | Pro-tier quota window is 1–5 min, not seconds |
| Rate-limit burst analysis | Add 90s sleep between analysis API calls (currently 15s between all operations) | Prevents quota exhaustion during back-to-back meeting days |
| Persistent failure log | Write `NEEDS_ANALYSIS: <filename>` to a durable `failed_analysis.log` on each analysis failure | Makes the gap observable without digging through /tmp logs |
| Fix duplicate transcript prevention | Before writing transcript, check output folder for existing file with same `YY-MM-DD HH.MM` prefix (not just state.json) | State.json isn't read fast enough during retries within the same minute |

---

## Verification After Fix

After `--reprocess-partial` completes:
1. `Minn - Obsidian/3 - Meetings/analysis/` → expect 8 files (4 Feb 17 + 4 Feb 18)
2. `Mus - Obsidian/* Meetings/analysis/` → expect 6 files (all Feb 18)
3. `~/.meeting_transcriber_state.json` → all Feb 17–18 entries show `status: complete`
4. No new duplicates in transcript folders

---

## References

- Auto transcriber: [[6 - Code/RecordingAnalyser/auto_transcribe.py]]
- On-demand tool: [[6 - Code/RecordingAnalyser/ondemand_transcribe.py]]
- State file: `~/.meeting_transcriber_state.json`
- Service log: `/tmp/transcriber.out`
