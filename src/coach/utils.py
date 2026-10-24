from __future__ import annotations

from datetime import date, timedelta
from typing import List

from .models import Activity, WeeklySummary

# Categories that are logged but are not training time
NON_TRAINING = {"walk", "multisport"}


def classify_sport(sport_type: str) -> str:
    """Classify a raw Garmin sport/activity type into a simple category."""
    s = sport_type.lower()
    if "multi_sport" in s or "multisport" in s:
        return "multisport"
    if "transition" in s:
        return "other"
    if "swim" in s or "pool" in s:
        return "swim"
    if "run" in s or "jog" in s or "trail" in s or "treadmill" in s:
        return "run"
    if (
        "cycl" in s or "bike" in s or "bik" in s
        or "virtual_ride" in s or "virtual ride" in s
    ):
        return "bike"
    if "strength" in s or "weight" in s:
        return "strength"
    if "yoga" in s or "pilates" in s or "mobility" in s or "stretch" in s or "breath" in s:
        return "mobility"
    if "walk" in s or "hik" in s:
        return "walk"
    return "other"


def _dedupe_overlapping(activities: List[Activity]) -> List[Activity]:
    """Drop duplicate recordings of the same session (e.g. watch + bike computer).

    Two activities of the same category that overlap for at least half of the
    shorter one are treated as one session. The recording with TSS (power data)
    wins; otherwise the longer one.
    """
    kept: list[Activity] = []
    for a in sorted((x for x in activities if x.start_time), key=lambda x: x.start_time):
        cat = classify_sport(a.sport_type or a.activity_type)
        a_start = a.start_time.timestamp()
        a_end = a_start + a.duration_seconds
        dup_index = None
        for i, k in enumerate(kept):
            if classify_sport(k.sport_type or k.activity_type) != cat:
                continue
            k_start = k.start_time.timestamp()
            k_end = k_start + k.duration_seconds
            overlap = min(a_end, k_end) - max(a_start, k_start)
            shorter = min(a.duration_seconds, k.duration_seconds) or 1
            if overlap >= 0.5 * shorter:
                dup_index = i
                break
        if dup_index is None:
            kept.append(a)
            continue
        current = kept[dup_index]
        if (a.tss and not current.tss) or (
            bool(a.tss) == bool(current.tss) and a.duration_seconds > current.duration_seconds
        ):
            kept[dup_index] = a
    return kept


def build_weekly_summaries(activities: List[Activity]) -> List[WeeklySummary]:
    """Build weekly summaries from a list of activities.

    Multisport parents and walks are excluded from training hours (the
    multisport legs are stored as their own activities), and duplicate
    recordings of the same session are counted once. The result is sorted
    newest-first.
    """
    if not activities:
        return []

    weeks: dict[date, WeeklySummary] = {}
    for a in _dedupe_overlapping(activities):
        d = a.start_time.date()
        week_start = d - timedelta(days=d.weekday())
        if week_start not in weeks:
            weeks[week_start] = WeeklySummary(week_start=week_start)
        w = weeks[week_start]
        hours = a.duration_seconds / 3600
        km = a.distance_meters / 1000

        sport = classify_sport(a.sport_type or a.activity_type)
        if sport in NON_TRAINING:
            if sport == "walk":
                w.other_sessions += 1
            continue
        if sport == "swim":
            w.swim_hours += hours
            w.swim_km += km
            w.swim_sessions += 1
        elif sport == "bike":
            w.bike_hours += hours
            w.bike_km += km
            w.bike_sessions += 1
        elif sport == "run":
            w.run_hours += hours
            w.run_km += km
            w.run_sessions += 1
        elif sport == "strength":
            w.strength_sessions += 1
        else:
            w.other_sessions += 1

        w.total_hours += hours
        if a.tss:
            w.total_tss += a.tss

    return sorted(weeks.values(), key=lambda w: w.week_start, reverse=True)
