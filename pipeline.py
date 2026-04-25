"""Shared transcription pipeline: API integration, parsing, file I/O, state management.

All entry points (daemon, on-demand CLI, maintenance CLI) import from here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

from faster_whisper import WhisperModel
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

try:
    import ollama as _ollama_lib
    import httpx as _httpx
    _OLLAMA_AVAILABLE = True
except ImportError:
    _httpx = None
    _OLLAMA_AVAILABLE = False

try:
    import mlx_whisper as _mlx_whisper
    _MLX_AVAILABLE = True
except ImportError:
    _MLX_AVAILABLE = False

try:
    import parakeet_mlx as _parakeet_mlx
    _PARAKEET_AVAILABLE = True
except ImportError:
    _PARAKEET_AVAILABLE = False

from config import (
    ANALYSIS_PROMPT,
    ANALYSIS_PROVIDER,
    ANALYSIS_RETRY_BACKOFF,
    API_KEY,
    API_TIMEOUT,
    FAILED_ANALYSIS_LOG,
    FOLDERS,
    MAX_RETRIES,
    OLLAMA_HOST,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_THINKING,
    OLLAMA_TIMEOUT,
    RETRY_BACKOFF,
    STATE_FILE,
    TRANSCRIPTION_MODEL,
    TRANSCRIPTION_PROMPT,
    WHISPER_BACKEND,
    WHISPER_COMPUTE_TYPE,
    WHISPER_DEVICE,
    WHISPER_FALLBACK_MODEL,
    WHISPER_MODEL,
)

# Shared timestamp format for filenames: "YY-MM-DD HH.MM"
TIMESTAMP_FORMAT = "%y-%m-%d %H.%M"

# Parakeet allocates one Metal buffer for the full audio — files longer than this
# exceed the GPU's 14 GB limit and cause an uncatchable C++ crash.
PARAKEET_MAX_SECS: int = 15 * 60

_ollama_client = None
_whisper_model: WhisperModel | None = None
_parakeet_model = None
_mlx_fallback_loaded: bool = False
_gemini_client: genai.Client | None = None


def configure_whisper():
    """Load transcription model into memory (idempotent — safe to call multiple times)."""
    global _whisper_model, _parakeet_model

    if WHISPER_BACKEND == "parakeet":
        if _parakeet_model is not None:
            return
        if not _PARAKEET_AVAILABLE:
            raise RuntimeError("parakeet-mlx not installed — run: pip install parakeet-mlx")
        print(f"🎙️  Loading Parakeet model ({WHISPER_MODEL})...", flush=True)
        _parakeet_model = _parakeet_mlx.from_pretrained(WHISPER_MODEL)
        print("✅ Parakeet model loaded (Apple Silicon GPU)", flush=True)
    elif WHISPER_BACKEND == "mlx":
        if _whisper_model is not None:
            return
        if not _MLX_AVAILABLE:
            raise RuntimeError("mlx-whisper not installed — run: pip install mlx-whisper")
        print(f"🎙️  Loading Whisper model via MLX ({WHISPER_MODEL})...", flush=True)
        import numpy as np
        _mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32), path_or_hf_repo=WHISPER_MODEL, verbose=False)
        _whisper_model = True  # sentinel — actual inference uses mlx_whisper.transcribe() directly
        print("✅ Whisper model loaded (MLX — Apple Silicon GPU)", flush=True)
    else:
        if _whisper_model is not None:
            return
        print(f"🎙️  Loading Whisper model ({WHISPER_MODEL})...", flush=True)
        _whisper_model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
        print("✅ Whisper model loaded", flush=True)


def _transcribe_with_mlx_fallback(file_path: str) -> list[str]:
    """Transcribe using mlx-whisper fallback. Returns list of '[MM:SS] text' lines."""
    global _mlx_fallback_loaded
    if not _MLX_AVAILABLE:
        raise RuntimeError("mlx-whisper not installed — cannot use as Parakeet fallback")
    if not _mlx_fallback_loaded:
        import numpy as np
        print(f"   🔄 Loading mlx-whisper fallback ({WHISPER_FALLBACK_MODEL})...", flush=True)
        _mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32), path_or_hf_repo=WHISPER_FALLBACK_MODEL, verbose=False)
        _mlx_fallback_loaded = True
    import os as _os
    _os.environ["TQDM_DISABLE"] = "1"
    result = _mlx_whisper.transcribe(file_path, path_or_hf_repo=WHISPER_FALLBACK_MODEL, verbose=False)
    lines = []
    for segment in result.get("segments", []):
        ts = f"[{int(segment['start'] // 60):02d}:{int(segment['start'] % 60):02d}]"
        lines.append(f"{ts} {segment['text'].strip()}")
    return lines


def configure_gemini():
    """Configure Gemini API client (idempotent)."""
    global _gemini_client
    if _gemini_client is not None:
        return
    _gemini_client = genai.Client(api_key=API_KEY)
    print(f"✅ Gemini configured (transcription: {TRANSCRIPTION_MODEL})", flush=True)


def configure_ollama() -> None:
    """Configure Ollama client, verify daemon + model, run warmup (idempotent)."""
    global _ollama_client
    if ANALYSIS_PROVIDER != "ollama":
        return
    if _ollama_client is not None:
        return
    if not _OLLAMA_AVAILABLE:
        raise FatalAPIError("ollama package not installed — run: pip install ollama")

    client = _ollama_lib.Client(host=OLLAMA_HOST, timeout=OLLAMA_TIMEOUT)

    try:
        client.list()
    except Exception as e:
        raise FatalAPIError(
            f"Ollama daemon not reachable at {OLLAMA_HOST} ({type(e).__name__}). "
            "Start it with: ollama serve"
        )

    try:
        client.show(OLLAMA_MODEL)
    except _ollama_lib.ResponseError as e:
        raise FatalAPIError(
            f"Ollama model '{OLLAMA_MODEL}' not found: {e}. "
            f"Pull it with: ollama pull {OLLAMA_MODEL}"
        )

    print(f"   🔥 Warming up '{OLLAMA_MODEL}' (num_ctx={OLLAMA_NUM_CTX})...", flush=True)
    try:
        client.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            think=False,   # never think during warmup — avoids reasoning timeout
            options=_ollama_lib.Options(num_ctx=OLLAMA_NUM_CTX),
            keep_alive=OLLAMA_KEEP_ALIVE,
        )
    except Exception as e:
        # Warmup failure is non-fatal — preflight passed, model is present.
        # First analysis call will absorb the cold-start latency instead.
        print(
            f"   ⚠️  Ollama warmup timed out ({type(e).__name__}) — "
            "service starting anyway; first analysis will be slower",
            flush=True,
        )

    _ollama_client = client
    print(
        f"✅ Ollama ready ({OLLAMA_MODEL} @ {OLLAMA_HOST}, "
        f"num_ctx={OLLAMA_NUM_CTX}, keep_alive={OLLAMA_KEEP_ALIVE}s, "
        f"thinking={OLLAMA_THINKING}, timeout={OLLAMA_TIMEOUT}s)",
        flush=True,
    )


# ============================================================================
# ERROR CLASSIFICATION
# ============================================================================


class FatalAPIError(Exception):
    """API error that should stop the entire service (bad key, no permissions)."""


class PermanentFileError(Exception):
    """Error specific to one file that retrying won't fix (bad format, etc)."""


def classify_api_error(error: Exception) -> Exception:
    """Map a google.genai error to FatalAPIError, PermanentFileError, or return as-is."""
    if isinstance(error, genai_errors.ClientError):
        if error.code in (401, 403):
            return FatalAPIError(f"Gemini API key rejected ({error.code}): {error}")
        if error.code == 400:
            return PermanentFileError(f"Bad request (bad file or prompt): {error}")
    return error


def classify_ollama_error(error: Exception) -> tuple[str, bool]:
    """Map an Ollama/httpx exception to (human_message, is_retryable)."""
    if _httpx and isinstance(error, _httpx.TimeoutException):
        return (
            f"inference timed out after {OLLAMA_TIMEOUT}s — "
            "consider raising ollama_timeout in config.yaml",
            True,
        )
    if _httpx and isinstance(error, (_httpx.ConnectError, ConnectionError)):
        return (
            f"daemon became unreachable at {OLLAMA_HOST} — "
            "check if Ollama is still running",
            True,
        )
    if _OLLAMA_AVAILABLE and isinstance(error, _ollama_lib.ResponseError):
        if "not found" in str(error).lower():
            return (
                f"model '{OLLAMA_MODEL}' was evicted and cannot reload — "
                f"run: ollama pull {OLLAMA_MODEL}",
                False,
            )
        return (f"Ollama API error: {error}", True)
    return (f"unexpected error ({type(error).__name__}): {error}", True)


def extract_response_text(response) -> str:
    """Pull text out of a Gemini GenerateContentResponse, raising on empty/blocked."""
    try:
        text = response.text
    except Exception as e:
        raise PermanentFileError(f"Could not extract response text: {e}") from e
    if not text or not text.strip():
        raise PermanentFileError("Gemini returned empty transcript")
    return text.strip()


# ============================================================================
# STATE MANAGEMENT
# ============================================================================


def load_state():
    """Load processed files state from disk."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️  Warning: Could not load state file, starting fresh: {e}", flush=True)
    return {"processed": {}}


def save_state(state):
    """Save processed files state to disk."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
    except OSError as e:
        print(f"⚠️  Warning: Could not save state file: {e}", flush=True)


# ============================================================================
# HELPERS: TIMESTAMP, PARSING, FILE I/O
# ============================================================================


def get_audio_timestamp(audio_path):
    """Extract recording timestamp from audio file.

    Strategy: 1) macOS mdls  2) directory/filename parse  3) file ctime
    """
    # Strategy 1: macOS metadata
    try:
        result = subprocess.run(
            ["mdls", "-name", "kMDItemContentCreationDate", "-raw", audio_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            timestamp_str = result.stdout.strip()
            for fmt in ["%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"]:
                try:
                    return datetime.strptime(timestamp_str, fmt)
                except ValueError:
                    continue
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass

    # Strategy 2: directory + filename parse
    try:
        path_parts = Path(audio_path).parts
        filename = Path(audio_path).stem
        for part in reversed(path_parts):
            if len(part) == 10 and part[4] == "-" and part[7] == "-":
                try:
                    year, month, day = part.split("-")
                    time_part = filename.split()[0] if " " in filename else filename
                    hour, minute, second = time_part.split("-")
                    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass

    # Strategy 3: file creation time
    try:
        ctime = os.path.getctime(audio_path)
        return datetime.fromtimestamp(ctime)
    except Exception:
        return datetime.now()


def extract_section(content, start_marker, end_marker):
    """Extract content between start_marker and end_marker."""
    lines = content.split("\n")
    capturing = False
    section_lines = []

    for line in lines:
        if start_marker and start_marker in line:
            capturing = True
            continue
        if end_marker and end_marker in line:
            break
        if capturing:
            section_lines.append(line)

    return "\n".join(section_lines).strip()


def parse_transcript(content: str) -> str:
    """Extract raw transcript text from transcription model response."""
    return content.strip()


def parse_analysis_response(content: str) -> tuple[str, str, str]:
    """Parse CATEGORY, FILENAME, and analysis body from analysis model response."""
    category = "DEFAULT"
    filename = "Unknown Meeting.md"

    lines = content.strip().split("\n")
    for line in lines:
        if line.startswith("CATEGORY:"):
            extracted_cat = line.split("CATEGORY:")[1].strip().upper()
            extracted_cat = extracted_cat.replace('"', "").replace("'", "")
            if extracted_cat in FOLDERS:
                category = extracted_cat
        elif line.startswith("FILENAME:"):
            extracted_name = line.split("FILENAME:")[1].strip()
            extracted_name = extracted_name.replace("/", "-").replace(":", ".")
            if extracted_name and not extracted_name.endswith(".md"):
                extracted_name += ".md"
            filename = extracted_name

    analysis_content = extract_section(content, "---ANALYSIS---", None)

    if not analysis_content:
        clean_lines = [
            line for line in lines
            if not (line.startswith("CATEGORY:") or line.startswith("FILENAME:"))
        ]
        analysis_content = "\n".join(clean_lines).strip()
        if analysis_content.startswith("---"):
            analysis_content = analysis_content[3:].strip()

    return category, filename, analysis_content


def save_transcript(category, filename, transcript_content):
    """Save transcript to disk. Prevents same-minute duplicates."""
    dest_folder = FOLDERS.get(category, FOLDERS["DEFAULT"])
    transcripts_folder = os.path.join(dest_folder, "transcripts")

    try:
        os.makedirs(transcripts_folder, exist_ok=True)
    except OSError as e:
        print(f"⚠️  Warning: Could not create transcripts folder: {e}", flush=True)
        transcripts_folder = dest_folder

    # Prevent duplicates: if a transcript with the same YY-MM-DD HH.MM prefix exists, skip
    timestamp_prefix = filename[:14]
    try:
        for existing in os.listdir(transcripts_folder):
            if existing.startswith(timestamp_prefix) and existing.endswith(".md"):
                existing_path = os.path.join(transcripts_folder, existing)
                print(f"   ⚠️  Duplicate prevented: '{timestamp_prefix}' already exists as '{existing}'", flush=True)
                return existing_path
    except OSError:
        pass

    transcript_path = os.path.join(transcripts_folder, filename)
    try:
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript_content)
        return transcript_path
    except (OSError, IOError) as e:
        print(f"❌ Failed to save transcript: {e}", flush=True)
        raise


def save_analysis(category, filename, analysis_content):
    """Save analysis to disk."""
    dest_folder = FOLDERS.get(category, FOLDERS["DEFAULT"])
    analysis_folder = os.path.join(dest_folder, "analysis")

    try:
        os.makedirs(analysis_folder, exist_ok=True)
    except OSError as e:
        print(f"⚠️  Warning: Could not create analysis folder: {e}", flush=True)
        analysis_folder = dest_folder

    analysis_filename = filename.replace(".md", " - Analysis.md")
    analysis_path = os.path.join(analysis_folder, analysis_filename)

    try:
        with open(analysis_path, "w", encoding="utf-8") as f:
            f.write(analysis_content)
        return analysis_path
    except (OSError, IOError) as e:
        print(f"⚠️  Warning: Failed to save analysis: {e}", flush=True)
        return None


def log_failed_analysis(transcript_path, category, filename):
    """Append a NEEDS_ANALYSIS entry to the persistent failure log."""
    try:
        entry = f"{datetime.now().isoformat()} | NEEDS_ANALYSIS | {category} | {filename} | {transcript_path}\n"
        with open(FAILED_ANALYSIS_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
        print(f"   📝 Logged to failed_analysis.log: {filename}", flush=True)
    except OSError as e:
        print(f"⚠️  Warning: Could not write to failed_analysis.log: {e}", flush=True)


# ============================================================================
# DISCOVERY & INDEXING
# ============================================================================


def build_transcript_index(folders):
    """Scan all category folders, build lookup dict keyed by "YY-MM-DD HH.MM"."""
    index = {}

    for category, base_path in folders.items():
        transcripts_folder = os.path.join(base_path, "transcripts")
        if not os.path.exists(transcripts_folder):
            continue

        try:
            for filename in os.listdir(transcripts_folder):
                if not filename.endswith(".md"):
                    continue
                if len(filename) >= 14:
                    timestamp_key = filename[:14]
                    analysis_folder = os.path.join(base_path, "analysis")
                    analysis_filename = filename.replace(".md", " - Analysis.md")
                    analysis_path = os.path.join(analysis_folder, analysis_filename)

                    index[timestamp_key] = {
                        "category": category,
                        "transcript_path": os.path.join(transcripts_folder, filename),
                        "analysis_path": analysis_path if os.path.exists(analysis_path) else None,
                    }
        except (OSError, PermissionError) as e:
            print(f"⚠️  Warning: Could not scan {transcripts_folder}: {e}", flush=True)

    return index


def is_file_stable(path, wait_seconds=2):
    """Check if file has finished syncing (not still downloading from iCloud)."""
    try:
        size1 = os.path.getsize(path)
        if size1 == 0:
            return False
        time.sleep(wait_seconds)
        size2 = os.path.getsize(path)
        return size1 == size2
    except (OSError, FileNotFoundError):
        return False


def discover_recent_folders(watch_folder, days_back=7):
    """Find date-based subfolders from the last N days, sorted oldest-first."""
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    cutoff = datetime.now() - timedelta(days=days_back)
    recent_folders = []

    try:
        for entry in os.listdir(watch_folder):
            if not date_pattern.match(entry):
                continue
            full_path = os.path.join(watch_folder, entry)
            if not os.path.isdir(full_path):
                continue
            try:
                folder_date = datetime.strptime(entry, "%Y-%m-%d")
                if folder_date >= cutoff:
                    recent_folders.append((folder_date, full_path))
            except ValueError:
                continue
    except OSError as e:
        print(f"❌ Cannot list watch folder: {e}", flush=True)
        return []

    recent_folders.sort(key=lambda x: x[0])
    return [path for _, path in recent_folders]


# ============================================================================
# PROCESSING WITH RETRY
# ============================================================================


def _get_audio_duration(file_path: str) -> float | None:
    """Return audio duration in seconds via ffprobe, or None on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", file_path],
            capture_output=True, text=True, timeout=5,
        )
        return float(result.stdout.strip()) if result.returncode == 0 else None
    except Exception:
        return None


def upload_to_gemini(file_path: str):
    """Upload audio file to Gemini Files API and wait until it is ACTIVE."""
    uploaded = _gemini_client.files.upload(
        file=file_path,
        config=genai_types.UploadFileConfig(mimeType="audio/m4a"),
    )
    deadline = time.time() + API_TIMEOUT
    while uploaded.state == genai_types.FileState.PROCESSING:
        if time.time() > deadline:
            raise TimeoutError(f"Gemini file upload timed out after {API_TIMEOUT}s")
        time.sleep(5)
        uploaded = _gemini_client.files.get(name=uploaded.name)
    if uploaded.state == genai_types.FileState.FAILED:
        raise PermanentFileError(f"Gemini rejected uploaded file: {uploaded.name}")
    return uploaded


def transcribe_with_gemini(file_path: str) -> str:
    """Single Gemini Flash transcription attempt. Returns transcript string."""
    uploaded = upload_to_gemini(file_path)
    try:
        response = _gemini_client.models.generate_content(
            model=TRANSCRIPTION_MODEL,
            contents=[TRANSCRIPTION_PROMPT, uploaded],
            config=genai_types.GenerateContentConfig(
                http_options=genai_types.HttpOptions(timeout=API_TIMEOUT * 1000),
            ),
        )
        return extract_response_text(response)
    except (FatalAPIError, PermanentFileError):
        raise
    except Exception as e:
        raise classify_api_error(e) from e
    finally:
        try:
            _gemini_client.files.delete(name=uploaded.name)
        except Exception:
            pass


def transcribe_with_fallback(file_path: str) -> str:
    """Try Gemini Flash (2 attempts), fall back to local Whisper on failure."""
    gemini_attempts = 2
    last_error: Exception | None = None

    for attempt in range(1, gemini_attempts + 1):
        try:
            print(f"   ☁️  Transcribing with Gemini Flash (attempt {attempt}/{gemini_attempts})...", flush=True)
            transcript = transcribe_with_gemini(file_path)
            print(f"   ✅ Gemini transcription succeeded", flush=True)
            return transcript
        except (FatalAPIError, PermanentFileError):
            raise
        except Exception as e:
            last_error = e
            print(f"   ⚠️  Gemini attempt {attempt} failed: {e}", flush=True)
            if attempt < gemini_attempts:
                wait = RETRY_BACKOFF[0]
                print(f"   ⏳ Retrying in {wait}s...", flush=True)
                time.sleep(wait)

    print(
        f"   🔄 Gemini failed after {gemini_attempts} attempts ({last_error}), "
        "falling back to local transcription...",
        flush=True,
    )
    return transcribe_local(file_path)


def transcribe_local(file_path: str) -> str:
    """Transcribe audio locally (Gemini fallback). Loads model on first call."""
    configure_whisper()  # lazy — no-op if already loaded

    try:
        duration_secs = _get_audio_duration(file_path)
        duration_str = f"{int(duration_secs // 60)}m {int(duration_secs % 60)}s" if duration_secs else "unknown length"
    except Exception:
        duration_str = "unknown length"

    lines: list[str] = []
    try:
        if WHISPER_BACKEND == "parakeet":
            if duration_secs and duration_secs > PARAKEET_MAX_SECS:
                print(f"   ⏭️  Recording too long for Parakeet ({duration_str} > 15m), using mlx-whisper...", flush=True)
                try:
                    lines = _transcribe_with_mlx_fallback(file_path)
                except Exception as e:
                    raise PermanentFileError(f"mlx-whisper failed on long recording: {e}") from e
            else:
                print(f"   🧠 Transcribing locally (Parakeet, {duration_str})...", flush=True)
                result = _parakeet_model.transcribe(file_path)
                for segment in result.segments:
                    ts = f"[{int(segment.start // 60):02d}:{int(segment.start % 60):02d}]"
                    lines.append(f"{ts} {segment.text.strip()}")
        elif WHISPER_BACKEND == "mlx":
            print(f"   🧠 Transcribing locally (mlx-whisper, {duration_str})...", flush=True)
            import os as _os
            _os.environ["TQDM_DISABLE"] = "1"
            result = _mlx_whisper.transcribe(file_path, path_or_hf_repo=WHISPER_MODEL, verbose=False)
            for segment in result.get("segments", []):
                ts = f"[{int(segment['start'] // 60):02d}:{int(segment['start'] % 60):02d}]"
                lines.append(f"{ts} {segment['text'].strip()}")
        else:
            print(f"   🧠 Transcribing locally (faster-whisper, {duration_str})...", flush=True)
            with warnings.catch_warnings():
                # faster_whisper emits numpy RuntimeWarnings on silent frames — benign.
                warnings.filterwarnings("ignore", category=RuntimeWarning, module="faster_whisper")
                segments, _info = _whisper_model.transcribe(
                    file_path,
                    beam_size=5,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500},
                )
            for segment in segments:
                ts = f"[{int(segment.start // 60):02d}:{int(segment.start % 60):02d}]"
                lines.append(f"{ts} {segment.text.strip()}")
    except PermanentFileError:
        raise
    except Exception as e:
        if WHISPER_BACKEND == "parakeet":
            print(f"   ⚠️  Parakeet failed ({e}), falling back to mlx-whisper ({WHISPER_FALLBACK_MODEL})...", flush=True)
            try:
                lines = _transcribe_with_mlx_fallback(file_path)
            except Exception as fallback_e:
                raise PermanentFileError(
                    f"Both Parakeet and mlx-whisper fallback failed — parakeet={e}, mlx={fallback_e}"
                ) from fallback_e
        else:
            raise PermanentFileError(f"Transcription failed on this file: {e}") from e

    if not lines:
        raise PermanentFileError("Transcription returned empty result — audio may be silent or corrupt")

    return "\n".join(lines)


# --- Claude analysis (disabled) ---
# def analyze_with_claude(transcript_content): ...


def analyze_with_ollama(transcript_content: str) -> tuple[str, str, str] | None:
    """Single analysis+classification attempt via local Ollama.

    Returns (category, filename, analysis) on success.
    Returns None on retryable failure.
    Raises PermanentFileError on non-retryable failure.
    """
    if _ollama_client is None:
        return None
    try:
        response = _ollama_client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": ANALYSIS_PROMPT},
                {"role": "user", "content": f"---TRANSCRIPT TO ANALYZE---\n{transcript_content}"},
            ],
            think=OLLAMA_THINKING,
            options=_ollama_lib.Options(num_ctx=OLLAMA_NUM_CTX),
            keep_alive=OLLAMA_KEEP_ALIVE,
        )
        return parse_analysis_response(response.message.content)
    except Exception as e:
        message, retryable = classify_ollama_error(e)
        print(f"   ❌ Ollama analysis failed: {message}", flush=True)
        if not retryable:
            raise PermanentFileError(f"Ollama: {message}") from e
        return None


def analyze_with_retry(transcript_content: str) -> tuple[str, str, str] | None:
    """Call Ollama analysis with retry backoff.

    Attempt sequence: Ollama → Ollama(60s) → Ollama(180s) → Ollama(300s)

    Returns (category, filename, analysis) tuple or None.
    """
    schedule = [
        (0 if i == 0 else ANALYSIS_RETRY_BACKOFF[min(i - 1, len(ANALYSIS_RETRY_BACKOFF) - 1)])
        for i in range(len(ANALYSIS_RETRY_BACKOFF) + 1)
    ]
    total = len(schedule)

    for attempt_idx, wait_before in enumerate(schedule):
        attempt_num = attempt_idx + 1

        if wait_before > 0:
            print(f"   ⏳ Analysis attempt {attempt_num}/{total} [Ollama] in {wait_before}s...", flush=True)
            time.sleep(wait_before)

        print(f"   📊 Analyzing transcript [Ollama] (attempt {attempt_num}/{total})...", flush=True)
        result = analyze_with_ollama(transcript_content)
        if result is not None:
            print(f"   ✅ Analysis succeeded via Ollama (attempt {attempt_num}/{total})", flush=True)
            return result

    print(f"   ⚠️  Analysis failed after {total} attempts", flush=True)
    return None


def process_audio(file_path, timestamp, state):
    """Full processing pipeline for one audio file with retry at each stage.

    Returns (success: bool, category: str).
    Raises FatalAPIError if the API key is bad.
    """
    basename = os.path.basename(file_path)
    attempts = state.get("processed", {}).get(file_path, {}).get("attempts", 0)

    try:
        # Stage B: Transcribe (Gemini Flash primary, local Whisper fallback after 2 failures)
        transcript_content = transcribe_with_fallback(file_path)
        formatted_timestamp = timestamp.strftime(TIMESTAMP_FORMAT)

        # Stage C: Analyze + Classify
        analysis_result = analyze_with_retry(transcript_content)

        # Stage D: Save transcript + analysis to correct category folder
        if analysis_result:
            category, ai_filename, analysis_text = analysis_result
            filename = f"{formatted_timestamp} - {ai_filename}"
            transcript_path = save_transcript(category, filename, transcript_content)
            print(f"   ✅ Transcript saved: {transcript_path}", flush=True)
            analysis_path = save_analysis(category, filename, analysis_text)
            if analysis_path:
                print(f"   ✅ Analysis saved: {analysis_path}", flush=True)
        else:
            category = "DEFAULT"
            filename = f"{formatted_timestamp} - Unknown Meeting.md"
            transcript_path = save_transcript(category, filename, transcript_content)
            print(f"   ✅ Transcript saved (DEFAULT — analysis failed): {transcript_path}", flush=True)
            log_failed_analysis(transcript_path, category, filename)

        state.setdefault("processed", {})[file_path] = {
            "status": "complete",
            "category": category,
            "timestamp": timestamp.isoformat(),
            "processed_at": datetime.now().isoformat(),
            "attempts": attempts + 1,
        }
        save_state(state)
        return True, category

    except FatalAPIError:
        raise

    except PermanentFileError as e:
        print(f"   🛑 Permanent error for {basename}: {e}", flush=True)
        state.setdefault("processed", {})[file_path] = {
            "status": "failed_permanent",
            "error": str(e),
            "processed_at": datetime.now().isoformat(),
            "attempts": attempts + 1,
        }
        save_state(state)
        return False, None

    except Exception as e:
        print(f"   ❌ Failed to process {basename}: {e}", flush=True)

        attempts += 1
        if attempts >= MAX_RETRIES:
            state.setdefault("processed", {})[file_path] = {
                "status": "failed_permanent",
                "error": str(e),
                "processed_at": datetime.now().isoformat(),
                "attempts": attempts,
            }
            print(f"   🛑 Permanently failed after {attempts} attempts", flush=True)
        else:
            state.setdefault("processed", {})[file_path] = {
                "status": "failed_retry",
                "error": str(e),
                "processed_at": datetime.now().isoformat(),
                "attempts": attempts,
            }
            print(f"   🔄 Will retry on next cycle (attempt {attempts}/{MAX_RETRIES})", flush=True)

        save_state(state)
        return False, None
