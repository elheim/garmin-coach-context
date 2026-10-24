from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
from datetime import date, datetime, timedelta

from garminconnect import Garmin
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from .analysis import ANALYSIS_VERSION, Thresholds, analyze, classify
from .config import SESSION_DIR, get_garmin_credentials
from .database import Database
from .models import Activity, HealthMetrics

logger = logging.getLogger(__name__)

_client_instance = None


def _prompt_mfa() -> str:
    """Prompt the user for a Garmin MFA code in the terminal."""
    import sys

    import typer

    if not sys.stdin.isatty():
        # The MCP server talks over stdin — prompting there would hang it.
        raise RuntimeError("Garmin needs a new 2FA login. Run `coach login` in a terminal, then retry.")
    return typer.prompt("Enter Garmin MFA/2FA code")


def _secure_session_files() -> None:
    """Garmin OAuth tokens grant account access — keep them readable by the owner only."""
    SESSION_DIR.chmod(0o700)
    for f in SESSION_DIR.glob("*"):
        f.chmod(0o600)


def _get_client() -> Garmin:
    global _client_instance
    if _client_instance is None:
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
        _secure_session_files()
        _client_instance = client
    return _client_instance


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


def _fetch_raw_activities(lookback_days: int = 90, silent: bool = False) -> list[dict]:
    """Fetch raw activity data from Garmin API."""
    client = _get_client()
    start_date = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end_date = date.today().strftime("%Y-%m-%d")

    if silent:
        return client.get_activities_by_date(start_date, end_date)
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=20),
    ) as progress:
        progress.add_task("Fetching activities from Garmin Connect...", total=None)
        return client.get_activities_by_date(start_date, end_date)


def _child_to_list_format(detail: dict) -> dict:
    """Map a get_activity() detail payload onto the list-endpoint shape _parse_activity expects."""
    s = detail.get("summaryDTO", {}) or {}
    start = (s.get("startTimeLocal") or "").replace("T", " ").split(".")[0]
    return {
        "activityId": detail.get("activityId"),
        "activityName": detail.get("activityName", ""),
        "activityType": {"typeKey": (detail.get("activityTypeDTO") or {}).get("typeKey", "")},
        "startTimeLocal": start or None,
        "duration": s.get("duration"),
        "distance": s.get("distance"),
        "averageHR": s.get("averageHR"),
        "maxHR": s.get("maxHR"),
        "calories": s.get("calories"),
        "averageSpeed": s.get("averageSpeed"),
        "avgPower": s.get("averagePower"),
        "normPower": s.get("normalizedPower"),
        "trainingStressScore": s.get("trainingStressScore"),
        "aerobicTrainingEffect": s.get("trainingEffect"),
        "anaerobicTrainingEffect": s.get("anaerobicTrainingEffect"),
        "elevationGain": s.get("elevationGain"),
        "averageRunningCadenceInStepsPerMinute": s.get("averageRunCadence"),
        "averageBikingCadenceInRevPerMinute": s.get("averageBikeCadence"),
        "description": detail.get("description", ""),
        "parentId": detail.get("metadataDTO", {}).get("parentId"),
    }


def _expand_multisport(raw_activities: list[dict]) -> list[dict]:
    """Add the individual legs (swim, T1, bike, T2, run) of multisport activities.

    Garmin's activity list only returns the multisport parent, so without this a
    triathlon's swim and run never reach the database.
    """
    client = _get_client()
    expanded = list(raw_activities)
    for raw in raw_activities:
        if raw.get("activityType", {}).get("typeKey") != "multi_sport":
            continue
        parent = _safe_get(client.get_activity, str(raw["activityId"]), default={}) or {}
        for child_id in (parent.get("metadataDTO", {}) or {}).get("childIds", []) or []:
            detail = _safe_get(client.get_activity, str(child_id), default=None)
            if detail:
                expanded.append(_child_to_list_format(detail))
    return expanded


def _process_raw_activities(raw_activities: list[dict], limit: int | None = None) -> list[Activity]:
    """Convert raw activity data to Activity objects."""
    activities = []
    raw_activities = _expand_multisport(raw_activities[:limit])
    for raw in raw_activities:
        try:
            activities.append(_parse_activity(raw))
        except Exception as e:
            logger.warning("Failed to process activity %s: %s", raw.get("activityId"), e)

    return activities


def _fetch_health_metrics_for_day(client: Garmin, ds: str) -> HealthMetrics:
    """Fetch all health metrics for a single day from Garmin API concurrently."""
    current = datetime.strptime(ds, "%Y-%m-%d").date()
    metrics = HealthMetrics(metric_date=current)

    # Define all API calls as functions for concurrent execution
    def get_stats():
        return _as_dict(_safe_get(client.get_stats, ds, default={}))

    def get_sleep():
        return _as_dict(_safe_get(client.get_sleep_data, ds, default={}))

    def get_hrv():
        return _as_dict(_safe_get(client.get_hrv_data, ds, default={}))

    def get_stress():
        return _as_dict(_safe_get(client.get_stress_data, ds, default={}))

    def get_readiness():
        return _as_dict(_safe_get(client.get_training_readiness, ds, default={}))

    def get_max_metrics():
        return _as_dict(_safe_get(client.get_max_metrics, ds, default={}))

    def get_body_composition():
        return _as_dict(_safe_get(client.get_body_composition, ds, default={}))

    # Execute all API calls concurrently using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_stats): 'stats',
            executor.submit(get_sleep): 'sleep',
            executor.submit(get_hrv): 'hrv',
            executor.submit(get_stress): 'stress',
            executor.submit(get_readiness): 'readiness',
            executor.submit(get_max_metrics): 'max_met',
            executor.submit(get_body_composition): 'body',
        }

        results = {}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                logger.debug("API call %s failed for %s: %s", key, ds, e)
                results[key] = None

    # Process results
    stats = results.get('stats')
    if stats:
        metrics.resting_hr = stats.get("restingHeartRate")
        # Daily body battery levels (not the charged/drained amounts).
        metrics.body_battery_high = stats.get("bodyBatteryHighestValue")
        metrics.body_battery_low = stats.get("bodyBatteryLowestValue")
        metrics.stress_avg = stats.get("averageStressLevel")

    sleep = results.get('sleep')
    if sleep:
        daily = _as_dict(sleep.get("dailySleepDTO", {}))
        metrics.sleep_score = _as_dict(
            _as_dict(daily.get("sleepScores", {})).get("overall", {})
        ).get("value")
        metrics.sleep_duration_seconds = daily.get("sleepTimeSeconds")
        metrics.deep_sleep_seconds = daily.get("deepSleepSeconds")
        metrics.rem_sleep_seconds = daily.get("remSleepSeconds")

    hrv = results.get('hrv')
    if hrv:
        summary = _as_dict(hrv.get("hrvSummary", {}))
        metrics.hrv_weekly_avg = summary.get("weeklyAvg")
        metrics.hrv_last_night = summary.get("lastNightAvg") or summary.get("lastNight")
        metrics.hrv_status = summary.get("status")

    stress = results.get('stress')
    if stress and metrics.stress_avg is None:
        metrics.stress_avg = stress.get("avgStressLevel") or stress.get("overallStressLevel")

    readiness = results.get('readiness')
    if readiness:
        metrics.training_readiness = readiness.get(
            "score"
        ) or readiness.get("trainingReadinessScore")

    max_met = results.get('max_met')
    if max_met:
        generic = _as_dict(max_met.get("generic", {}))
        cycling = _as_dict(max_met.get("cycling", {}))
        if generic:
            metrics.vo2_max_running = generic.get("vo2MaxPreciseValue")
        if cycling:
            metrics.vo2_max_cycling = cycling.get("vo2MaxPreciseValue")

    body = results.get('body')
    if body:
        metrics.weight_kg = body.get("weight")
        if metrics.weight_kg and metrics.weight_kg > 1000:
            metrics.weight_kg = metrics.weight_kg / 1000.0
        metrics.body_fat_pct = body.get("bodyFat")

    return metrics


def _run_concurrent_fetch(executor_func, items: list, max_workers: int = 4, silent: bool = False, progress_desc: str = "Processing...", item_count: int = None) -> list:
    """Generic helper to run concurrent API calls with optional progress tracking."""
    results = []
    item_count = item_count or len(items)
    
    def _do_fetch():
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(executor_func, item): item for item in items}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result is not None:
                        results.append(result)
                except Exception as e:
                    item = futures[future]
                    logger.warning("Failed to fetch for %s: %s", item, e)
    
    if silent:
        _do_fetch()
    else:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=20),
        ) as progress:
            task = progress.add_task(progress_desc, total=item_count)
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(executor_func, item): item for item in items}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        if result is not None:
                            results.append(result)
                    except Exception as e:
                        item = futures[future]
                        logger.warning("Failed to fetch for %s: %s", item, e)
                    
                    progress.advance(task)
    
    return results


def _fetch_health_metrics_list(lookback_days: int = 14, max_workers: int = 4, silent: bool = False) -> list[HealthMetrics]:
    """Fetch health metrics from Garmin API concurrently."""
    client = _get_client()
    
    end = date.today()
    start = end - timedelta(days=lookback_days)
    
    # Generate all date strings, including today (last night's sleep/HRV is
    # filed under today's date)
    date_strings = [
        (start + timedelta(days=day_offset)).strftime("%Y-%m-%d")
        for day_offset in range(lookback_days + 1)
    ]
    
    def fetch_day_metrics(ds: str) -> HealthMetrics:
        return _fetch_health_metrics_for_day(client, ds)
    
    return _run_concurrent_fetch(
        fetch_day_metrics,
        date_strings,
        max_workers=max_workers,
        silent=silent,
        progress_desc="Fetching health metrics...",
        item_count=len(date_strings)
    )


def incremental_lookback(db: Database, full_days: int, sync_type: str = "activities") -> int:
    """Days to re-fetch: everything on the first sync, otherwise since the last sync plus a 3-day margin
    (late uploads, edited activities)."""
    last = db.get_last_sync_time(sync_type)
    if not last:
        return full_days
    return min(full_days, max(3, (datetime.now() - last).days + 3))


def sync_activities(db: Database, lookback_days: int = 90, silent: bool = False) -> int:
    raw_activities = _fetch_raw_activities(lookback_days, silent=silent)
    activities = _process_raw_activities(raw_activities)

    count = 0
    for activity in activities:
        try:
            db.upsert_activity(activity)
            count += 1
        except Exception as e:
            logger.warning("Failed to upsert activity %s: %s", activity.activity_id, e)

    db.log_sync("activities", count)
    return count


def sync_health(db: Database, lookback_days: int = 14, max_workers: int = 4, silent: bool = False) -> int:
    metrics_list = _fetch_health_metrics_list(lookback_days, max_workers, silent=silent)

    count = 0
    for metrics in metrics_list:
        try:
            db.upsert_health(metrics)
            count += 1
        except Exception as e:
            logger.warning("Failed to upsert health metrics for %s: %s", metrics.metric_date, e)

    db.log_sync("health", count)
    return count


# ── Optional cycle awareness (opt-in; see cycle.py for the privacy model) ──

def sync_cycle(db: Database, lookback_days: int = 14) -> None:
    """Check eligibility once; sync cycle days only when the athlete has opted in."""
    from . import cycle
    from .config import load_config, save_config

    cfg = load_config()
    if cfg.cycle.eligible is None:
        profile = _safe_get(_get_client().get_user_profile, default={}) or {}
        gender = ((profile.get("userData") or {}).get("gender") or "").upper()
        cfg.cycle.eligible = gender == "FEMALE"
        save_config(cfg)
    if not cfg.cycle.eligible or cfg.cycle.tracking != "on":
        return

    client = _get_client()
    url = getattr(client, "garmin_connect_menstrual_dayview_url", "/periodichealth-service/menstrualcycle/dayview")
    today = date.today().isoformat()
    rows = []
    for offset in range(lookback_days + 1):
        ds = (date.today() - timedelta(days=offset)).isoformat()
        params = {"todayDate": today, "includeAnalysis": "true", "skipbm": "true"}
        try:  # deliberately silent: nothing about this data goes to logs
            payload = client.connectapi(f"{url}/{ds}", params=params)
        except Exception:
            payload = None
        row = cycle.parse_day(ds, payload)
        if row:
            rows.append(row)
    if rows:
        cycle.save_days(db.conn, rows)


# ── Workout analysis ──

_garmin_thresholds: dict | None = None


def _garmin_threshold_fallback() -> dict:
    """FTP and lactate-threshold HR from Garmin, used only when config.yaml leaves them empty."""
    global _garmin_thresholds
    if _garmin_thresholds is None:
        client = _get_client()
        ftp = _safe_get(client.get_cycling_ftp, default=None)
        lt = _safe_get(client.get_lactate_threshold, default=None)
        ftp = _as_dict(ftp)
        lt_hr = _as_dict((lt or {}).get("speed_and_heart_rate") if isinstance(lt, dict) else None)
        _garmin_thresholds = {
            "ftp": ftp.get("functionalThresholdPower"),
            "lthr": lt_hr.get("heartRate"),
        }
    return _garmin_thresholds


def resolve_thresholds() -> Thresholds:
    from .config import load_config

    cfg = load_config()
    t = cfg.thresholds
    ftp, lthr_bike, lthr_run = t.ftp_watts, t.lthr_bike, t.lthr_run
    if not ftp or not lthr_run:
        g = _garmin_threshold_fallback()
        ftp = ftp or g.get("ftp")
        lthr_run = lthr_run or g.get("lthr")
    return Thresholds(
        ftp_watts=ftp,
        lthr_bike=lthr_bike or lthr_run,
        lthr_run=lthr_run,
        easy_hr_cap=t.easy_hr_cap,
        units=cfg.coach.units,
    )


def _threshold_key(th: Thresholds) -> list:
    return [th.ftp_watts, th.lthr_bike, th.lthr_run, th.easy_hr_cap, th.units]


def analyze_activity(db: Database, activity: Activity, force: bool = False,
                     thresholds: Thresholds | None = None) -> dict | None:
    """Lap/rep/pacing analysis of one run or ride, cached in the database."""
    if classify(activity.sport_type or activity.activity_type) not in ("run", "bike"):
        return None
    th = thresholds or resolve_thresholds()
    if not force:
        cached = db.get_analysis(activity.activity_id, ANALYSIS_VERSION)
        if cached and cached.get("th_key") == _threshold_key(th):
            return cached

    client = _get_client()
    raw = json.loads(activity.raw_json) if activity.raw_json else {}
    raw.setdefault("activityId", activity.activity_id)
    raw.setdefault("activityType", {"typeKey": activity.sport_type})
    detail = _safe_get(client.get_activity, str(activity.activity_id), default={}) or {}
    summary_dto = detail.get("summaryDTO") or {}
    for k in ("directWorkoutRpe", "directWorkoutFeel"):
        if summary_dto.get(k) is not None:
            raw[k] = summary_dto[k]
    splits = _safe_get(client.get_activity_splits, activity.activity_id, default={}) or {}
    details = _safe_get(client.get_activity_details, activity.activity_id, 20000, default=None)

    result = analyze(raw, splits.get("lapDTOs", []) or [], details, th)
    result["th_key"] = _threshold_key(th)
    result["notes"] = activity.description or ""
    db.save_analysis(activity.activity_id, ANALYSIS_VERSION, result)
    return result


def analyze_recent(db: Database, days: int = 14, silent: bool = False) -> int:
    """Analyze run and bike sessions from the last `days` days that are not cached yet."""
    th = resolve_thresholds()
    count = 0
    for activity in db.get_activities(since_days=days):
        if classify(activity.sport_type or activity.activity_type) not in ("run", "bike"):
            continue
        cached = db.get_analysis(activity.activity_id, ANALYSIS_VERSION)
        if cached and cached.get("th_key") == _threshold_key(th):
            continue
        try:
            if analyze_activity(db, activity, force=True, thresholds=th):
                count += 1
        except Exception as e:
            logger.warning("Failed to analyze activity %s: %s", activity.activity_id, e)
    return count


def get_recent_activities(lookback_days: int = 90, limit: int = 14) -> list[Activity]:
    """Fetch recent activities directly from Garmin API without storing in DB."""
    raw_activities = _fetch_raw_activities(lookback_days)
    return _process_raw_activities(raw_activities, limit=limit)


def get_health_metrics(lookback_days: int = 14, max_workers: int = 4) -> list[HealthMetrics]:
    """Fetch health metrics directly from Garmin API without storing in DB.
    
    Uses concurrent fetching to speed up the process.
    """
    metrics_list = _fetch_health_metrics_list(lookback_days, max_workers)
    return sorted(metrics_list, key=lambda m: m.metric_date, reverse=True)


def register_tools(mcp):
    @mcp.tool()
    def sync_garmin_data() -> str:
        """Sync latest activities and health data from Garmin Connect."""
        from datetime import datetime, timedelta
        db = Database()
        
        now = datetime.now()
        last_act_sync = db.get_last_sync_time("activities")
        last_health_sync = db.get_last_sync_time("health")
        
        act_count = 0
        if not last_act_sync or (now - last_act_sync) > timedelta(hours=1):
            act_count = sync_activities(db, lookback_days=incremental_lookback(db, 90), silent=True)

        health_count = 0
        if not last_health_sync or (now - last_health_sync) > timedelta(hours=1):
            health_count = sync_health(db, lookback_days=incremental_lookback(db, 45, "health"), silent=True)

        from .config import load_config
        try:
            sync_cycle(db, lookback_days=14)
        except Exception:
            pass
        analyzed = analyze_recent(db, days=load_config().sync.analyze_days, silent=True)

        db.close()
        if act_count == 0 and health_count == 0:
            return "Data is already up to date (last synced less than 1 hour ago)."
        return (f"Synced {act_count} activities and {health_count} health records from Garmin Connect; "
                f"analyzed {analyzed} new workouts.")

    @mcp.tool()
    def analyze_workout(activity_id: int = 0, date: str = "") -> str:
        """Detailed breakdown of one run or ride: every rep/interval with power, pace, HR and cadence, where the athlete went too hard or faded, stops, surges, decoupling.

        Pass activity_id, or a date (YYYY-MM-DD) to analyze every run/ride that day, or nothing for the most recent run/ride.
        Use this whenever the athlete asks how a workout went — the weekly context only has summaries.
        """
        from .analysis import render

        db = Database()
        try:
            if activity_id:
                a = db.get_activity(activity_id)
                targets = [a] if a else []
            elif date:
                targets = db.find_activities_on(date)
            else:
                targets = [x for x in db.get_recent_activities(limit=20)
                           if classify(x.sport_type or x.activity_type) in ("run", "bike")][:1]
            out = []
            for a in targets:
                result = analyze_activity(db, a)
                if result:
                    out.append(render(result))
            if not out:
                return "No run or ride found for that request. Sync first or check the activity id/date."
            return "\n\n".join(out)
        finally:
            db.close()

    return mcp
