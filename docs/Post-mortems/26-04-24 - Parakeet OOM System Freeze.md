26-04-24 / tags: post-mortem, transcriber, parakeet, crash, system

---

# Post-Mortem: Parakeet OOM System Freeze — Apr 24, 2026

## Summary

Switching transcription to `parakeet-mlx v3` caused an out-of-memory crash on a long recording (~19–30 min). The crash bypassed Python's exception handler — a C++ level Metal GPU abort that killed the process. The OOM attempted to allocate 31.5 GB against a 14.3 GB GPU buffer limit, causing system-level memory pressure that froze macOS. On reboot, VS Code's Gemini Assist agent auto-launched as a login item, consumed excessive resources before the system was stable, kept relaunching when force-closed, and compounded the recovery difficulty.

**Verdict:** Two cascading failures — an unguarded OOM in the transcription model, and an aggressively-relaunching login item hitting the system during a vulnerable recovery window.

---

## Timeline

| Time | Event |
|---|---|
| ~17:29 | Transcriber picks up backlog of 25 files. First file: `2026-04-20/10-02-12.m4a` (19m 29s) |
| ~17:29 | Parakeet attempts to allocate single 31.5 GB Metal buffer for full audio → exceeds 14.3 GB GPU limit |
| ~17:29 | `libc++abi: terminating due to uncaught exception of type std::runtime_error: [METAL] Command buffer execution failed: Insufficient Memory` — process killed by OS |
| ~17:29–? | System freezes under memory pressure from failed 31 GB allocation attempt |
| ~18:06 | After forced restart, launchd restarts transcriber. Picks up second file (`09-32-17.m4a`, 29m 35s) |
| ~18:06 | Same OOM, this time caught as a Python exception (`[metal::malloc]` error) — falls back to mlx-whisper |
| Boot | VS Code Gemini Assist starts as login item. Begins consuming GPU/CPU resources before system is fully stable |
| Boot | User closes VS Code → Gemini Assist relaunches automatically (KeepAlive or login item behavior) |
| Boot | System overwhelmed, difficult to use; recovery required manual force-quit loop |

---

## Root Causes

### RC-1: Parakeet has no chunking — allocates full-audio buffer (primary)
Parakeet-mlx v3 allocates a single Metal GPU buffer for the entire audio before inference begins. For recordings over ~15 minutes, this buffer exceeds the 14.3 GB Metal limit (`kIOGPUCommandBufferCallbackErrorOutOfMemory`). No duration check was in place.

### RC-2: C++ crash bypasses Python exception handling (primary)
The first OOM triggered a C++ runtime abort (`libc++abi: terminating`) that cannot be caught by Python's `try/except`. The transcriber process was killed by the OS before it could log the failure or update state. launchd restarted it, which re-queued the same file — a potential crash loop.

### RC-3: VS Code Gemini Assist configured as login item (contributing)
The agent launched at boot without waiting for system stability, consumed GPU/memory resources immediately, and relaunched when closed. This compounded an already resource-constrained recovery.

---

## What Did NOT Happen

- No recordings were lost — state was rebuilt from filesystem on restart
- No data corruption to state file or transcript output
- The second OOM (29m file) was caught as a Python exception and fell back to mlx-whisper successfully
- The issue was not specific to v3 — any long recording would have triggered the same failure with parakeet-mlx

---

## Fixes Applied

### Duration gate in `pipeline.py` (deployed Apr 24)

Added `PARAKEET_MAX_SECS = 15 * 60`. In `transcribe_local()`, files longer than 15 minutes skip Parakeet entirely and route directly to mlx-whisper before any GPU allocation is attempted:

```
⏭️  Recording too long for Parakeet (19m 29s > 15m), using mlx-whisper...
```

This prevents the C++ abort entirely — the OOM never occurs.

**File changed:** `pipeline.py` — `PARAKEET_MAX_SECS` constant + duration gate in `transcribe_local()`

---

## Outstanding Actions

| Action | Owner | Why |
|---|---|---|
| Remove VS Code Gemini Assist from Login Items | Human | Prevents resource contention at boot; close-to-relaunch behavior is unsafe on a personal machine |
| Consider reducing `scan_days_back` during backlog catch-up | Human | 25-file backlog queued up due to multiple service restarts; large backlogs amplify any per-file crash |
| Evaluate Parakeet chunking support | Future | If parakeet-mlx adds native chunking, the 15-min threshold can be relaxed or removed |

---

## Verification

After fix deployed, first affected file processed correctly:

```
📂 [1/5] 2026-04-20/10-02-12.m4a
   ⏭️  Recording too long for Parakeet (19m 29s > 15m), using mlx-whisper...
   🔄 Loading mlx-whisper fallback (mlx-community/whisper-large-v3-turbo)...
   ✅ [processing in progress]
```

No `libc++abi: terminating` in logs. Service stable across cycles.

---

## References

- Pipeline: [[1 - Code/RecordingAnalyser/pipeline.py]] — `transcribe_local()`, `PARAKEET_MAX_SECS`
- Transcriber log: `1 - Code/RecordingAnalyser/transcriber.log`
- State file: `~/.meeting_transcriber_state.json`
- Related: Parakeet model swap — `mlx-community/parakeet-tdt-0.6b-v3`
