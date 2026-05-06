from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = APP_DIR / ".reporting_assistant"
CONFIG_PATH = CONFIG_DIR / "config.json"
DEFAULT_CLIENTS_ROOT = Path.home() / "Desktop" / "CLIENTES"


@dataclass(slots=True)
class AppConfig:
    clients_root: str = str(DEFAULT_CLIENTS_ROOT)
    default_client: str = ""
    default_report_id: str = ""
    template_path: str = ""
    popup_hour_mon_wed: str = "18:00"
    popup_hour_thu_fri: str = "17:30"
    default_rest_minutes: int = 60
    country_holidays: str = "CO"
    preferred_service_mode: str = "remoto"
    log_file: str = str(CONFIG_DIR / "assistant.log")
    extra: dict[str, str] = field(default_factory=dict)


def ensure_config_dir() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def _normalize_config(config: AppConfig) -> AppConfig:
    if not config.clients_root:
        config.clients_root = str(DEFAULT_CLIENTS_ROOT)
    return config


def load_config() -> AppConfig:
    ensure_config_dir()
    if not CONFIG_PATH.exists():
        config = _normalize_config(AppConfig())
        save_config(config)
        return config

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if "clients_root" not in data and data.get("reports_dir"):
        reports_dir = Path(data["reports_dir"])
        if reports_dir.name.lower() == "avance de servicio":
            data["clients_root"] = str(reports_dir.parent.parent)
        else:
            data["clients_root"] = str(DEFAULT_CLIENTS_ROOT)
    allowed = {field for field in AppConfig.__dataclass_fields__}
    cleaned = {key: value for key, value in data.items() if key in allowed}
    config = _normalize_config(AppConfig(**cleaned))
    return config


def save_config(config: AppConfig) -> None:
    ensure_config_dir()
    normalized = _normalize_config(config)
    CONFIG_PATH.write_text(
        json.dumps(asdict(normalized), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
