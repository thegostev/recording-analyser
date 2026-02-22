import os
import time
import sys
import glob
import subprocess
import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path
import google.generativeai as genai

from config import (
    API_KEY, TRANSCRIPTION_MODEL, ANALYSIS_MODEL,
    WATCH_FOLDER, FOLDERS,
    TRANSCRIPTION_PROMPT, ANALYSIS_PROMPT,
)

genai.configure(api_key=API_KEY)

# Specific subfolders to scan in manual mode (edit before each run).
# In catchup mode (--catchup), folders are auto-discovered instead.
SCAN_SUBFOLDERS = [
    "2026-01-18",
    "2026-01-19",
    "2026-01-20"
]

# ============================================================================
# HELPER FUNCTIONS (from auto_transcribe.py)
# ============================================================================

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
    """Parse CATEGORY, FILENAME, and transcript from Call 1 response."""
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

    # Extract transcript content
    transcript_content = extract_section(content, "---TRANSCRIPT---", None)

    # Fallback if no transcript marker found
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
    """Save transcript to disk (separate function for clarity)."""
    dest_folder = FOLDERS.get(category, FOLDERS["DEFAULT"])
    transcripts_folder = os.path.join(dest_folder, "transcripts")

    try:
        os.makedirs(transcripts_folder, exist_ok=True)
    except OSError as e:
        print(f"⚠️  Warning: Could not create transcripts folder: {e}", flush=True)
        transcripts_folder = dest_folder

    transcript_path = os.path.join(transcripts_folder, filename)
    try:
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript_content)
        return transcript_path
    except (OSError, IOError) as e:
        print(f"❌ Failed to save transcript: {e}", flush=True)
        raise

def save_analysis(category, filename, analysis_content):
    """Save analysis to disk (separate function for clarity)."""
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
        print(f"   Transcript was saved successfully.", flush=True)
        return None  # Non-fatal, transcript is primary

def process_audio(file_path):
    """Process audio file through transcription and analysis stages."""
    try:
        print("🚀 Uploading to Gemini...", flush=True)
        # Upload file to Gemini
        audio_file = genai.upload_file(path=file_path)

        # Poll for processing completion
        while audio_file.state.name == "PROCESSING":
            print("...processing audio on server...", flush=True)
            time.sleep(2)
            audio_file = genai.get_file(audio_file.name)

        if audio_file.state.name == "FAILED":
            raise ValueError("Audio processing failed on Google servers.")

        # CALL 1: Transcription + Classification (using FAST model)
        print("🧠 Transcribing and Classifying...", flush=True)
        transcription_model = genai.GenerativeModel(TRANSCRIPTION_MODEL)
        transcription_response = transcription_model.generate_content([TRANSCRIPTION_PROMPT, audio_file])

        # Extract correct timestamp from audio file
        timestamp = get_audio_timestamp(file_path)
        formatted_timestamp = timestamp.strftime('%y-%m-%d %H.%M')

        # Parse transcription response (now without date/time)
        category, ai_filename, transcript_content = parse_transcription_response(transcription_response.text)

        # Construct final filename with correct timestamp
        filename = f"{formatted_timestamp} - {ai_filename}"

        # Save transcript immediately (failure recovery point)
        transcript_path = save_transcript(category, filename, transcript_content)
        print(f"✅ Transcript saved: {transcript_path}", flush=True)

        # CALL 2: Analysis (text input only, using PRO model)
        print("📊 Analyzing transcript...", flush=True)
        analysis_model = genai.GenerativeModel(ANALYSIS_MODEL)
        analysis_prompt_with_transcript = f"{ANALYSIS_PROMPT}\n\n---TRANSCRIPT TO ANALYZE---\n{transcript_content}"
        analysis_response = analysis_model.generate_content(analysis_prompt_with_transcript)

        # Save analysis
        analysis_path = save_analysis(category, filename, analysis_response.text)
        if analysis_path:
            print(f"✅ Analysis saved: {analysis_path}", flush=True)

        # Cleanup remote file
        audio_file.delete()

        return True

    except Exception as e:
        print(f"❌ Error: {e}", flush=True)
        return False

# ============================================================================
# NEW FUNCTIONS FOR ON-DEMAND PROCESSING
# ============================================================================

def discover_recent_folders(watch_folder, days_back=7):
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

def get_audio_timestamp(audio_path):
    """
    Extract recording timestamp from audio file.

    Strategy:
    1. Use macOS mdls to get kMDItemContentCreationDate
    2. Fallback: Parse from parent directory + filename (YYYY-MM-DD/HH-MM-SS.m4a)
    3. Final fallback: File creation time

    Returns: datetime object
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
            # Parse format: "2025-12-22 12:07:11 +0000"
            timestamp_str = result.stdout.strip()
            # Try parsing with timezone
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
        filename = Path(audio_path).stem  # Without extension

        # Find YYYY-MM-DD pattern in parent directories
        for part in reversed(path_parts):
            if len(part) == 10 and part[4] == '-' and part[7] == '-':
                # Looks like YYYY-MM-DD
                try:
                    year, month, day = part.split('-')
                    # Parse filename as HH-MM-SS or HH-MM-SS 2 (for collisions)
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
        # Last resort: current time
        return datetime.now()

def build_transcript_index(folders):
    """
    Scan all category folders once, build lookup dictionary.

    Returns: {
        "25-12-22 13:07": {  # YY-MM-DD HH:MM format
            "category": "MINNESOTERE",
            "transcript_path": "/path/to/transcript.md",
            "analysis_path": "/path/to/analysis.md" or None
        }
    }
    """
    index = {}

    for category, base_path in folders.items():
        # Check transcripts folder
        transcripts_folder = os.path.join(base_path, "transcripts")
        if not os.path.exists(transcripts_folder):
            continue

        try:
            for filename in os.listdir(transcripts_folder):
                if not filename.endswith(".md"):
                    continue

                # Extract timestamp from filename: "YY-MM-DD HH:MM - [Title].md"
                # Take first 14 characters: "25-12-22 13:07"
                if len(filename) >= 14:
                    timestamp_key = filename[:14]

                    # Check if analysis exists
                    analysis_folder = os.path.join(base_path, "analysis")
                    analysis_filename = filename.replace(".md", " - Analysis.md")
                    analysis_path = os.path.join(analysis_folder, analysis_filename)

                    index[timestamp_key] = {
                        "category": category,
                        "transcript_path": os.path.join(transcripts_folder, filename),
                        "analysis_path": analysis_path if os.path.exists(analysis_path) else None
                    }
        except (OSError, PermissionError) as e:
            print(f"⚠️  Warning: Could not scan {transcripts_folder}: {e}", flush=True)
            continue

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

def discover_audio_files(watch_folder, scan_subfolders, verbose=False):
    """
    Scan specific subfolders within watch folder for all .m4a files.

    Args:
        watch_folder: Base directory path
        scan_subfolders: List of subfolder names to scan (relative to watch_folder)
        verbose: Print detailed progress

    Returns: List of tuples (audio_path, timestamp)

    Raises:
        ValueError: If scan_subfolders is empty or None
    """
    # Validation: Ensure subfolders are specified
    if not scan_subfolders:
        raise ValueError(
            "SCAN_SUBFOLDERS must contain at least one subfolder. "
            "Recursive full-folder scanning is not supported. "
            "Update SCAN_SUBFOLDERS in configuration section."
        )

    audio_files = []
    nonexistent_subfolders = []

    # Scan each specified subfolder
    for subfolder in scan_subfolders:
        subfolder_path = os.path.join(watch_folder, subfolder)

        # Check if subfolder exists
        if not os.path.exists(subfolder_path):
            nonexistent_subfolders.append(subfolder)
            if verbose:
                print(f"⚠️  Warning: Subfolder does not exist: {subfolder_path}", flush=True)
            continue

        if not os.path.isdir(subfolder_path):
            if verbose:
                print(f"⚠️  Warning: Path is not a directory: {subfolder_path}", flush=True)
            continue

        # Scan for .m4a files in this subfolder (non-recursive)
        pattern = os.path.join(subfolder_path, "*.m4a")
        subfolder_files = glob.glob(pattern, recursive=False)

        if verbose:
            print(f"🔍 Scanning {subfolder}: found {len(subfolder_files)} file(s)", flush=True)

        for file_path in subfolder_files:
            # Skip iCloud placeholders, temp files, hidden files
            filename = os.path.basename(file_path)
            if ".icloud" in file_path or ".tmp" in file_path or filename.startswith("."):
                if verbose:
                    print(f"⏭️  Skipping: {file_path} (temp/iCloud file)", flush=True)
                continue

            # Check if file is stable (not being synced)
            if not is_file_stable(file_path, wait_seconds=1):
                if verbose:
                    print(f"⏭️  Skipping: {file_path} (still syncing)", flush=True)
                continue

            # Get timestamp
            timestamp = get_audio_timestamp(file_path)
            audio_files.append((file_path, timestamp))

    # Warning summary for nonexistent subfolders
    if nonexistent_subfolders:
        print(f"\n⚠️  Warning: {len(nonexistent_subfolders)} subfolder(s) not found:", flush=True)
        for subfolder in nonexistent_subfolders:
            print(f"   - {subfolder}", flush=True)
        print(f"   Continuing with existing subfolders...\n", flush=True)

    # Sort by timestamp (oldest first)
    audio_files.sort(key=lambda x: x[1])

    return audio_files

def check_processing_status(audio_file, timestamp, transcript_index):
    """
    Check if audio file has been processed.

    Returns: ("complete" | "transcript_only" | "unprocessed", category, transcript_path, analysis_path)
    """
    # Format timestamp as YY-MM-DD HH.MM (matches filename format)
    timestamp_key = timestamp.strftime("%y-%m-%d %H.%M")

    # Look up in index
    if timestamp_key in transcript_index:
        entry = transcript_index[timestamp_key]
        category = entry["category"]
        transcript_path = entry["transcript_path"]
        analysis_path = entry["analysis_path"]

        if analysis_path:
            return ("complete", category, transcript_path, analysis_path)
        else:
            return ("transcript_only", category, transcript_path, None)

    return ("unprocessed", None, None, None)

def process_batch(unprocessed_files, dry_run=False, verbose=False):
    """
    Process list of audio files.

    Returns: {"success": int, "failed": list}
    """
    success_count = 0
    failed_files = []

    total = len(unprocessed_files)

    for i, (audio_path, timestamp) in enumerate(unprocessed_files, 1):
        filename = os.path.basename(audio_path)
        print(f"\n[{i}/{total}] Processing {filename}...", flush=True)

        if dry_run:
            print(f"  📁 Path: {audio_path}", flush=True)
            print(f"  🕐 Timestamp: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
            print(f"  ⚠️  DRY RUN - Would process this file", flush=True)
            success_count += 1
            continue

        # Process the file
        try:
            success = process_audio(audio_path)
            if success:
                success_count += 1
            else:
                failed_files.append(audio_path)
        except Exception as e:
            print(f"❌ Exception processing {filename}: {e}", flush=True)
            failed_files.append(audio_path)

    return {
        "success": success_count,
        "failed": failed_files,
        "total": total
    }

def reprocess_analysis_only(transcript_only_files, dry_run=False, verbose=False):
    """
    For files with transcripts but no analysis: regenerate analysis only.

    Returns: {"success": int, "failed": list}
    """
    success_count = 0
    failed_files = []

    total = len(transcript_only_files)

    for i, (audio_path, timestamp, category, transcript_path) in enumerate(transcript_only_files, 1):
        filename = os.path.basename(audio_path)
        print(f"\n[{i}/{total}] Generating analysis for {filename}...", flush=True)

        if dry_run:
            print(f"  📝 Transcript: {transcript_path}", flush=True)
            print(f"  ⚠️  DRY RUN - Would generate analysis", flush=True)
            success_count += 1
            continue

        try:
            # Read existing transcript
            with open(transcript_path, "r", encoding="utf-8") as f:
                transcript_content = f.read()

            # Generate analysis (skip transcription step)
            print("📊 Analyzing transcript...", flush=True)
            analysis_model = genai.GenerativeModel(ANALYSIS_MODEL)
            analysis_prompt_with_transcript = f"{ANALYSIS_PROMPT}\n\n---TRANSCRIPT TO ANALYZE---\n{transcript_content}"
            analysis_response = analysis_model.generate_content(analysis_prompt_with_transcript)

            # Extract filename from transcript path
            transcript_filename = os.path.basename(transcript_path)

            # Save analysis
            analysis_path = save_analysis(category, transcript_filename, analysis_response.text)
            if analysis_path:
                print(f"✅ Analysis saved: {analysis_path}", flush=True)
                success_count += 1
            else:
                failed_files.append(audio_path)

        except Exception as e:
            print(f"❌ Failed to generate analysis: {e}", flush=True)
            failed_files.append(audio_path)

    return {
        "success": success_count,
        "failed": failed_files,
        "total": total
    }

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Process unprocessed audio recordings on-demand",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python ondemand_transcribe.py --catchup --dry-run    # Preview last 7 days
  python ondemand_transcribe.py --catchup 14           # Process last 14 days
  python ondemand_transcribe.py --catchup --reprocess-partial  # Catchup + fix missing analysis
  python ondemand_transcribe.py --dry-run              # See what would be processed (hardcoded folders)
  python ondemand_transcribe.py                        # Process unprocessed files (hardcoded folders)
        """
    )
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be processed without actually processing")
    parser.add_argument("--reprocess-partial", action="store_true",
                       help="Generate missing analysis for existing transcripts")
    parser.add_argument("--verbose", action="store_true",
                       help="Show detailed progress and file listings")
    parser.add_argument("--catchup", type=int, metavar="DAYS", nargs="?", const=7,
                       help="Auto-discover date folders from last N days (default: 7)")

    args = parser.parse_args()

    print("=" * 60)
    print("📼 On-Demand Audio Transcription & Analysis")
    print("=" * 60)

    # Step 1: Resolve scan subfolders
    if args.catchup is not None:
        folder_paths = discover_recent_folders(WATCH_FOLDER, days_back=args.catchup)
        scan_subfolders = [os.path.basename(p) for p in folder_paths]
        print(f"\n🔄 Catchup mode: scanning last {args.catchup} days", flush=True)
    else:
        scan_subfolders = SCAN_SUBFOLDERS

    # Step 2: Discover audio files
    print(f"🔍 Scanning for audio files in {WATCH_FOLDER}...", flush=True)
    print(f"📁 Target subfolders: {', '.join(scan_subfolders)}", flush=True)
    all_audio_files = discover_audio_files(WATCH_FOLDER, scan_subfolders, verbose=args.verbose)
    print(f"Found {len(all_audio_files)} audio files", flush=True)

    # Step 2: Build transcript index
    print("\n📚 Building transcript index...", flush=True)
    transcript_index = build_transcript_index(FOLDERS)
    print(f"Found {len(transcript_index)} existing transcripts", flush=True)

    # Step 3: Check processing status
    print("\n🔎 Checking processing status...", flush=True)
    unprocessed = []
    transcript_only = []
    complete = 0

    for audio_path, timestamp in all_audio_files:
        status, category, transcript_path, analysis_path = check_processing_status(
            audio_path, timestamp, transcript_index
        )

        if status == "unprocessed":
            unprocessed.append((audio_path, timestamp))
        elif status == "transcript_only":
            transcript_only.append((audio_path, timestamp, category, transcript_path))
        else:  # complete
            complete += 1

    # Step 4: Print status summary
    print("\n" + "=" * 60)
    print("📊 Status Summary")
    print("=" * 60)
    print(f"   ✅ Complete (transcript + analysis):  {complete}")
    print(f"   📝 Transcript only (missing analysis): {len(transcript_only)}")
    print(f"   🆕 Unprocessed:                        {len(unprocessed)}")
    print(f"   📁 Total audio files:                  {len(all_audio_files)}")
    print("=" * 60)

    # Early exit if nothing to do
    if not unprocessed and not transcript_only:
        print("\n✨ All files are fully processed!")
        return

    if args.dry_run:
        print("\n⚠️  DRY RUN MODE - No files will be processed")

    # Step 5: Process unprocessed files
    if unprocessed:
        print(f"\n🚀 Processing {len(unprocessed)} unprocessed files...")
        print("-" * 60)
        results = process_batch(unprocessed, dry_run=args.dry_run, verbose=args.verbose)
        print("\n" + "-" * 60)
        print(f"Unprocessed files results:")
        print(f"  ✅ Success: {results['success']}")
        print(f"  ❌ Failed:  {len(results['failed'])}")
        if results['failed'] and args.verbose:
            print(f"\nFailed files:")
            for f in results['failed']:
                print(f"  - {f}")

    # Step 6: Regenerate missing analysis
    if transcript_only and args.reprocess_partial:
        print(f"\n📊 Generating analysis for {len(transcript_only)} existing transcripts...")
        print("-" * 60)
        results = reprocess_analysis_only(transcript_only, dry_run=args.dry_run, verbose=args.verbose)
        print("\n" + "-" * 60)
        print(f"Partial reprocessing results:")
        print(f"  ✅ Success: {results['success']}")
        print(f"  ❌ Failed:  {len(results['failed'])}")
        if results['failed'] and args.verbose:
            print(f"\nFailed files:")
            for f in results['failed']:
                print(f"  - {f}")
    elif transcript_only and not args.reprocess_partial:
        print(f"\n💡 Tip: {len(transcript_only)} file(s) have transcripts but no analysis.")
        print(f"   Run with --reprocess-partial to generate missing analysis.")

    print("\n" + "=" * 60)
    print("✅ Done!")
    print("=" * 60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Interrupted by user. Exiting...")
        sys.exit(0)