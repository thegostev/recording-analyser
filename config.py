"""Configuration loader for MeetingTranscriber.

Loads settings from config.yaml. API key comes from GEMINI_API_KEY env var only.
"""

import os
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------- Locate config file ----------
_CONFIG_DIR = Path(__file__).parent
_CONFIG_FILE = _CONFIG_DIR / "config.yaml"


def load_config(config_path: Path = _CONFIG_FILE) -> dict[str, Any]:
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
        cfg: dict[str, Any] = yaml.safe_load(f)

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("ERROR: GEMINI_API_KEY environment variable is not set.", flush=True)
        sys.exit(1)
    cfg["api_key"] = api_key

    cfg["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")

    # Expand ~ in path values
    cfg["watch_folder"] = os.path.expanduser(cfg["watch_folder"])
    cfg["state_file"] = os.path.expanduser(cfg.get("state_file", "~/.meeting_transcriber_state.json"))
    cfg["failed_analysis_log"] = os.path.expanduser(cfg.get("failed_analysis_log", "failed_analysis.log"))

    for cat in cfg.get("folders", {}):
        cfg["folders"][cat] = os.path.expanduser(cfg["folders"][cat])

    return cfg


# ---------- Load once at import time ----------
_cfg = load_config()

# ---------- Export as module-level constants ----------
API_KEY: str = _cfg["api_key"]
TRANSCRIPTION_MODEL: str = _cfg.get("transcription_model", "gemini-2.5-flash")
TRANSCRIPTION_PROMPT: str = _cfg["transcription_prompt"]
ANALYSIS_PROVIDER: str = _cfg.get("analysis_provider", "ollama")
OLLAMA_MODEL: str = _cfg.get("ollama_model", "qwen3.5:9b")
OLLAMA_HOST: str = _cfg.get("ollama_host", "http://localhost:11434")
OLLAMA_THINKING: bool = _cfg.get("ollama_thinking", False)
OLLAMA_TIMEOUT: int = _cfg.get("ollama_timeout", 600)
OLLAMA_NUM_CTX: int = _cfg.get("ollama_num_ctx", 16384)
OLLAMA_KEEP_ALIVE: int = _cfg.get("ollama_keep_alive", 300)
WHISPER_BACKEND: str = _cfg.get("whisper_backend", "parakeet")
WHISPER_MODEL: str = _cfg.get("whisper_model", "mlx-community/parakeet-tdt-0.6b-v2")
WHISPER_FALLBACK_MODEL: str = _cfg.get("whisper_fallback_model", "mlx-community/whisper-large-v3-turbo")
WHISPER_DEVICE: str = _cfg.get("whisper_device", "cpu")
WHISPER_COMPUTE_TYPE: str = _cfg.get("whisper_compute_type", "int8")

WATCH_FOLDER: str = _cfg["watch_folder"]
FOLDERS: dict[str, str] = _cfg["folders"]
STATE_FILE: str = _cfg["state_file"]
FAILED_ANALYSIS_LOG: str = _cfg["failed_analysis_log"]

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
