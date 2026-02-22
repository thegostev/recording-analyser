"""Shared test fixtures for MeetingTranscriber."""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Create a test config.yaml BEFORE config.py is imported by any test module.
# config.py loads at import time, so the file must exist first.
_PROJECT_DIR = Path(__file__).parent.parent
_TEST_CONFIG = _PROJECT_DIR / "config.yaml"

if not _TEST_CONFIG.exists():
    import yaml

    _test_cfg = {
        "api_key": "test-key-not-real",
        "transcription_model": "gemini-3-flash-preview",
        "analysis_model": "gemini-3-pro-preview",
        "watch_folder": "/tmp/test-watch-folder",
        "folders": {
            "WORK": "/tmp/test-work",
            "PERSONAL": "/tmp/test-personal",
            "DEFAULT": "/tmp/test-default",
        },
        "state_file": "/tmp/test-state.json",
        "failed_analysis_log": "/tmp/test-failed.log",
        "transcription_prompt": "Test transcription prompt.",
        "analysis_prompt": "Test analysis prompt.",
    }
    _TEST_CONFIG.write_text(yaml.dump(_test_cfg))
    _CREATED_TEST_CONFIG = True
else:
    _CREATED_TEST_CONFIG = False


@pytest.fixture
def tmp_output_dir(tmp_path):
    """Temporary directory structure mimicking Obsidian vault output."""
    categories = ["WORK", "PERSONAL", "DEFAULT"]
    for cat in categories:
        (tmp_path / cat / "transcripts").mkdir(parents=True)
        (tmp_path / cat / "analysis").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def sample_state():
    """Sample processing state dict."""
    return {
        "processed": {
            "/path/to/test_audio.m4a": {
                "status": "complete",
                "category": "WORK",
                "timestamp": "2026-02-21T14:30:00",
                "processed_at": "2026-02-21T14:35:00",
                "attempts": 1,
            }
        }
    }


@pytest.fixture
def state_file(tmp_path, sample_state):
    """Temporary state file with sample data."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(sample_state))
    return state_path


@pytest.fixture
def mock_gemini_response():
    """Factory fixture for mock Gemini API responses."""

    def _make_response(category="WORK", filename="Test Meeting - Topics",
                       transcript="This is a test transcript."):
        text = (
            f"CATEGORY: {category}\n"
            f"FILENAME: {filename}\n"
            f"---TRANSCRIPT---\n"
            f"{transcript}"
        )
        return text

    return _make_response


@pytest.fixture(autouse=True)
def no_api_calls(monkeypatch):
    """Prevent any real Gemini API calls during tests."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
