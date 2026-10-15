"""Explicit, validated configuration; this module never loads dotenv files."""

import json
import math
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional


def reject_environment_path(path: Path) -> None:
    """Reject environment-file names in every path component before reading."""
    if any(part.lower().startswith(".env") for part in path.parts):
        raise ValueError("Environment files are not permitted")


@dataclass
class Settings:
    data_dir: str = "data"
    provider: str = "offline"
    base_url: str = ""
    model: str = ""
    api_key_env: str = "RAG_API_KEY"
    max_rounds: int = 6
    max_tool_calls: int = 12
    timeout_seconds: float = 60.0
    request_timeout_seconds: float = 20.0
    max_context_chars: int = 24000
    max_total_tokens: int = 24000
    max_output_tokens: int = 1500

    def __post_init__(self) -> None:
        """Validate direct construction as well as JSON-loaded settings."""
        for name in ("data_dir", "provider", "base_url", "model", "api_key_env"):
            if not isinstance(getattr(self, name), str):
                raise ValueError("{} must be a string".format(name))
        if not self.data_dir.strip() or self.provider not in ("offline", "openai"):
            raise ValueError("Use a data directory and provider offline/openai")
        if not self.api_key_env.isidentifier() or len(self.api_key_env) > 128:
            raise ValueError("api_key_env must be an environment variable name")
        ranges = {"max_rounds": (1, 30), "max_tool_calls": (1, 100),
                  "max_context_chars": (100, 500000), "max_total_tokens": (1, 1000000),
                  "max_output_tokens": (1, 32000)}
        for name, (low, high) in ranges.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("{} is outside its allowed range".format(name))
        for name in ("timeout_seconds", "request_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise ValueError("{} must be a finite number".format(name))
            if not math.isfinite(value) or not 0 < value <= 3600:
                raise ValueError("{} is outside its allowed range".format(name))
        if self.provider == "openai" and (not self.base_url.strip() or not self.model.strip()):
            raise ValueError("OpenAI mode requires base_url and model")


def load_settings(path: Optional[str] = None, **overrides) -> Settings:
    """Read only an explicitly supplied JSON file; resolve its local data path."""
    payload = {}
    base = Path.cwd()
    if path is not None:
        config_path = Path(path)
        reject_environment_path(config_path)
        config_path = config_path.resolve()
        reject_environment_path(config_path)
        if config_path.suffix.lower() != ".json":
            raise ValueError("Configuration must be an explicit JSON file")
        if config_path.stat().st_size > 65536:
            raise ValueError("Configuration file is too large")
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("Configuration must be a JSON object")
        base = config_path.parent
    payload.update(overrides)
    allowed = {item.name for item in fields(Settings)}
    if set(payload) - allowed:
        raise ValueError("Unknown configuration fields; API keys are not allowed")
    settings = Settings(**payload)
    data_path = Path(settings.data_dir)
    if not data_path.is_absolute():
        settings.data_dir = str((base / data_path).resolve())
    return settings
