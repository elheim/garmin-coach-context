from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class Activity(BaseModel):
    activity_id: int
    sport_type: str = ""
    activity_type: str = ""
    start_time: datetime | None = None
    duration_seconds: float = 0.0
    distance_meters: float = 0.0
    avg_hr: float | None = None
    max_hr: float | None = None
    hr_zones: dict | list | None = None
    calories: float | None = None
    avg_pace_min_per_km: float | None = None
    avg_speed_kmh: float | None = None
    avg_power: float | None = None
    normalized_power: float | None = None
    tss: float | None = None
    training_effect_aerobic: float | None = None
    training_effect_anaerobic: float | None = None
    elevation_gain: float | None = None
    avg_cadence: float | None = None
    activity_name: str = ""
    description: str = ""
    raw_json: str = ""


class HealthMetrics(BaseModel):
    metric_date: date
    resting_hr: int | None = None
    hrv_weekly_avg: float | None = None
    hrv_last_night: float | None = None
    hrv_status: str | None = None
    sleep_score: int | None = None
    sleep_duration_seconds: int | None = None
    deep_sleep_seconds: int | None = None
    rem_sleep_seconds: int | None = None
    body_battery_high: int | None = None
    body_battery_low: int | None = None
    stress_avg: int | None = None
    training_readiness: int | None = None
    vo2_max_running: float | None = None
    vo2_max_cycling: float | None = None
    weight_kg: float | None = None
    body_fat_pct: float | None = None


class WeeklySummary(BaseModel):
    week_start: date
    swim_hours: float = 0.0
    swim_km: float = 0.0
    swim_sessions: int = 0
    bike_hours: float = 0.0
    bike_km: float = 0.0
    bike_sessions: int = 0
    run_hours: float = 0.0
    run_km: float = 0.0
    run_sessions: int = 0
    strength_sessions: int = 0
    other_sessions: int = 0
    total_hours: float = 0.0
    total_tss: float = 0.0
    avg_hr_all: float | None = None
