from __future__ import annotations

import os
from pathlib import Path

import keyring
import yaml
from pydantic import BaseModel, Field

APP_DIR = Path.home() / ".config" / "personal-ai-coach"
CONFIG_PATH = APP_DIR / "config.yaml"
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "coach.db"
SESSION_DIR = APP_DIR / "garmin_session"
CONTEXT_FILE = Path("training_context.md")

KEYRING_SERVICE = "personal-ai-coach"
KEYRING_EMAIL_KEY = "garmin_email"
KEYRING_PASSWORD_KEY = "garmin_password"


class AthleteConfig(BaseModel):
    race_date: str = ""
    race_name: str = ""
    experience: str = "first_timer"
    max_weekly_hours: float = 12.0
    injury_history: list[str] = Field(default_factory=list)
    goals: str = ""


class SyncConfig(BaseModel):
    lookback_days: int = 90


class AppConfig(BaseModel):
    athlete: AthleteConfig = Field(default_factory=AthleteConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    ensure_dirs()
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
        data.pop("garmin", None)
        return AppConfig.model_validate(data)
    return AppConfig()


def save_config(config: AppConfig) -> None:
    ensure_dirs()
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config.model_dump(), f, default_flow_style=False, sort_keys=False)


def save_garmin_credentials(email: str, password: str) -> None:
    keyring.set_password(KEYRING_SERVICE, KEYRING_EMAIL_KEY, email)
    keyring.set_password(KEYRING_SERVICE, KEYRING_PASSWORD_KEY, password)


def get_garmin_credentials() -> tuple[str, str]:
    email = keyring.get_password(KEYRING_SERVICE, KEYRING_EMAIL_KEY) or ""
    password = keyring.get_password(KEYRING_SERVICE, KEYRING_PASSWORD_KEY) or ""
    return email, password


def has_garmin_credentials() -> bool:
    email, password = get_garmin_credentials()
    return bool(email and password)
