"""Smoke test to verify test infrastructure works."""


def test_tmp_output_dir_fixture(tmp_output_dir):
    """Verify the tmp_output_dir fixture creates the expected structure."""
    categories = ["WORK", "PERSONAL", "DEFAULT"]
    for cat in categories:
        assert (tmp_output_dir / cat / "transcripts").is_dir()
        assert (tmp_output_dir / cat / "analysis").is_dir()


def test_sample_state_fixture(sample_state):
    """Verify the sample_state fixture has expected structure."""
    assert "processed" in sample_state
    assert len(sample_state["processed"]) == 1
    entry = list(sample_state["processed"].values())[0]
    assert entry["status"] == "complete"
    assert entry["category"] == "WORK"


def test_state_file_fixture(state_file):
    """Verify the state_file fixture creates a readable JSON file."""
    import json
    data = json.loads(state_file.read_text())
    assert "processed" in data


def test_mock_gemini_response_fixture(mock_gemini_response):
    """Verify the mock_gemini_response factory produces valid format."""
    response = mock_gemini_response()
    assert "CATEGORY: WORK" in response
    assert "FILENAME: Test Meeting - Topics" in response
    assert "---TRANSCRIPT---" in response
    assert "This is a test transcript." in response


def test_mock_gemini_response_custom(mock_gemini_response):
    """Verify the factory accepts custom arguments."""
    response = mock_gemini_response(
        category="PERSONAL",
        filename="Custom Name",
        transcript="Custom text"
    )
    assert "CATEGORY: PERSONAL" in response
    assert "FILENAME: Custom Name" in response
    assert "Custom text" in response


def test_no_api_calls_fixture(monkeypatch):
    """Verify the autouse no_api_calls fixture sets the env var."""
    import os
    assert os.environ.get("GEMINI_API_KEY") == "test-key-not-real"
