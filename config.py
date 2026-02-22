"""Configuration loader for MeetingTranscriber.

Loads settings from config.yaml in the project directory.
Falls back to GEMINI_API_KEY environment variable for the API key.
"""

import os
import sys
from pathlib import Path

import yaml

# ---------- Locate config file ----------
_CONFIG_DIR = Path(__file__).parent
_CONFIG_FILE = _CONFIG_DIR / "config.yaml"


def load_config(config_path: Path = _CONFIG_FILE) -> dict:
    """Load configuration from YAML file.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Configuration dictionary with all settings.
    """
    if not config_path.exists():
        print(
            f"ERROR: Config file not found: {config_path}\n"
            f"Copy config.example.yaml to config.yaml and fill in your values.",
            flush=True,
        )
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # API key: config file first, then env var
    api_key = cfg.get("api_key") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "ERROR: No API key found. "
            "Set 'api_key' in config.yaml or GEMINI_API_KEY environment variable.",
            flush=True,
        )
        sys.exit(1)
    cfg["api_key"] = api_key

    # Expand ~ in path values
    cfg["watch_folder"] = os.path.expanduser(cfg["watch_folder"])
    cfg["state_file"] = os.path.expanduser(
        cfg.get("state_file", "~/.meeting_transcriber_state.json")
    )
    cfg["failed_analysis_log"] = os.path.expanduser(
        cfg.get("failed_analysis_log", "failed_analysis.log")
    )

    for cat in cfg.get("folders", {}):
        cfg["folders"][cat] = os.path.expanduser(cfg["folders"][cat])

    return cfg


# ---------- Load once at import time ----------
_cfg = load_config()

# ---------- Export as module-level constants ----------
API_KEY: str = _cfg["api_key"]
TRANSCRIPTION_MODEL: str = _cfg.get("transcription_model", "gemini-3-flash-preview")
ANALYSIS_MODEL: str = _cfg.get("analysis_model", "gemini-3-pro-preview")

WATCH_FOLDER: str = _cfg["watch_folder"]
FOLDERS: dict[str, str] = _cfg["folders"]
STATE_FILE: str = _cfg["state_file"]
FAILED_ANALYSIS_LOG: str = _cfg["failed_analysis_log"]

TRANSCRIPTION_PROMPT: str = _cfg["transcription_prompt"]
ANALYSIS_PROMPT: str = _cfg["analysis_prompt"]

# Service behavior
SCAN_INTERVAL: int = _cfg.get("scan_interval", 30)
SCAN_DAYS_BACK: int = _cfg.get("scan_days_back", 7)
DELAY_BETWEEN_FILES: int = _cfg.get("delay_between_files", 90)
MAX_RETRIES: int = _cfg.get("max_retries", 3)
MAX_FILES_PER_CYCLE: int = _cfg.get("max_files_per_cycle", 5)
RETRY_BACKOFF: list[int] = _cfg.get("retry_backoff", [10, 30, 60])
ANALYSIS_RETRY_BACKOFF: list[int] = _cfg.get("analysis_retry_backoff", [60, 180, 300])
API_TIMEOUT: int = _cfg.get("api_timeout", 300)
