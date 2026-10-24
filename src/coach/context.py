from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import CONTEXT_FILE, load_config
from .models import Activity, HealthMetrics, WeeklySummary
from .utils import NON_TRAINING, _dedupe_overlapping, build_weekly_summaries, classify_sport


def _fmt_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def athlete_docs_dir() -> Path:
    """athlete_docs/ in the working directory, else next to the installed project (MCP clients often start elsewhere)."""
    local = Path("athlete_docs")
    if local.is_dir():
        return local
    return Path(__file__).resolve().parents[2] / "athlete_docs"


def _section(title: str, content: str) -> str:
    return f"## {title}\n\n{content}\n"


def build_athlete_profile(config_athlete, health: list[HealthMetrics], thresholds=None) -> str:
    lines = []
    if config_athlete.sport:
        sport = config_athlete.sport + (f" — {config_athlete.race_distance}" if config_athlete.race_distance else "")
        lines.append(f"- **Sport**: {sport}")
    if thresholds is not None:
        t = thresholds
        parts = [
            f"FTP {t.ftp_watts} W" if t.ftp_watts else None,
            f"LTHR run {t.lthr_run}" if t.lthr_run else None,
            f"LTHR bike {t.lthr_bike}" if t.lthr_bike else None,
            f"easy HR cap {t.easy_hr_cap}" if t.easy_hr_cap else None,
        ]
        parts = [p for p in parts if p]
        lines.append(f"- **Thresholds**: {', '.join(parts)}" if parts
                     else "- **Thresholds**: not set — run `coach config` (workout analysis falls back to Garmin's FTP/LTHR)")
    if config_athlete.race_name:
        lines.append(f"- **Target race**: {config_athlete.race_name}")
    if config_athlete.race_date:
        try:
            rd = date.fromisoformat(config_athlete.race_date)
            days_out = (rd - date.today()).days
            lines.append(f"- **Race date**: {config_athlete.race_date} ({days_out} days away)")
        except ValueError:
            lines.append(f"- **Race date**: {config_athlete.race_date}")
    if config_athlete.experience:
        lines.append(f"- **Experience level**: {config_athlete.experience}")
    if config_athlete.max_weekly_hours:
        lines.append(f"- **Max weekly training hours**: {config_athlete.max_weekly_hours}")
    if config_athlete.goals:
        lines.append(f"- **Goals**: {config_athlete.goals}")
    if config_athlete.injury_history:
        lines.append(f"- **Injury history**: {', '.join(config_athlete.injury_history)}")

    recent = [h for h in health[:7] if h.vo2_max_running or h.vo2_max_cycling or h.weight_kg]
    if recent:
        latest = recent[0]
        if latest.vo2_max_running:
            lines.append(f"- **VO2 max (running)**: {latest.vo2_max_running:.1f}")
        if latest.vo2_max_cycling:
            lines.append(f"- **VO2 max (cycling)**: {latest.vo2_max_cycling:.1f}")
        if latest.weight_kg:
            lines.append(f"- **Weight**: {latest.weight_kg:.1f} kg")
        rhr_values = [h.resting_hr for h in recent if h.resting_hr]
        if rhr_values:
            lines.append(f"- **Resting HR (7d avg)**: {sum(rhr_values) / len(rhr_values):.0f} bpm")

    return "\n".join(lines) if lines else "No athlete profile configured."


def build_weekly_summary_table(weeks: list[WeeklySummary]) -> str:
    if not weeks:
        return "No training data available yet."

    lines = [
        "| Week | Swim | Bike | Run | Strength | Total Hours | TSS |",
        "|------|------|------|-----|----------|-------------|-----|",
    ]
    for w in weeks:
        swim = f"{w.swim_hours:.1f}h / {w.swim_km:.1f}km ({w.swim_sessions}x)" if w.swim_sessions else "-"
        bike = f"{w.bike_hours:.1f}h / {w.bike_km:.0f}km ({w.bike_sessions}x)" if w.bike_sessions else "-"
        run = f"{w.run_hours:.1f}h / {w.run_km:.1f}km ({w.run_sessions}x)" if w.run_sessions else "-"
        strength = f"{w.strength_sessions}x" if w.strength_sessions else "-"
        tss = f"{w.total_tss:.0f}" if w.total_tss else "-"
        lines.append(
            f"| {w.week_start} | {swim} | {bike} | {run} | {strength} | {w.total_hours:.1f} | {tss} |"
        )

    return "\n".join(lines)


def build_training_load(weeks: list[WeeklySummary]) -> str:
    if len(weeks) < 2:
        return "Not enough data for training load analysis."

    lines = []

    recent_hours = [w.total_hours for w in weeks[:1]]
    acute = sum(recent_hours) / max(len(recent_hours), 1)

    chronic_hours = [w.total_hours for w in weeks[:6]]
    chronic = sum(chronic_hours) / max(len(chronic_hours), 1)

    if chronic > 0:
        ratio = acute / chronic
        lines.append(f"- **Acute load (this week)**: {acute:.1f} hours")
        lines.append(f"- **Chronic load (6-week avg)**: {chronic:.1f} hours/week")
        lines.append(f"- **Acute:Chronic ratio**: {ratio:.2f}")
        if ratio > 1.3:
            lines.append("- **Warning**: Ratio >1.3 — elevated injury/overtraining risk")
        elif ratio < 0.8:
            lines.append("- **Note**: Ratio <0.8 — detraining risk if sustained")
        else:
            lines.append("- **Status**: Ratio in safe zone (0.8–1.3)")
    else:
        lines.append(f"- **This week**: {acute:.1f} hours")
        lines.append("- Not enough historical data for load ratio")

    hour_trend = [w.total_hours for w in reversed(weeks)]
    if len(hour_trend) >= 3:
        trend_str = " → ".join(f"{h:.1f}" for h in hour_trend[-6:])
        lines.append(f"- **Weekly hours trend**: {trend_str}")

    return "\n".join(lines)


def build_recent_activities(activities: list[Activity], analyses: dict[int, dict] | None = None,
                            units: str = "metric") -> str:
    from .analysis import fmt_dist, fmt_pace

    if not activities:
        return "No recent activities."
    analyses = analyses or {}

    lines = []
    for a in activities[:14]:
        date_str = a.start_time.strftime("%Y-%m-%d %H:%M") if a.start_time else "?"
        sport = a.sport_type or a.activity_type or "unknown"
        dur = _fmt_duration(a.duration_seconds)
        dist = fmt_dist(a.distance_meters, units).replace(" ", "") if a.distance_meters else ""

        detail_parts = []
        if a.avg_hr:
            detail_parts.append(f"avg HR {a.avg_hr:.0f}")
        if a.max_hr:
            detail_parts.append(f"max HR {a.max_hr:.0f}")
        if a.avg_speed_kmh and "run" in sport.lower():
            detail_parts.append(f"pace {fmt_pace(a.avg_speed_kmh / 3.6, units)}")
        if a.avg_speed_kmh and classify_sport(sport) == "bike":
            speed = a.avg_speed_kmh / 1.609344 if units == "imperial" else a.avg_speed_kmh
            detail_parts.append(f"avg {speed:.1f} {'mph' if units == 'imperial' else 'km/h'}")
        if a.avg_power:
            detail_parts.append(f"power {a.avg_power:.0f}W")
        if a.normalized_power:
            detail_parts.append(f"NP {a.normalized_power:.0f}W")
        if a.tss:
            detail_parts.append(f"TSS {a.tss:.0f}")
        if a.training_effect_aerobic:
            detail_parts.append(f"TE(aero) {a.training_effect_aerobic:.1f}")
        if a.elevation_gain:
            detail_parts.append(f"elev +{a.elevation_gain:.0f}m")

        details = " | ".join(detail_parts)
        name = f" — *{a.activity_name}*" if a.activity_name else ""
        line = f"- **{date_str}** {sport} {dur} {dist}{name}"
        if details:
            line += f"\n  {details}"
        an = analyses.get(a.activity_id)
        if an:
            warns = [f["text"] for f in an.get("flags", []) if f["level"] == "warn"]
            line += f"\n  → {an['session_type']}" + (f" · ⚠ {warns[0]}" if warns else "")
        lines.append(line)

    return "\n".join(lines)


def _baseline(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 5:
        return None
    m = sum(values) / len(values)
    sd = (sum((v - m) ** 2 for v in values) / len(values)) ** 0.5
    return m, sd


def _day_status(h: HealthMetrics, base: dict, adj: dict | None = None) -> tuple[str, list[str]]:
    """GREEN / AMBER / RED for one morning, judged against the athlete's own 30-day baseline.

    `adj` widens the tolerance for expected, non-fatigue shifts (resting HR up, HRV down).
    """
    adj = adj or {}
    red, amber = [], []
    if h.hrv_last_night and base.get("hrv"):
        dev = (h.hrv_last_night - base["hrv"][0]) / base["hrv"][0] + adj.get("hrv_pct", 0)
        if dev <= -0.15:
            red.append(f"HRV {h.hrv_last_night:.0f} ms is {abs(dev) * 100:.0f}% below baseline")
        elif dev <= -0.08:
            amber.append(f"HRV {h.hrv_last_night:.0f} ms is {abs(dev) * 100:.0f}% below baseline")
    if (h.hrv_status or "").upper() in ("LOW", "POOR"):
        red.append(f"Garmin HRV status {h.hrv_status}")
    if h.resting_hr and base.get("rhr"):
        diff = h.resting_hr - base["rhr"][0] - adj.get("rhr", 0)
        if diff >= 5:
            red.append(f"resting HR {h.resting_hr} is +{diff:.0f} vs baseline")
        elif diff >= 3:
            amber.append(f"resting HR {h.resting_hr} is +{diff:.0f} vs baseline")
    if h.sleep_score is not None and h.sleep_score < 60:
        amber.append(f"sleep score {h.sleep_score}")
    if h.sleep_duration_seconds and h.sleep_duration_seconds < 6 * 3600:
        amber.append(f"slept {h.sleep_duration_seconds / 3600:.1f} h")
    if h.training_readiness is not None and h.training_readiness < 30:
        amber.append(f"Garmin readiness {h.training_readiness}")
    if red or len(amber) >= 3:
        return "RED", red + amber
    if amber:
        return "AMBER", amber
    return "GREEN", []


def build_readiness(health: list[HealthMetrics], personal: dict | None = None, detailed: bool = True) -> str:
    """Day-by-day recovery markers vs the athlete's own baseline — no weekly averaging.

    `personal` maps dates to optional per-day context (see cycle.py); `detailed` controls whether
    that context is described or only applied.
    """
    from . import cycle

    personal = personal or {}
    if not health:
        return "No health data available."
    recent = health[:7]
    history = health[7:37]
    base = {
        "hrv": _baseline([h.hrv_last_night for h in history if h.hrv_last_night]),
        "rhr": _baseline([h.resting_hr for h in history if h.resting_hr]),
        "sleep": _baseline([h.sleep_score for h in history if h.sleep_score]),
    }
    today = recent[0]
    adj = lambda h: cycle.allowance(personal.get(h.metric_date))  # noqa: E731
    status, reasons = _day_status(today, base, adj(today))
    lines = [f"**Today ({today.metric_date}): {status}**" + (f" — {'; '.join(reasons)}" if reasons else " — all markers within normal range of baseline")]
    streak = 0
    for h in recent:
        if _day_status(h, base, adj(h))[0] == "GREEN":
            break
        streak += 1
    if streak >= 2:
        lines.append(f"- ⚠ {streak} non-GREEN mornings in a row — accumulated fatigue or illness risk, not a one-off.")

    def b(key, fmt="{:.0f}"):
        v = base.get(key)
        return f"{fmt.format(v[0])} ±{fmt.format(v[1])}" if v else "n/a"

    lines += [
        "",
        f"Baseline (prior 30 days): HRV {b('hrv')} ms · resting HR {b('rhr')} · sleep score {b('sleep')}",
        "",
        "| Date | Status | HRV (7d avg) | RHR | Sleep score / h | Readiness | Body battery | Stress |",
        "|------|--------|--------------|-----|-----------------|-----------|--------------|--------|",
    ]
    for h in recent:
        st, _ = _day_status(h, base, adj(h))
        hrv = f"{h.hrv_last_night:.0f}" if h.hrv_last_night else "-"
        if h.hrv_weekly_avg:
            hrv += f" ({h.hrv_weekly_avg:.0f})"
        sleep = f"{h.sleep_score or '-'} / {h.sleep_duration_seconds / 3600:.1f}" if h.sleep_duration_seconds else f"{h.sleep_score or '-'}"
        bb = f"{h.body_battery_low}–{h.body_battery_high}" if h.body_battery_high is not None else "-"
        lines.append(
            f"| {h.metric_date} | {st} | {hrv} | {h.resting_hr or '-'} | {sleep} | "
            f"{h.training_readiness if h.training_readiness is not None else '-'} | {bb} | {h.stress_avg if h.stress_avg is not None else '-'} |"
        )
    note = cycle.coaching_note(personal.get(today.metric_date), detailed)
    if note:
        lines += ["", note]
    lines.append("")
    lines.append("*Rules: RED = HRV ≥15% below baseline, Garmin HRV LOW, resting HR ≥+5, or 3+ amber signs. "
                 "AMBER = HRV 8–15% below, resting HR +3–4, sleep score <60 or <6 h, readiness <30.*")
    return "\n".join(lines)


def _estimate_tss(a: Activity, th) -> tuple[float, bool]:
    """TSS if Garmin has it (power), otherwise an HR-based estimate (hrTSS)."""
    if a.tss:
        return a.tss, False
    hours = a.duration_seconds / 3600
    sport = classify_sport(a.sport_type or a.activity_type)
    lthr = (th.lthr_bike if sport == "bike" else th.lthr_run) if th else None
    if a.avg_hr and lthr:
        intensity = min(a.avg_hr / lthr, 1.1)
        return hours * intensity ** 2 * 100, True
    return hours * 50, True


def build_fitness_form(activities: list[Activity], th) -> str:
    """Banister fitness (CTL, 42 d), fatigue (ATL, 7 d) and form (TSB) from daily TSS."""
    acts = [a for a in _dedupe_overlapping(activities)
            if classify_sport(a.sport_type or a.activity_type) not in NON_TRAINING]
    if not acts:
        return "Not enough data."
    daily: dict[date, float] = {}
    est = total = 0.0
    for a in acts:
        tss, estimated = _estimate_tss(a, th)
        d = a.start_time.date()
        daily[d] = daily.get(d, 0) + tss
        total += tss
        if estimated:
            est += tss
    start = min(daily)
    ctl = atl = 0.0
    series = []
    d = start
    while d <= date.today():
        t = daily.get(d, 0.0)
        ctl += (t - ctl) / 42
        atl += (t - atl) / 7
        series.append((d, ctl, atl))
        d += timedelta(days=1)
    days_hist = len(series)
    _, ctl_now, atl_now = series[-1]
    tsb = (series[-2][1] - series[-2][2]) if len(series) > 1 else 0
    ctl_week_ago = series[-8][1] if len(series) > 8 else series[0][1]
    ramp = ctl_now - ctl_week_ago

    if tsb < -30:
        form = "deep fatigue — high risk if it stays here more than a few days"
    elif tsb < -10:
        form = "productive training fatigue"
    elif tsb <= 5:
        form = "neutral — absorbing load"
    elif tsb <= 20:
        form = "fresh"
    else:
        form = "very fresh — losing fitness if this lasts more than ~10 days"
    lines = [
        f"- **Fitness (CTL, 42 d)**: {ctl_now:.0f} · **Fatigue (ATL, 7 d)**: {atl_now:.0f} · **Form (TSB)**: {tsb:+.0f} → {form}",
        f"- **Ramp rate**: {ramp:+.1f} CTL/week" + (" ⚠ fast — injury risk above ~+7/week" if ramp > 7 else ""),
        f"- Last 7 days TSS: {sum(daily.get(date.today() - timedelta(days=i), 0) for i in range(7)):.0f}",
    ]
    if days_hist < 42:
        lines.append(f"- *Only {days_hist} days of history — CTL is underestimated until 6 weeks are synced.*")
    if total and est / total > 0.2:
        hint = "" if th and th.lthr_run else " Set lthr_run/lthr_bike in config — without them a flat 50 TSS/h is assumed."
        lines.append(f"- *{100 * est / total:.0f}% of load is estimated from heart rate (hrTSS — runs, swims and strength have no power-based TSS).{hint}*")
    return "\n".join(lines)


def build_intensity_distribution(activities: list[Activity]) -> str:
    """Share of time in easy / moderate / hard HR zones over the last 4 weeks (Garmin HR zones)."""
    cutoff = datetime.now() - timedelta(days=28)
    by_sport: dict[str, list[float]] = {}
    for a in _dedupe_overlapping(activities):
        if not a.start_time or a.start_time < cutoff or not a.raw_json:
            continue
        sport = classify_sport(a.sport_type or a.activity_type)
        if sport not in ("run", "bike", "swim"):
            continue
        try:
            raw = json.loads(a.raw_json)
        except json.JSONDecodeError:
            continue
        z = [raw.get(f"hrTimeInZone_{i}") or 0 for i in range(1, 6)]
        if sum(z):
            acc = by_sport.setdefault(sport, [0.0] * 5)
            by_sport[sport] = [x + y for x, y in zip(acc, z)]
    if not by_sport:
        return "No heart-rate zone data in the last 4 weeks."
    lines = ["| Sport | Hours | Easy (Z1–2) | Moderate (Z3) | Hard (Z4–5) |", "|---|---|---|---|---|"]
    total = [0.0] * 5
    for sport, z in sorted(by_sport.items()):
        t = sum(z)
        total = [x + y for x, y in zip(total, z)]
        lines.append(f"| {sport} | {t / 3600:.1f} | {100 * (z[0] + z[1]) / t:.0f}% | {100 * z[2] / t:.0f}% | {100 * (z[3] + z[4]) / t:.0f}% |")
    t = sum(total)
    easy, mod, hard = 100 * (total[0] + total[1]) / t, 100 * total[2] / t, 100 * (total[3] + total[4]) / t
    lines.append(f"| **all** | {t / 3600:.1f} | {easy:.0f}% | {mod:.0f}% | {hard:.0f}% |")
    if mod > 20:
        lines.append(f"\n⚠ {mod:.0f}% of time in Z3 — too much 'grey zone'. Easy days are probably not easy enough (aim ≥75–80% Z1–2).")
    elif easy >= 75:
        lines.append(f"\n✓ {easy:.0f}% easy — intensity distribution is well polarized.")
    return "\n".join(lines)


def build_session_breakdowns(analyses: list[dict]) -> str:
    """Brief rep/pacing breakdown of the last 7 days' key or flagged sessions."""
    from .analysis import render

    picked = [a for a in analyses
              if a.get("reps") or any(f["level"] == "warn" for f in a.get("flags", []))
              or a.get("surges", {}).get("matches")]
    if not picked:
        return "No interval sessions or flagged workouts in the last 7 days."
    return "\n\n".join(render(a, brief=True) for a in picked[:6])


def build_health_trends(health: list[HealthMetrics]) -> str:
    if not health:
        return "No health data available."

    lines = []
    week = health[:7]

    # HRV
    hrv_values = [h.hrv_last_night for h in week if h.hrv_last_night]
    if hrv_values:
        avg_hrv = sum(hrv_values) / len(hrv_values)
        latest_status = next((h.hrv_status for h in week if h.hrv_status), None)
        status_str = f" (status: {latest_status})" if latest_status else ""
        lines.append(f"- **HRV (7d avg)**: {avg_hrv:.0f} ms{status_str}")

    # Sleep
    sleep_scores = [h.sleep_score for h in week if h.sleep_score]
    if sleep_scores:
        avg_sleep = sum(sleep_scores) / len(sleep_scores)
        lines.append(f"- **Sleep score (7d avg)**: {avg_sleep:.0f}/100")
    sleep_durations = [h.sleep_duration_seconds for h in week if h.sleep_duration_seconds]
    if sleep_durations:
        avg_dur = sum(sleep_durations) / len(sleep_durations) / 3600
        lines.append(f"- **Sleep duration (7d avg)**: {avg_dur:.1f} hours")

    # Body battery
    bb_high = [h.body_battery_high for h in week if h.body_battery_high is not None]
    bb_low = [h.body_battery_low for h in week if h.body_battery_low is not None]
    if bb_high:
        lines.append(f"- **Body battery high (7d avg)**: {sum(bb_high) / len(bb_high):.0f}")
    if bb_low:
        lines.append(f"- **Body battery low (7d avg)**: {sum(bb_low) / len(bb_low):.0f}")

    # Stress
    stress_vals = [h.stress_avg for h in week if h.stress_avg is not None]
    if stress_vals:
        lines.append(f"- **Stress (7d avg)**: {sum(stress_vals) / len(stress_vals):.0f}")

    # Training readiness
    tr_vals = [h.training_readiness for h in week if h.training_readiness is not None]
    if tr_vals:
        lines.append(f"- **Training readiness (7d avg)**: {sum(tr_vals) / len(tr_vals):.0f}")

    # Fatigue flags
    flags = _detect_fatigue_flags(health)
    if flags:
        lines.append("\n### Fatigue Flags")
        for flag in flags:
            lines.append(f"- ⚠ {flag}")

    return "\n".join(lines) if lines else "No health trends available."


def _detect_fatigue_flags(health: list[HealthMetrics]) -> list[str]:
    flags = []
    week = health[:7]

    sleep_scores = [h.sleep_score for h in week if h.sleep_score]
    if sleep_scores and sum(s < 60 for s in sleep_scores) >= 3:
        flags.append("Poor sleep (score < 60) on 3+ days this week")

    stress_vals = [h.stress_avg for h in week if h.stress_avg is not None]
    if stress_vals and sum(stress_vals) / len(stress_vals) > 50:
        flags.append("Elevated average stress level this week")

    bb_high = [h.body_battery_high for h in week if h.body_battery_high is not None]
    if bb_high and sum(bb_high) / len(bb_high) < 50:
        flags.append("Low body battery — inadequate recovery")

    tr_vals = [h.training_readiness for h in week if h.training_readiness is not None]
    if tr_vals and sum(tr_vals) / len(tr_vals) < 40:
        flags.append("Low training readiness — consider reducing load")

    if len(health) >= 14:
        hrv_this = [h.hrv_last_night for h in health[:7] if h.hrv_last_night]
        hrv_last = [h.hrv_last_night for h in health[7:14] if h.hrv_last_night]
        if hrv_this and hrv_last:
            avg_this = sum(hrv_this) / len(hrv_this)
            avg_last = sum(hrv_last) / len(hrv_last)
            if avg_last > 0 and (avg_this - avg_last) / avg_last < -0.15:
                flags.append("HRV declining >15% vs previous week — possible overreaching")

    return flags

def build_context(output_path: Path | None = None, use_db: bool = True) -> Path:
    """Build training context from either Garmin API or local database.
    
    Args:
        output_path: Optional path for output file (default: training_context.md)
        use_db: If True, use local database. If False, fetch directly from Garmin API.
    """
    content = _build_context_md(use_db=use_db, for_file=True)
    out = output_path or CONTEXT_FILE
    out.write_text(content, encoding="utf-8")
    return out

def _build_context_md(use_db: bool = True, for_file: bool = False) -> str:
    """for_file=True builds training_context.md, which people paste into other tools — keep it minimal."""
    cfg = load_config()
    
    from .analysis import Thresholds
    from .persona import coaching_brief

    t = cfg.thresholds
    th = Thresholds(ftp_watts=t.ftp_watts, lthr_bike=t.lthr_bike or t.lthr_run, lthr_run=t.lthr_run,
                    easy_hr_cap=t.easy_hr_cap, units=cfg.coach.units)
    analyses: dict[int, dict] = {}
    personal: dict = {}
    if use_db:
        from .analysis import ANALYSIS_VERSION
        from .database import Database
        db = Database()
        health = db.get_health_metrics(since_days=90)
        activities = db.get_recent_activities(limit=14)
        history = db.get_activities(since_days=120)
        weeks = db.get_weekly_summaries(weeks=8)
        for a in db.get_activities(since_days=14):
            an = db.get_analysis(a.activity_id, ANALYSIS_VERSION)
            if an:
                analyses[a.activity_id] = an
        if cfg.cycle.eligible and cfg.cycle.tracking == "on":
            from . import cycle
            personal = cycle.load_days(db.conn, since_days=60)
        db.close()
        data_source = "Last sync data"
    else:
        from .garmin_client import get_recent_activities, get_health_metrics
        health = get_health_metrics(lookback_days=90)
        history = get_recent_activities(lookback_days=90, limit=None)
        activities = history[:14]
        weeks = build_weekly_summaries(history)
        data_source = "Live data from Garmin Connect"

    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    last_week_analyses = sorted(
        (an for an in analyses.values() if (an.get("start") or "").replace(" ", "T") >= week_ago),
        key=lambda an: an.get("start") or "", reverse=True,
    )

    today = date.today().isoformat()
    sections = [
        f"# Training Context — {today}\n",
        f"*Auto-generated by personal-ai-coach. {data_source}.*\n",
        _section("Athlete Profile", build_athlete_profile(cfg.athlete, health, cfg.thresholds)),
        _section("Readiness — day by day vs baseline",
                 build_readiness(health, personal, detailed=not for_file or cfg.cycle.share_in_context_file)),
        _section("Fitness / Fatigue / Form", build_fitness_form(history, th)),
        _section("Session Breakdowns (last 7 days)", build_session_breakdowns(last_week_analyses)),
        _section("Recent Activities (last 14)", build_recent_activities(activities, analyses, cfg.coach.units)),
        _section("Weekly Training Summary (last 8 weeks)", build_weekly_summary_table(weeks)),
        _section("Intensity Distribution (last 4 weeks)", build_intensity_distribution(history)),
        _section("Volume Trend", build_training_load(weeks)),
        _section("Health — 7-day averages & fatigue flags", build_health_trends(health)),
    ]

    # Append reference to detailed athlete docs if they exist
    athlete_docs = athlete_docs_dir()
    if athlete_docs.is_dir():
        docs = sorted(athlete_docs.glob("*.md"))
        if docs:
            ref_lines = [
                "For full athlete profile, race targets, fueling science, "
                "and coaching guidelines see:",
            ]
            for doc in docs:
                ref_lines.append(f"- `{doc}`")
            sections.append(_section("Detailed Athlete Documentation", "\n".join(ref_lines)))

    sections.append(_section("Coaching Brief (how to coach this athlete)", coaching_brief(cfg.coach.tone)))
    return "\n".join(sections)

def register_tools(mcp):
    @mcp.tool()
    def get_coach_context() -> str:
        """Get current training data: weekly load, recent activities, health trends, fatigue flags."""
        return _build_context_md(use_db=True)

    @mcp.tool()
    def get_athlete_docs() -> str:
        """Get full athlete documentation: race targets, fueling strategy, gear, injury history, coaching guidelines. Call this for deep strategy questions."""
        athlete_docs = athlete_docs_dir()
        if not athlete_docs.is_dir():
            return "No athlete documentation found. Add markdown files to the athlete_docs/ directory."
        docs = sorted(athlete_docs.glob("*.md"))
        if not docs:
            return "athlete_docs/ directory exists but contains no markdown files."
        sections = []
        for doc in docs:
            sections.append(f"# {doc.stem}\n\n{doc.read_text(encoding='utf-8')}")
        return "\n\n---\n\n".join(sections)

    return mcp
