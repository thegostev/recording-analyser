import os
import re
import time
import sys
import glob
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
import google.generativeai as genai
from google.api_core import exceptions as api_exceptions
from google.generativeai.types import RequestOptions

from config import (
    API_KEY, TRANSCRIPTION_MODEL, ANALYSIS_MODEL,
    WATCH_FOLDER, FOLDERS, STATE_FILE, FAILED_ANALYSIS_LOG,
    TRANSCRIPTION_PROMPT, ANALYSIS_PROMPT,
    SCAN_INTERVAL, SCAN_DAYS_BACK, DELAY_BETWEEN_FILES,
    MAX_RETRIES, MAX_FILES_PER_CYCLE, RETRY_BACKOFF,
    ANALYSIS_RETRY_BACKOFF, API_TIMEOUT,
)

genai.configure(api_key=API_KEY)


# ============================================================================
# API ERROR CLASSIFICATION
# ============================================================================

class FatalAPIError(Exception):
    """API error that should stop the entire service (bad key, no permissions)."""
    pass


class PermanentFileError(Exception):
    """Error specific to one file that retrying won't fix (bad format, etc)."""
    pass


def classify_api_error(error):
    """
    Classify a Gemini API error to decide retry strategy.
    Returns: "fatal", "permanent", "transient"
    """
    if isinstance(error, (api_exceptions.Unauthenticated, api_exceptions.PermissionDenied)):
        return "fatal"
    if isinstance(error, (api_exceptions.InvalidArgument, api_exceptions.BadRequest)):
        return "permanent"
    if isinstance(error, (api_exceptions.ResourceExhausted, api_exceptions.ServiceUnavailable,
                          api_exceptions.DeadlineExceeded, api_exceptions.InternalServerError)):
        return "transient"
    if isinstance(error, api_exceptions.GoogleAPICallError):
        return "transient"
    return "transient"


def extract_response_text(response):
    """
    Safely extract text from a Gemini response.
    Raises ValueError if response is blocked or empty.
    """
    if not response.candidates:
        raise ValueError("Gemini returned no candidates (empty response)")

    candidate = response.candidates[0]
    finish_reason = getattr(candidate, 'finish_reason', None)

    # finish_reason values: STOP=normal, SAFETY=blocked, MAX_TOKENS=truncated, OTHER=unknown
    if finish_reason is not None:
        reason_name = finish_reason.name if hasattr(finish_reason, 'name') else str(finish_reason)
        if reason_name == "SAFETY":
            raise ValueError(f"Response blocked by safety filter (finish_reason={reason_name})")
        if reason_name == "OTHER":
            raise ValueError(f"Response failed with finish_reason={reason_name}")

    try:
        text = response.text
    except ValueError as e:
        raise ValueError(f"Could not extract response text: {e}")

    if not text or not text.strip():
        raise ValueError("Gemini returned empty text")

    return text


# ============================================================================
# PERSISTENT STATE
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
# HELPER FUNCTIONS (shared with ondemand_transcribe.py)
# ============================================================================

def get_audio_timestamp(audio_path):
    """
    Extract recording timestamp from audio file.

    Strategy:
    1. Use macOS mdls to get kMDItemContentCreationDate
    2. Fallback: Parse from parent directory + filename (YYYY-MM-DD/HH-MM-SS.m4a)
    3. Final fallback: File creation time
    """
    # Strategy 1: Try macOS metadata
    try:
        result = subprocess.run(
            ["mdls", "-name", "kMDItemContentCreationDate", "-raw", audio_path],
            capture_output=True,
            text=True,
            timeout=5
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

    # Strategy 2: Parse from directory + filename
    try:
        path_parts = Path(audio_path).parts
        filename = Path(audio_path).stem

        for part in reversed(path_parts):
            if len(part) == 10 and part[4] == '-' and part[7] == '-':
                try:
                    year, month, day = part.split('-')
                    time_part = filename.split()[0] if ' ' in filename else filename
                    hour, minute, second = time_part.split('-')
                    return datetime(int(year), int(month), int(day),
                                    int(hour), int(minute), int(second))
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass

    # Strategy 3: File creation time fallback
    try:
        ctime = os.path.getctime(audio_path)
        return datetime.fromtimestamp(ctime)
    except Exception:
        return datetime.now()


def extract_section(content, start_marker, end_marker):
    """Extract content between start_marker and end_marker."""
    lines = content.split('\n')
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

    return '\n'.join(section_lines).strip()


def parse_transcription_response(content):
    """Parse CATEGORY, FILENAME, and transcript from Gemini response."""
    category = "DEFAULT"
    filename = "Unknown Meeting.md"

    lines = content.strip().split('\n')
    for line in lines:
        if line.startswith("CATEGORY:"):
            extracted_cat = line.split("CATEGORY:")[1].strip().upper()
            extracted_cat = extracted_cat.replace('"', '').replace("'", "")
            if extracted_cat in FOLDERS:
                category = extracted_cat
        elif line.startswith("FILENAME:"):
            extracted_name = line.split("FILENAME:")[1].strip()
            extracted_name = extracted_name.replace("/", "-").replace(":", ".")
            if extracted_name and not extracted_name.endswith(".md"):
                extracted_name += ".md"
            filename = extracted_name

    transcript_content = extract_section(content, "---TRANSCRIPT---", None)

    if not transcript_content:
        clean_content_lines = []
        for line in lines:
            if not (line.startswith("CATEGORY:") or line.startswith("FILENAME:")):
                clean_content_lines.append(line)
        transcript_content = "\n".join(clean_content_lines).strip()
        if transcript_content.startswith("---"):
            transcript_content = transcript_content[3:].strip()

    return category, filename, transcript_content


def save_transcript(category, filename, transcript_content):
    """Save transcript to disk."""
    dest_folder = FOLDERS.get(category, FOLDERS["DEFAULT"])
    transcripts_folder = os.path.join(dest_folder, "transcripts")

    try:
        os.makedirs(transcripts_folder, exist_ok=True)
    except OSError as e:
        print(f"⚠️  Warning: Could not create transcripts folder: {e}", flush=True)
        transcripts_folder = dest_folder

    # Prevent duplicates: if a transcript with the same YY-MM-DD HH.MM prefix already
    # exists (e.g. from a same-minute retry), return the existing path instead of overwriting.
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
    """Append a NEEDS_ANALYSIS entry to the persistent failure log so the backlog is visible."""
    try:
        entry = f"{datetime.now().isoformat()} | NEEDS_ANALYSIS | {category} | {filename} | {transcript_path}\n"
        with open(FAILED_ANALYSIS_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
        print(f"   📝 Logged to failed_analysis.log: {filename}", flush=True)
    except OSError as e:
        print(f"⚠️  Warning: Could not write to failed_analysis.log: {e}", flush=True)


def build_transcript_index(folders):
    """
    Scan all category folders, build lookup dictionary.
    Returns dict keyed by "YY-MM-DD HH.MM" timestamp strings.
    """
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


# ============================================================================
# AUTO-DISCOVERY
# ============================================================================

def discover_recent_folders(watch_folder, days_back=SCAN_DAYS_BACK):
    """
    Find date-based subfolders from the last N days.
    Returns list of full paths to date folders, sorted oldest-first.
    """
    date_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}$')
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


def discover_audio_files(watch_folder, state, transcript_index):
    """
    Find all unprocessed .m4a files in recent date folders.
    Returns list of (audio_path, timestamp) tuples, oldest first.
    Capped at MAX_FILES_PER_CYCLE to prevent stampedes.
    """
    folders = discover_recent_folders(watch_folder)
    audio_files = []
    state_dirty = False

    for folder_path in folders:
        pattern = os.path.join(folder_path, "*.m4a")
        for file_path in glob.glob(pattern):
            basename = os.path.basename(file_path)

            # Filter 1: Skip iCloud placeholders, temp files, hidden files
            if ".icloud" in file_path or ".tmp" in file_path or basename.startswith("."):
                continue

            # Filter 2: Skip if already in persistent state as complete or permanently failed
            if file_path in state.get("processed", {}):
                entry = state["processed"][file_path]
                if entry.get("status") in ("complete", "failed_permanent"):
                    continue

            # Filter 3: Get timestamp and check transcript index BEFORE stability check
            timestamp = get_audio_timestamp(file_path)
            timestamp_key = timestamp.strftime("%y-%m-%d %H.%M")
            if timestamp_key in transcript_index:
                existing = transcript_index[timestamp_key]
                if existing.get("analysis_path"):
                    state.setdefault("processed", {})[file_path] = {
                        "status": "complete",
                        "category": existing["category"],
                        "timestamp": timestamp.isoformat(),
                        "processed_at": datetime.now().isoformat(),
                        "attempts": 0,
                        "note": "found in transcript index",
                    }
                    state_dirty = True
                    continue

            # Filter 4: Check file stability (only for genuinely new files)
            if not is_file_stable(file_path, wait_seconds=1):
                continue

            audio_files.append((file_path, timestamp))

    # Batch-write state for all index matches at once
    if state_dirty:
        save_state(state)

    audio_files.sort(key=lambda x: x[1])

    # Cap to prevent first-run stampede
    if len(audio_files) > MAX_FILES_PER_CYCLE:
        deferred = len(audio_files) - MAX_FILES_PER_CYCLE
        print(f"   📋 {len(audio_files)} files found, processing {MAX_FILES_PER_CYCLE} "
              f"this cycle ({deferred} deferred to next cycle)", flush=True)
        audio_files = audio_files[:MAX_FILES_PER_CYCLE]

    return audio_files


# ============================================================================
# PROCESSING WITH RETRY
# ============================================================================

def upload_to_gemini(file_path):
    """Upload audio file and wait for processing. Returns Gemini file object."""
    print("   🚀 Uploading to Gemini...", flush=True)
    audio_file = genai.upload_file(path=file_path)

    while audio_file.state.name == "PROCESSING":
        print("   ...processing audio on server...", flush=True)
        time.sleep(2)
        audio_file = genai.get_file(audio_file.name)

    if audio_file.state.name == "FAILED":
        raise ValueError("Audio processing failed on Google servers.")

    return audio_file


def transcribe_with_retry(audio_file):
    """
    Call transcription model with retry on API errors or bad classification.
    Returns (category, ai_filename, transcript_content).
    Raises FatalAPIError for auth issues, PermanentFileError for bad input.
    """
    best_result = None

    for attempt in range(MAX_RETRIES):
        try:
            if attempt > 0:
                delay = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
                print(f"   ⏳ Retry {attempt + 1}/{MAX_RETRIES} in {delay}s...", flush=True)
                time.sleep(delay)

            print(f"   🧠 Transcribing and classifying (attempt {attempt + 1}/{MAX_RETRIES})...", flush=True)
            model = genai.GenerativeModel(TRANSCRIPTION_MODEL)
            response = model.generate_content(
                [TRANSCRIPTION_PROMPT, audio_file],
                request_options=RequestOptions(timeout=API_TIMEOUT),
            )

            text = extract_response_text(response)
            category, ai_filename, transcript_content = parse_transcription_response(text)
            best_result = (category, ai_filename, transcript_content)

            # Check quality
            is_default = (category == "DEFAULT")
            is_unknown = (ai_filename == "Unknown Meeting.md")

            if not is_default and not is_unknown:
                return best_result

            if is_default:
                print(f"   ⚠️  Classification fell to DEFAULT", flush=True)
            if is_unknown:
                print(f"   ⚠️  Filename is 'Unknown Meeting'", flush=True)

        except (FatalAPIError, PermanentFileError):
            raise
        except Exception as e:
            error_class = classify_api_error(e)
            if error_class == "fatal":
                raise FatalAPIError(f"Authentication/permission error: {e}") from e
            if error_class == "permanent":
                raise PermanentFileError(f"Bad request for this file: {e}") from e
            print(f"   ❌ Transcription attempt {attempt + 1} failed: {e}", flush=True)

    # Exhausted retries - return best result or raise
    if best_result:
        print(f"   ⚠️  Using best available result after {MAX_RETRIES} attempts", flush=True)
        return best_result

    raise RuntimeError(f"Transcription failed after {MAX_RETRIES} attempts")


def analyze_with_retry(transcript_content):
    """
    Call analysis model with retry on API errors.
    Returns analysis text or None.
    Raises FatalAPIError for auth issues.
    """
    for attempt in range(MAX_RETRIES):
        try:
            if attempt > 0:
                delay = ANALYSIS_RETRY_BACKOFF[min(attempt - 1, len(ANALYSIS_RETRY_BACKOFF) - 1)]
                print(f"   ⏳ Analysis retry {attempt + 1}/{MAX_RETRIES} in {delay}s...", flush=True)
                time.sleep(delay)

            print(f"   📊 Analyzing transcript (attempt {attempt + 1}/{MAX_RETRIES})...", flush=True)
            model = genai.GenerativeModel(ANALYSIS_MODEL)
            prompt = f"{ANALYSIS_PROMPT}\n\n---TRANSCRIPT TO ANALYZE---\n{transcript_content}"
            response = model.generate_content(
                prompt,
                request_options=RequestOptions(timeout=API_TIMEOUT),
            )
            return extract_response_text(response)

        except FatalAPIError:
            raise
        except Exception as e:
            error_class = classify_api_error(e)
            if error_class == "fatal":
                raise FatalAPIError(f"Authentication/permission error: {e}") from e
            print(f"   ❌ Analysis attempt {attempt + 1} failed: {e}", flush=True)

    print(f"   ⚠️  Analysis failed after {MAX_RETRIES} attempts (transcript was saved)", flush=True)
    return None


def process_audio(file_path, timestamp, state):
    """
    Full processing pipeline for one audio file with retry at each stage.
    Returns (success: bool, category: str).
    Raises FatalAPIError if the API key is bad (caller should stop the service).
    """
    basename = os.path.basename(file_path)
    attempts = state.get("processed", {}).get(file_path, {}).get("attempts", 0)
    audio_file = None

    try:
        # Stage A: Upload
        audio_file = upload_to_gemini(file_path)

        # Stage B: Transcribe + Classify (with retry)
        category, ai_filename, transcript_content = transcribe_with_retry(audio_file)

        # Use cached timestamp (already computed during discovery)
        formatted_timestamp = timestamp.strftime('%y-%m-%d %H.%M')
        filename = f"{formatted_timestamp} - {ai_filename}"

        # Stage C: Save transcript
        transcript_path = save_transcript(category, filename, transcript_content)
        print(f"   ✅ Transcript saved: {transcript_path}", flush=True)

        # Stage D: Analyze (with retry)
        analysis_text = analyze_with_retry(transcript_content)

        # Stage E: Save analysis + cleanup
        if analysis_text:
            analysis_path = save_analysis(category, filename, analysis_text)
            if analysis_path:
                print(f"   ✅ Analysis saved: {analysis_path}", flush=True)
        else:
            log_failed_analysis(transcript_path, category, filename)

        # Cleanup remote file
        try:
            audio_file.delete()
        except Exception:
            pass

        # Record success in state
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
        # Cleanup and re-raise - caller will stop the service
        if audio_file:
            try:
                audio_file.delete()
            except Exception:
                pass
        raise

    except PermanentFileError as e:
        print(f"   🛑 Permanent error for {basename}: {e}", flush=True)
        if audio_file:
            try:
                audio_file.delete()
            except Exception:
                pass
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

        if audio_file:
            try:
                audio_file.delete()
            except Exception:
                pass

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


# ============================================================================
# MAIN SERVICE LOOP
# ============================================================================

def run_scan_cycle(state, transcript_index, cycle_number):
    """
    Run one scan-and-process cycle. Returns count of files processed.
    Raises FatalAPIError if the API key is invalid.
    """
    new_files = discover_audio_files(WATCH_FOLDER, state, transcript_index)

    if not new_files:
        if cycle_number % 10 == 0:
            print(f"[Cycle {cycle_number}] {datetime.now().strftime('%H:%M:%S')} - No new files", flush=True)
        return 0

    print(f"\n[Cycle {cycle_number}] {datetime.now().strftime('%H:%M:%S')} - "
          f"Found {len(new_files)} file(s) to process", flush=True)

    success_count = 0
    fail_count = 0

    for i, (audio_path, timestamp) in enumerate(new_files, 1):
        basename = os.path.basename(audio_path)
        parent = os.path.basename(os.path.dirname(audio_path))
        print(f"\n📂 [{i}/{len(new_files)}] {parent}/{basename}", flush=True)

        success, category = process_audio(audio_path, timestamp, state)

        if success:
            success_count += 1
            ts_key = timestamp.strftime("%y-%m-%d %H.%M")
            transcript_index[ts_key] = {"category": category}
        else:
            fail_count += 1

        # Rate limiting between files
        if i < len(new_files):
            print(f"   ⏸️  Pausing {DELAY_BETWEEN_FILES}s before next file...", flush=True)
            time.sleep(DELAY_BETWEEN_FILES)

    print(f"\n{'=' * 50}", flush=True)
    print(f"Cycle {cycle_number} complete: {success_count} succeeded, {fail_count} failed", flush=True)
    print(f"{'=' * 50}", flush=True)

    return success_count


def main():
    print("=" * 60, flush=True)
    print("🎙️  Auto-Transcription Service", flush=True)
    print("=" * 60, flush=True)
    print(f"Watch folder: {WATCH_FOLDER}", flush=True)
    print(f"Scanning last {SCAN_DAYS_BACK} days of recordings", flush=True)
    print(f"Scan interval: {SCAN_INTERVAL}s | Delay between files: {DELAY_BETWEEN_FILES}s", flush=True)
    print(f"Max retries: {MAX_RETRIES} per stage | Max files/cycle: {MAX_FILES_PER_CYCLE}", flush=True)
    print(f"API timeout: {API_TIMEOUT}s | State file: {STATE_FILE}", flush=True)
    print("=" * 60, flush=True)

    # Startup
    state = load_state()
    processed_count = len([v for v in state.get("processed", {}).values()
                           if v.get("status") == "complete"])
    failed_count = len([v for v in state.get("processed", {}).values()
                        if v.get("status") == "failed_permanent"])
    print(f"\n📚 Loaded state: {processed_count} completed, {failed_count} permanently failed", flush=True)

    print("📚 Building transcript index...", flush=True)
    transcript_index = build_transcript_index(FOLDERS)
    print(f"Found {len(transcript_index)} existing transcripts", flush=True)

    print(f"\n🔄 Starting scan loop (every {SCAN_INTERVAL}s)...\n", flush=True)

    cycle = 0
    while True:
        cycle += 1
        try:
            run_scan_cycle(state, transcript_index, cycle)
        except FatalAPIError as e:
            print(f"\n🛑 FATAL: {e}", flush=True)
            print("   API key or permissions issue. Service stopping.", flush=True)
            sys.exit(1)
        except Exception as e:
            print(f"\n❌ Scan cycle {cycle} error: {e}", flush=True)
            print("   Continuing to next cycle...", flush=True)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Stopping auto-transcription service...", flush=True)
        sys.exit(0)
