from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = APP_DIR / ".reporting_assistant"
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass(slots=True)
class AppConfig:
    reports_dir: str = str(APP_DIR / "Reportes")
    default_report_id: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-5.2"
    transcription_model: str = "gpt-4o-mini-transcribe"
    popup_hour_mon_wed: str = "18:00"
    popup_hour_thu_fri: str = "17:30"
    default_rest_minutes: int = 60
    recorder_sample_rate: int = 16000
    country_holidays: str = "CO"
    preferred_service_mode: str = "remoto"
    log_file: str = str(CONFIG_DIR / "assistant.log")
    extra: dict[str, str] = field(default_factory=dict)


def ensure_config_dir() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def load_config() -> AppConfig:
    ensure_config_dir()
    if not CONFIG_PATH.exists():
        config = AppConfig()
        save_config(config)
        return config

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config = AppConfig(**data)
    if not config.openai_api_key:
        config.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    return config


def save_config(config: AppConfig) -> None:
    ensure_config_dir()
    CONFIG_PATH.write_text(
        json.dumps(asdict(config), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
