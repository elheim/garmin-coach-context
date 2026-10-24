from __future__ import annotations

import os
from pathlib import Path

import keyring
import keyring.errors
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
    sport: str = ""  # running, cycling, triathlon
    race_distance: str = ""  # 5K, 10K, half_marathon, marathon, sprint, olympic, 70.3, ironman
    race_date: str = ""
    race_name: str = ""
    experience: str = ""
    max_weekly_hours: float = 0.0
    injury_history: list[str] = Field(default_factory=list)
    goals: str = ""


class ThresholdConfig(BaseModel):
    """Training thresholds used to judge workouts. Empty = fall back to Garmin's values where possible."""
    ftp_watts: int | None = None
    lthr_bike: int | None = None  # lactate threshold HR on the bike
    lthr_run: int | None = None  # lactate threshold HR running
    easy_hr_cap: int | None = None  # upper HR limit for easy runs/rides


class CoachConfig(BaseModel):
    tone: str = "direct"  # direct, supportive, tough
    units: str = "metric"  # metric, imperial


class CycleConfig(BaseModel):
    """Optional cycle-aware readiness. Only offered when the Garmin profile is female."""
    eligible: bool | None = None  # set once from the Garmin profile; None = not checked yet
    tracking: str = "unasked"  # unasked, on, off
    share_in_context_file: bool = False  # include cycle details in training_context.md


class SyncConfig(BaseModel):
    lookback_days: int = 90
    analyze_days: int = 14  # analyze laps/reps of run & bike sessions from the last N days


class AppConfig(BaseModel):
    athlete: AthleteConfig = Field(default_factory=AthleteConfig)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    coach: CoachConfig = Field(default_factory=CoachConfig)
    cycle: CycleConfig = Field(default_factory=CycleConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)


def ensure_dirs() -> None:
    # Health data, profile and Garmin tokens live here — owner-only access.
    for d in (APP_DIR, DATA_DIR, SESSION_DIR):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)


def load_config() -> AppConfig:
    ensure_dirs()
    if CONFIG_PATH.exists():
        CONFIG_PATH.chmod(0o600)  # profile + injury history: owner-only
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
        data.pop("garmin", None)
        return AppConfig.model_validate(data)
    return AppConfig()


def save_config(config: AppConfig) -> None:
    ensure_dirs()
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config.model_dump(), f, default_flow_style=False, sort_keys=False)
    CONFIG_PATH.chmod(0o600)


def save_garmin_credentials(email: str, password: str) -> None:
    keyring.set_password(KEYRING_SERVICE, KEYRING_EMAIL_KEY, email)
    keyring.set_password(KEYRING_SERVICE, KEYRING_PASSWORD_KEY, password)


def delete_garmin_credentials() -> None:
    for key in (KEYRING_EMAIL_KEY, KEYRING_PASSWORD_KEY):
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass


def get_garmin_credentials() -> tuple[str, str]:
    email = keyring.get_password(KEYRING_SERVICE, KEYRING_EMAIL_KEY) or ""
    password = keyring.get_password(KEYRING_SERVICE, KEYRING_PASSWORD_KEY) or ""
    return email, password


def has_garmin_credentials() -> bool:
    email, password = get_garmin_credentials()
    return bool(email and password)
