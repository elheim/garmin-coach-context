from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from garminconnect import Garmin
from rich.progress import Progress, SpinnerColumn, TextColumn

from .config import SESSION_DIR, get_garmin_credentials
from .database import Database
from .models import Activity, HealthMetrics

logger = logging.getLogger(__name__)


def _prompt_mfa() -> str:
    """Prompt the user for a Garmin MFA code in the terminal."""
    import typer

    return typer.prompt("Enter Garmin MFA/2FA code")


def _get_client() -> Garmin:
    email, password = get_garmin_credentials()
    if not email or not password:
        raise RuntimeError("Garmin credentials not found. Run 'coach login' first.")
    client = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa)
    token_path = str(SESSION_DIR)
    try:
        client.login(tokenstore=token_path)
    except Exception:
        client.login()
        client.garth.dump(token_path)
    return client


def _parse_activity(raw: dict) -> Activity:
    start = None
    if raw.get("startTimeLocal"):
        try:
            start = datetime.fromisoformat(raw["startTimeLocal"])
        except (ValueError, TypeError):
            pass

    avg_pace = None
    avg_speed = raw.get("averageSpeed")
    if avg_speed and avg_speed > 0:
        avg_speed_kmh = avg_speed * 3.6
        avg_pace = 60.0 / avg_speed_kmh if avg_speed_kmh > 0 else None
    else:
        avg_speed_kmh = None

    return Activity(
        activity_id=raw["activityId"],
        sport_type=raw.get("activityType", {}).get("typeKey", ""),
        activity_type=raw.get("activityType", {}).get("typeKey", ""),
        start_time=start,
        duration_seconds=raw.get("duration", 0) or 0,
        distance_meters=raw.get("distance", 0) or 0,
        avg_hr=raw.get("averageHR"),
        max_hr=raw.get("maxHR"),
        calories=raw.get("calories"),
        avg_pace_min_per_km=avg_pace,
        avg_speed_kmh=avg_speed_kmh,
        avg_power=raw.get("avgPower"),
        normalized_power=raw.get("normPower"),
        tss=raw.get("trainingStressScore"),
        training_effect_aerobic=raw.get("aerobicTrainingEffect"),
        training_effect_anaerobic=raw.get("anaerobicTrainingEffect"),
        elevation_gain=raw.get("elevationGain"),
        avg_cadence=raw.get("averageRunningCadenceInStepsPerMinute")
        or raw.get("averageBikingCadenceInRevPerMinute"),
        activity_name=raw.get("activityName", ""),
        description=raw.get("description", "") or "",
        raw_json=json.dumps(raw, default=str),
    )


def _safe_get(func, *args, default=None, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as e:
        logger.debug("API call %s failed: %s", func.__name__, e)
        return default


def _as_dict(val, index: int = 0) -> dict:
    """Garmin API sometimes returns a list instead of a dict. Normalize it."""
    if isinstance(val, dict):
        return val
    if isinstance(val, list) and len(val) > index and isinstance(val[index], dict):
        return val[index]
    return {}


def sync_activities(db: Database, lookback_days: int = 90) -> int:
    client = _get_client()
    start_date = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end_date = date.today().strftime("%Y-%m-%d")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
    ) as progress:
        progress.add_task("Fetching activities from Garmin Connect...", total=None)
        raw_activities = client.get_activities_by_date(start_date, end_date)

    count = 0
    for raw in raw_activities:
        try:
            activity = _parse_activity(raw)

            hr_zones = _safe_get(
                client.get_activity_hr_in_timezones, str(activity.activity_id)
            )
            if hr_zones:
                activity.hr_zones = hr_zones

            db.upsert_activity(activity)
            count += 1
        except Exception as e:
            logger.warning("Failed to process activity %s: %s", raw.get("activityId"), e)

    db.log_sync("activities", count)
    return count


def sync_health(db: Database, lookback_days: int = 14) -> int:
    client = _get_client()
    count = 0

    end = date.today()
    start = end - timedelta(days=lookback_days)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
    ) as progress:
        task = progress.add_task("Fetching health metrics...", total=lookback_days)

        for day_offset in range(lookback_days):
            current = start + timedelta(days=day_offset)
            ds = current.strftime("%Y-%m-%d")

            try:
                metrics = HealthMetrics(metric_date=current)

                stats = _as_dict(_safe_get(client.get_stats, ds, default={}))
                if stats:
                    metrics.resting_hr = stats.get("restingHeartRate")

                sleep = _as_dict(_safe_get(client.get_sleep_data, ds, default={}))
                if sleep:
                    daily = _as_dict(sleep.get("dailySleepDTO", {}))
                    metrics.sleep_score = _as_dict(
                        _as_dict(daily.get("sleepScores", {})).get("overall", {})
                    ).get("value")
                    metrics.sleep_duration_seconds = daily.get("sleepTimeSeconds")
                    metrics.deep_sleep_seconds = daily.get("deepSleepSeconds")
                    metrics.rem_sleep_seconds = daily.get("remSleepSeconds")

                hrv = _as_dict(_safe_get(client.get_hrv_data, ds, default={}))
                if hrv:
                    summary = _as_dict(hrv.get("hrvSummary", {}))
                    metrics.hrv_weekly_avg = summary.get("weeklyAvg")
                    metrics.hrv_last_night = summary.get("lastNight")
                    metrics.hrv_status = summary.get("status")

                bb = _safe_get(client.get_body_battery, ds, default=[])
                bb_entry = _as_dict(bb)
                if bb_entry:
                    metrics.body_battery_high = bb_entry.get("charged")
                    metrics.body_battery_low = bb_entry.get("drained")

                stress = _as_dict(_safe_get(client.get_stress_data, ds, default={}))
                if stress:
                    metrics.stress_avg = stress.get("overallStressLevel")

                readiness = _as_dict(
                    _safe_get(client.get_training_readiness, ds, default={})
                )
                if readiness:
                    metrics.training_readiness = readiness.get(
                        "score"
                    ) or readiness.get("trainingReadinessScore")

                max_met = _as_dict(_safe_get(client.get_max_metrics, ds, default={}))
                if max_met:
                    generic = _as_dict(max_met.get("generic", {}))
                    cycling = _as_dict(max_met.get("cycling", {}))
                    if generic:
                        metrics.vo2_max_running = generic.get("vo2MaxPreciseValue")
                    if cycling:
                        metrics.vo2_max_cycling = cycling.get("vo2MaxPreciseValue")

                body = _as_dict(_safe_get(client.get_body_composition, ds, default={}))
                if body:
                    metrics.weight_kg = body.get("weight")
                    if metrics.weight_kg and metrics.weight_kg > 1000:
                        metrics.weight_kg = metrics.weight_kg / 1000.0
                    metrics.body_fat_pct = body.get("bodyFat")

                db.upsert_health(metrics)
                count += 1
            except Exception as e:
                logger.warning("Failed health metrics for %s: %s", ds, e)

            progress.advance(task)

    db.log_sync("health", count)
    return count
