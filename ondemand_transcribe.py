"""On-demand batch processing CLI for catchup after downtime.

Usage:
    python ondemand_transcribe.py --catchup --dry-run       # Preview last 7 days
    python ondemand_transcribe.py --catchup 14              # Process last 14 days
    python ondemand_transcribe.py --catchup --reprocess-partial  # Fix missing analysis
"""

import argparse
import glob
import os
import sys

from config import DELAY_BETWEEN_FILES, FOLDERS, WATCH_FOLDER
from pipeline import (
    TIMESTAMP_FORMAT,
    FatalAPIError,
    analyze_with_retry,
    build_transcript_index,
    configure_gemini,
    configure_ollama,
    discover_recent_folders,
    get_audio_timestamp,
    is_file_stable,
    load_state,
    process_audio,
    save_analysis,
)

# ============================================================================
# DISCOVERY (on-demand specific: explicit subfolder list, no state filtering)
# ============================================================================


def discover_audio_files(watch_folder, scan_subfolders, verbose=False):
    """Scan specific subfolders for .m4a files. Returns list of (path, timestamp) tuples."""
    if not scan_subfolders:
        raise ValueError(
            "scan_subfolders must contain at least one subfolder. Use --catchup to auto-discover date folders."
        )

    audio_files = []
    nonexistent = []

    for subfolder in scan_subfolders:
        subfolder_path = os.path.join(watch_folder, subfolder)

        if not os.path.isdir(subfolder_path):
            nonexistent.append(subfolder)
            if verbose:
                print(f"⚠️  Warning: Not found: {subfolder_path}", flush=True)
            continue

        pattern = os.path.join(subfolder_path, "*.m4a")
        for file_path in glob.glob(pattern):
            basename = os.path.basename(file_path)
            if ".icloud" in file_path or ".tmp" in file_path or basename.startswith("."):
                continue
            if not is_file_stable(file_path, wait_seconds=1):
                continue
            audio_files.append((file_path, get_audio_timestamp(file_path)))

    if nonexistent:
        print(f"\n⚠️  {len(nonexistent)} subfolder(s) not found: {', '.join(nonexistent)}", flush=True)

    audio_files.sort(key=lambda x: x[1])
    return audio_files


def check_processing_status(audio_file, timestamp, transcript_index):
    """Check if audio file has been processed.

    Returns: (status, category, transcript_path, analysis_path)
    where status is "complete" | "transcript_only" | "unprocessed"
    """
    timestamp_key = timestamp.strftime(TIMESTAMP_FORMAT)

    if timestamp_key in transcript_index:
        entry = transcript_index[timestamp_key]
        if entry["analysis_path"]:
            return ("complete", entry["category"], entry["transcript_path"], entry["analysis_path"])
        return ("transcript_only", entry["category"], entry["transcript_path"], None)

    return ("unprocessed", None, None, None)


# ============================================================================
# BATCH PROCESSING
# ============================================================================


def process_batch(unprocessed_files, state, dry_run=False):
    """Process list of audio files. Returns {"success": int, "failed": list}."""
    success_count = 0
    failed_files = []
    total = len(unprocessed_files)

    for i, (audio_path, timestamp) in enumerate(unprocessed_files, 1):
        filename = os.path.basename(audio_path)
        print(f"\n[{i}/{total}] Processing {filename}...", flush=True)

        if dry_run:
            print(f"  📁 Path: {audio_path}", flush=True)
            print(f"  🕐 Timestamp: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
            print("  ⚠️  DRY RUN - Would process this file", flush=True)
            success_count += 1
            continue

        try:
            success, category = process_audio(audio_path, timestamp, state)
            if success:
                success_count += 1
            else:
                failed_files.append(audio_path)
        except FatalAPIError as e:
            print(f"\n🛑 FATAL: {e}", flush=True)
            failed_files.append(audio_path)
            break
        except Exception as e:
            print(f"❌ Exception processing {filename}: {e}", flush=True)
            failed_files.append(audio_path)

        # Rate limiting between files
        if i < total and not dry_run:
            import time

            print(f"   ⏸️  Pausing {DELAY_BETWEEN_FILES}s before next file...", flush=True)
            time.sleep(DELAY_BETWEEN_FILES)

    return {"success": success_count, "failed": failed_files, "total": total}


def reprocess_analysis_only(transcript_only_files, dry_run=False):
    """Regenerate analysis for files with transcripts but no analysis."""
    success_count = 0
    failed_files = []
    total = len(transcript_only_files)

    for i, (audio_path, timestamp, category, transcript_path) in enumerate(transcript_only_files, 1):
        filename = os.path.basename(audio_path)
        print(f"\n[{i}/{total}] Generating analysis for {filename}...", flush=True)

        if dry_run:
            print(f"  📝 Transcript: {transcript_path}", flush=True)
            print("  ⚠️  DRY RUN - Would generate analysis", flush=True)
            success_count += 1
            continue

        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                transcript_content = f.read()

            result = analyze_with_retry(transcript_content)

            if result:
                _, _, analysis_text = result  # category/filename from disk — don't relocate
                transcript_filename = os.path.basename(transcript_path)
                analysis_path = save_analysis(category, transcript_filename, analysis_text)
                if analysis_path:
                    print(f"✅ Analysis saved: {analysis_path}", flush=True)
                    success_count += 1
                else:
                    failed_files.append(audio_path)
            else:
                failed_files.append(audio_path)

        except FatalAPIError as e:
            print(f"\n🛑 FATAL: {e}", flush=True)
            failed_files.append(audio_path)
            break
        except Exception as e:
            print(f"❌ Failed to generate analysis: {e}", flush=True)
            failed_files.append(audio_path)

        # Rate limiting
        if i < total and not dry_run:
            import time

            print(f"   ⏸️  Pausing {DELAY_BETWEEN_FILES}s before next analysis...", flush=True)
            time.sleep(DELAY_BETWEEN_FILES)

    return {"success": success_count, "failed": failed_files, "total": total}


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Process unprocessed audio recordings on-demand",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python ondemand_transcribe.py --catchup --dry-run          # Preview last 7 days
  python ondemand_transcribe.py --catchup 14                 # Process last 14 days
  python ondemand_transcribe.py --catchup --reprocess-partial  # Fix missing analysis
        """,
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be processed without actually processing"
    )
    parser.add_argument(
        "--reprocess-partial", action="store_true", help="Generate missing analysis for existing transcripts"
    )
    parser.add_argument("--verbose", action="store_true", help="Show detailed progress")
    parser.add_argument(
        "--catchup",
        type=int,
        metavar="DAYS",
        nargs="?",
        const=7,
        help="Auto-discover date folders from last N days (default: 7)",
    )

    args = parser.parse_args()

    if args.catchup is None:
        parser.print_help()
        print("\n⚠️  Please specify --catchup [DAYS] to auto-discover folders.")
        sys.exit(1)

    configure_gemini()
    configure_ollama()

    print("=" * 60)
    print("📼 On-Demand Audio Transcription & Analysis")
    print("=" * 60)

    # Discover folders and audio files
    folder_paths = discover_recent_folders(WATCH_FOLDER, days_back=args.catchup)
    scan_subfolders = [os.path.basename(p) for p in folder_paths]
    print(f"\n🔄 Catchup mode: scanning last {args.catchup} days", flush=True)
    print(f"📁 Target subfolders: {', '.join(scan_subfolders)}", flush=True)

    if not scan_subfolders:
        print("No date folders found in the specified range.", flush=True)
        return

    all_audio_files = discover_audio_files(WATCH_FOLDER, scan_subfolders, verbose=args.verbose)
    print(f"Found {len(all_audio_files)} audio files", flush=True)

    # Build index and load state
    print("\n📚 Building transcript index...", flush=True)
    transcript_index = build_transcript_index(FOLDERS)
    print(f"Found {len(transcript_index)} existing transcripts", flush=True)

    state = load_state()

    # Check processing status
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
        else:
            complete += 1

    # Summary
    print(f"\n{'=' * 60}")
    print("📊 Status Summary")
    print(f"{'=' * 60}")
    print(f"   ✅ Complete (transcript + analysis):  {complete}")
    print(f"   📝 Transcript only (missing analysis): {len(transcript_only)}")
    print(f"   🆕 Unprocessed:                        {len(unprocessed)}")
    print(f"   📁 Total audio files:                  {len(all_audio_files)}")
    print(f"{'=' * 60}")

    if not unprocessed and not transcript_only:
        print("\n✨ All files are fully processed!")
        return

    if args.dry_run:
        print("\n⚠️  DRY RUN MODE - No files will be processed")

    # Process unprocessed files
    if unprocessed:
        print(f"\n🚀 Processing {len(unprocessed)} unprocessed files...")
        print("-" * 60)
        results = process_batch(unprocessed, state, dry_run=args.dry_run)
        print(f"\n{'-' * 60}")
        print(f"  ✅ Success: {results['success']}")
        print(f"  ❌ Failed:  {len(results['failed'])}")

    # Regenerate missing analysis
    if transcript_only and args.reprocess_partial:
        print(f"\n📊 Generating analysis for {len(transcript_only)} existing transcripts...")
        print("-" * 60)
        results = reprocess_analysis_only(transcript_only, dry_run=args.dry_run)
        print(f"\n{'-' * 60}")
        print(f"  ✅ Success: {results['success']}")
        print(f"  ❌ Failed:  {len(results['failed'])}")
    elif transcript_only:
        print(f"\n💡 Tip: {len(transcript_only)} file(s) have transcripts but no analysis.")
        print("   Run with --reprocess-partial to generate missing analysis.")

    print(f"\n{'=' * 60}")
    print("✅ Done!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Interrupted by user. Exiting...")
        sys.exit(0)
