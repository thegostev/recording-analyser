#!/usr/bin/env python3
"""
Reclassify and Fix MeetingTranscriber Files

This script performs two maintenance tasks:
1. Generate missing analysis files for existing transcripts
2. Re-classify "Unknown Meeting" files and move them to correct category folders

Usage:
    python reclassify_and_fix.py --generate-missing-analysis [--dry-run] [--verbose]
    python reclassify_and_fix.py --reclassify [--dry-run] [--verbose]
    python reclassify_and_fix.py --generate-missing-analysis --reclassify [--dry-run]
"""

import os
import sys
import argparse
import shutil
import re
import time
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

# Import from existing scripts
from auto_transcribe import (
    FOLDERS,
    TRANSCRIPTION_MODEL,
    ANALYSIS_MODEL,
    TRANSCRIPTION_PROMPT,
    ANALYSIS_PROMPT,
    API_TIMEOUT,
    parse_transcription_response,
    save_analysis,
    genai
)
from google.generativeai.types import RequestOptions

# --- HELPER FUNCTIONS ---

def find_missing_analysis(folders, verbose=False):
    """
    Scan all category folders, return list of transcripts without analysis.

    Returns: List of (transcript_path, category) tuples
    """
    missing = []

    for category, base_path in folders.items():
        transcripts_folder = os.path.join(base_path, "transcripts")
        analysis_folder = os.path.join(base_path, "analysis")

        if not os.path.exists(transcripts_folder):
            continue

        # Get all transcript files
        transcript_files = []
        for f in os.listdir(transcripts_folder):
            if f.endswith(".md"):
                transcript_files.append(f)

        # Check each transcript for corresponding analysis
        for transcript_file in transcript_files:
            # Analysis filename is: [transcript_name] - Analysis.md
            analysis_file = transcript_file.replace(".md", " - Analysis.md")
            analysis_path = os.path.join(analysis_folder, analysis_file)

            if not os.path.exists(analysis_path):
                transcript_path = os.path.join(transcripts_folder, transcript_file)
                missing.append((transcript_path, category))

                if verbose:
                    print(f"  Missing analysis: {category}/{transcript_file}", flush=True)

    return missing


def generate_missing_analysis(transcript_path, category, dry_run=False, verbose=False):
    """
    Generate analysis for a single transcript.

    Returns: success/failure status
    """
    filename = os.path.basename(transcript_path)

    if dry_run:
        print(f"  [DRY RUN] Would generate analysis for: {category}/{filename}", flush=True)
        return True

    try:
        # Read existing transcript
        with open(transcript_path, "r", encoding="utf-8") as f:
            transcript_content = f.read()

        if verbose:
            print(f"  Generating analysis for: {filename}...", flush=True)

        # Generate analysis (skip transcription step)
        analysis_model = genai.GenerativeModel(ANALYSIS_MODEL)
        analysis_prompt_with_transcript = f"{ANALYSIS_PROMPT}\n\n---TRANSCRIPT TO ANALYZE---\n{transcript_content}"
        analysis_response = analysis_model.generate_content(
            analysis_prompt_with_transcript,
            request_options=RequestOptions(timeout=API_TIMEOUT),
        )

        # Save analysis
        analysis_path = save_analysis(category, filename, analysis_response.text)
        if analysis_path:
            if verbose:
                print(f"  ✅ Analysis saved: {analysis_path}", flush=True)
            return True
        else:
            print(f"  ❌ Failed to save analysis for: {filename}", flush=True)
            return False

    except Exception as e:
        print(f"  ❌ Error generating analysis for {filename}: {e}", flush=True)
        return False


def should_update_filename(filename):
    """
    Check if filename contains 'Unknown Meeting'.
    Only regenerate titles for these files.

    Returns: True if filename contains 'Unknown Meeting'
    """
    return 'Unknown Meeting' in filename


def reclassify_transcript(transcript_path, dry_run=False, verbose=False):
    """
    Re-classify a transcript and determine correct category.

    Returns: (new_category, new_filename) or None if classification fails
    """
    filename = os.path.basename(transcript_path)

    try:
        # Read transcript content
        with open(transcript_path, "r", encoding="utf-8") as f:
            transcript_content = f.read()

        if verbose:
            print(f"  Classifying: {filename}...", flush=True)

        if dry_run:
            print(f"  [DRY RUN] Would classify transcript: {filename}", flush=True)
            # In dry-run, we still classify to show what would happen
            # but don't actually move files

        # Call classification model
        transcription_model = genai.GenerativeModel(TRANSCRIPTION_MODEL)

        # Use classification prompt with transcript text
        classification_prompt = f"""{TRANSCRIPTION_PROMPT}

---TRANSCRIPT TO CLASSIFY---
{transcript_content}

Please classify this transcript and provide:
1. CATEGORY (PERSONLIG/MINNESOTERE/MUSIKKERE/INTERVJUER/DEFAULT)
2. FILENAME in format: [Meeting Name] - [Topic 1, Topic 2, Topic 3]
"""

        response = transcription_model.generate_content(
            classification_prompt,
            request_options=RequestOptions(timeout=API_TIMEOUT),
        )
        response_text = response.text

        # Parse response to extract CATEGORY and FILENAME
        category, suggested_filename, _ = parse_transcription_response(response_text)

        if verbose:
            print(f"  → Classified as: {category} | {suggested_filename}", flush=True)

        return (category, suggested_filename)

    except Exception as e:
        print(f"  ❌ Error classifying {filename}: {e}", flush=True)
        return None


def extract_timestamp(filename):
    """
    Extract timestamp prefix from filename (YY-MM-DD HH.MM format).

    Returns: timestamp string or None
    """
    match = re.match(r'^(\d{2}-\d{2}-\d{2}\s+\d{2}\.\d{2})', filename)
    if match:
        return match.group(1)
    return None


def move_transcript_and_analysis(old_transcript_path, new_category, new_filename, dry_run=False, verbose=False):
    """
    Move both transcript and analysis files to new category folder.

    Returns: success/failure status
    """
    old_filename = os.path.basename(old_transcript_path)

    # Extract timestamp from old filename
    timestamp = extract_timestamp(old_filename)
    if not timestamp:
        print(f"  ❌ Could not extract timestamp from: {old_filename}", flush=True)
        return False

    # Build new filename with timestamp
    new_full_filename = f"{timestamp} - {new_filename}.md" if not new_filename.endswith('.md') else f"{timestamp} - {new_filename}"

    # Get destination folder
    dest_folder = FOLDERS.get(new_category, FOLDERS["DEFAULT"])
    transcripts_folder = os.path.join(dest_folder, "transcripts")
    analysis_folder = os.path.join(dest_folder, "analysis")

    # Build new paths
    new_transcript_path = os.path.join(transcripts_folder, new_full_filename)
    new_analysis_filename = new_full_filename.replace(".md", " - Analysis.md")
    new_analysis_path = os.path.join(analysis_folder, new_analysis_filename)

    # Check for collisions
    if os.path.exists(new_transcript_path):
        print(f"  ⚠️  Warning: File already exists at destination: {new_transcript_path}", flush=True)
        # Add numeric suffix
        base, ext = os.path.splitext(new_full_filename)
        counter = 2
        while os.path.exists(os.path.join(transcripts_folder, f"{base} ({counter}){ext}")):
            counter += 1
        new_full_filename = f"{base} ({counter}){ext}"
        new_transcript_path = os.path.join(transcripts_folder, new_full_filename)
        new_analysis_filename = new_full_filename.replace(".md", " - Analysis.md")
        new_analysis_path = os.path.join(analysis_folder, new_analysis_filename)

    # Check if analysis file exists
    old_analysis_path = old_transcript_path.replace("/transcripts/", "/analysis/").replace(".md", " - Analysis.md")
    has_analysis = os.path.exists(old_analysis_path)

    if dry_run:
        print(f"  [DRY RUN] Would move:", flush=True)
        print(f"    FROM: {old_transcript_path}", flush=True)
        print(f"    TO:   {new_transcript_path}", flush=True)
        if has_analysis:
            print(f"    AND:  {old_analysis_path}", flush=True)
            print(f"    TO:   {new_analysis_path}", flush=True)
        return True

    try:
        # Create destination folders if needed
        os.makedirs(transcripts_folder, exist_ok=True)
        os.makedirs(analysis_folder, exist_ok=True)

        # Move transcript
        shutil.move(old_transcript_path, new_transcript_path)
        if verbose:
            print(f"  ✅ Moved transcript: {new_full_filename}", flush=True)

        # Move analysis if it exists
        if has_analysis:
            shutil.move(old_analysis_path, new_analysis_path)
            if verbose:
                print(f"  ✅ Moved analysis: {new_analysis_filename}", flush=True)
        elif verbose:
            print(f"  ⚠️  No analysis file to move", flush=True)

        return True

    except Exception as e:
        print(f"  ❌ Error moving files: {e}", flush=True)
        # Attempt rollback if partial failure
        try:
            if os.path.exists(new_transcript_path) and not os.path.exists(old_transcript_path):
                shutil.move(new_transcript_path, old_transcript_path)
                print(f"  🔄 Rolled back transcript move", flush=True)
        except:
            pass
        return False


def scan_default_folder(verbose=False):
    """
    Scan DEFAULT folder for transcript files.

    Returns: List of transcript paths
    """
    transcripts = []
    default_folder = FOLDERS['DEFAULT']
    transcripts_folder = os.path.join(default_folder, "transcripts")

    if not os.path.exists(transcripts_folder):
        print(f"⚠️  DEFAULT transcripts folder not found: {transcripts_folder}", flush=True)
        return transcripts

    for filename in os.listdir(transcripts_folder):
        if filename.endswith(".md"):
            transcript_path = os.path.join(transcripts_folder, filename)
            transcripts.append(transcript_path)

    if verbose:
        print(f"Found {len(transcripts)} transcripts in DEFAULT folder", flush=True)

    return transcripts


# --- MAIN ---

def main():
    parser = argparse.ArgumentParser(description="Reclassify and fix MeetingTranscriber files")
    parser.add_argument('--generate-missing-analysis', action='store_true',
                       help='Generate missing analysis files')
    parser.add_argument('--reclassify', action='store_true',
                       help='Reclassify and move Unknown Meeting files')
    parser.add_argument('--dry-run', action='store_true',
                       help='Preview changes without executing')
    parser.add_argument('--verbose', action='store_true',
                       help='Show detailed progress')

    args = parser.parse_args()

    # At least one operation must be selected
    if not args.generate_missing_analysis and not args.reclassify:
        parser.print_help()
        print("\n⚠️  Please specify at least one operation: --generate-missing-analysis or --reclassify")
        sys.exit(1)

    print("="*60)
    print("🔧 MeetingTranscriber Maintenance")
    print("="*60)

    if args.dry_run:
        print("⚠️  DRY RUN MODE - No files will be modified\n")

    # Task 1: Generate Missing Analysis
    if args.generate_missing_analysis:
        print("\n📊 Task 1: Generating Missing Analysis Files")
        print("-"*60)

        missing = find_missing_analysis(FOLDERS, verbose=args.verbose)

        if not missing:
            print("✅ No missing analysis files found!")
        else:
            print(f"Found {len(missing)} transcripts without analysis\n")

            success_count = 0
            failed_count = 0

            for i, (transcript_path, category) in enumerate(missing, 1):
                filename = os.path.basename(transcript_path)
                print(f"[{i}/{len(missing)}] {category}/{filename}")

                if generate_missing_analysis(transcript_path, category, args.dry_run, args.verbose):
                    success_count += 1
                else:
                    failed_count += 1

                # Respect pro-tier quota recovery window between analysis calls
                if i < len(missing) and not args.dry_run:
                    print(f"  ⏸️  Pausing 90s before next analysis (pro-tier quota)...", flush=True)
                    time.sleep(90)

            print(f"\n📊 Results:")
            print(f"  ✅ Success: {success_count}")
            print(f"  ❌ Failed:  {failed_count}")

    # Task 2: Reclassify and Move
    if args.reclassify:
        print("\n📁 Task 2: Reclassifying and Moving Files")
        print("-"*60)

        transcripts = scan_default_folder(verbose=args.verbose)
        unknown_meetings = [t for t in transcripts if should_update_filename(os.path.basename(t))]

        if not unknown_meetings:
            print("✅ No 'Unknown Meeting' files found in DEFAULT folder!")
        else:
            print(f"Found {len(unknown_meetings)} 'Unknown Meeting' files\n")

            moved_count = 0
            skipped_count = 0
            failed_count = 0

            for i, transcript_path in enumerate(unknown_meetings, 1):
                filename = os.path.basename(transcript_path)
                print(f"[{i}/{len(unknown_meetings)}] {filename}")

                # Classify transcript
                result = reclassify_transcript(transcript_path, args.dry_run, args.verbose)

                if result is None:
                    print(f"  ❌ Classification failed")
                    failed_count += 1
                    continue

                new_category, new_filename = result

                # Skip if still DEFAULT
                if new_category == 'DEFAULT':
                    print(f"  ⚠️  Still classified as DEFAULT, skipping move")
                    skipped_count += 1
                    continue

                # Move files
                if move_transcript_and_analysis(transcript_path, new_category, new_filename, args.dry_run, args.verbose):
                    moved_count += 1
                else:
                    failed_count += 1

            print(f"\n📊 Results:")
            print(f"  ✅ Moved:   {moved_count}")
            print(f"  ⚠️  Skipped: {skipped_count}")
            print(f"  ❌ Failed:  {failed_count}")

    print("\n" + "="*60)
    print("✅ Done!")
    print("="*60)


if __name__ == "__main__":
    main()
