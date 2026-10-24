"""Optional menstrual-cycle awareness for readiness and coaching.

Privacy model:
- Offered only when the Garmin profile is female; never mentioned otherwise.
- Off until the athlete explicitly opts in (config: cycle.tracking = "on").
- Data stays in the local database; turning it off deletes it.
- Excluded from training_context.md (which people paste into other AIs) unless
  cycle.share_in_context_file is true. The file then only says a personalized
  adjustment was applied.
- Never logged.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

_TABLE = """
CREATE TABLE IF NOT EXISTS cycle_days (
    metric_date TEXT PRIMARY KEY,
    day_in_cycle INTEGER,
    phase TEXT,
    cycle_length INTEGER,
    days_to_next_cycle INTEGER,
    flow TEXT,
    symptoms TEXT,
    flags TEXT
);
"""

# Typical luteal-phase shift vs the athlete's overall baseline (resting HR up, HRV down).
# Applied as extra tolerance so expected hormonal changes aren't read as fatigue.
_ALLOWANCE = {
    "luteal": {"rhr": 2, "hrv_pct": 0.05},
    "late_luteal": {"rhr": 3, "hrv_pct": 0.07},
}


def phase_for(day: int, cycle_len: int | None, period_len: int | None, luteal_start: int | None) -> str:
    cycle_len = cycle_len or 28
    period_len = period_len or 5
    luteal_start = luteal_start or max(cycle_len - 14, period_len + 2)
    if day <= period_len:
        return "menstrual"
    if day < luteal_start - 2:
        return "follicular"
    if day <= luteal_start:
        return "ovulatory"
    if cycle_len - day < 5:
        return "late_luteal"
    return "luteal"


def _find(d, key):
    """Depth-first search for a key in Garmin's nested payloads."""
    if isinstance(d, dict):
        if key in d:
            return d[key]
        for v in d.values():
            r = _find(v, key)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = _find(v, key)
            if r is not None:
                return r
    return None


def parse_day(day: str, payload: dict | None) -> dict | None:
    """Turn a Garmin day-view payload into one stored row (None if there's no cycle data)."""
    if not payload:
        return None
    summary = _find(payload, "daySummary") or payload
    day_in = _find(summary, "dayInCycle")
    if not day_in:
        return None
    if _find(summary, "pregnancyCycle"):
        return {"metric_date": day, "day_in_cycle": day_in, "phase": "pregnancy", "cycle_length": None,
                "days_to_next_cycle": None, "flow": None, "symptoms": "[]", "flags": "[]"}
    cycle_len = _find(summary, "predictedCycleLength") or _find(summary, "cycleLength")
    phase = phase_for(day_in, cycle_len, _find(summary, "periodLength"), _find(summary, "lutealPhaseStart"))
    flags = []
    if _find(payload, "hasAmenorrhea"):
        flags.append("amenorrhea")
    if _find(payload, "isAtypical"):
        flags.append("atypical_cycle")
    if _find(payload, "perimenopauseDetected"):
        flags.append("perimenopause")
    return {
        "metric_date": day,
        "day_in_cycle": day_in,
        "phase": phase,
        "cycle_length": cycle_len,
        "days_to_next_cycle": (cycle_len - day_in + 1) if cycle_len else None,
        "flow": _find(payload, "flow"),
        "symptoms": json.dumps(_find(payload, "symptoms") or []),
        "flags": json.dumps(flags),
    }


# ── local storage ──

def save_days(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executescript(_TABLE)
    for r in rows:
        conn.execute(
            """INSERT INTO cycle_days VALUES (:metric_date, :day_in_cycle, :phase, :cycle_length,
               :days_to_next_cycle, :flow, :symptoms, :flags)
               ON CONFLICT(metric_date) DO UPDATE SET day_in_cycle=excluded.day_in_cycle, phase=excluded.phase,
               cycle_length=excluded.cycle_length, days_to_next_cycle=excluded.days_to_next_cycle,
               flow=excluded.flow, symptoms=excluded.symptoms, flags=excluded.flags""",
            r,
        )
    conn.commit()


def load_days(conn: sqlite3.Connection, since_days: int = 60) -> dict[date, dict]:
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='cycle_days'"
    ).fetchone()
    if not exists:
        return {}
    cutoff = (date.today() - timedelta(days=since_days)).isoformat()
    rows = conn.execute("SELECT * FROM cycle_days WHERE metric_date >= ?", (cutoff,)).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        d["symptoms"] = json.loads(d.get("symptoms") or "[]")
        d["flags"] = json.loads(d.get("flags") or "[]")
        out[date.fromisoformat(d["metric_date"])] = d
    return out


def purge(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS cycle_days")
    conn.commit()


# ── coaching ──

def allowance(day_row: dict | None) -> dict:
    if not day_row:
        return {}
    return _ALLOWANCE.get(day_row.get("phase"), {})


_NOTES = {
    "menstrual": "Check symptoms before intensity: cramps, low energy or poor sleep → keep it aerobic and move the key "
                 "session 1–2 days. Feeling good → train as planned; performance is often unaffected. Heavy flow + "
                 "fatigue → keep iron/ferritin on the radar.",
    "follicular": "Often a good window for hard sessions and tests if recovery markers agree — but individual "
                  "response varies, so let the data and how she feels decide.",
    "ovulatory": "No change needed. If plyometrics or change-of-direction work is planned, emphasise landing mechanics.",
    "luteal": "Resting HR a little higher and HRV a little lower are expected now (readiness thresholds already "
              "adjusted). Body temperature is up: heat tolerance is lower, so hydrate with sodium and fuel carbs well; "
              "the same pace can feel harder (higher RPE).",
    "late_luteal": "Premenstrual days: sleep and mood can dip and perceived effort rises. Expected marker shifts are "
                   "already allowed for; keep the plan unless symptoms say otherwise and protect sleep.",
    "pregnancy": "Pregnancy recorded in Garmin: no automatic adjustments — training should follow her clinician's guidance.",
}


def coaching_note(day_row: dict | None, detailed: bool) -> str | None:
    """One-line context for the coach. `detailed=False` reveals nothing about the cycle itself."""
    if not day_row:
        return None
    if not detailed:
        return "*Personalized baseline adjustment applied where relevant.*" if allowance(day_row) else None
    phase = day_row["phase"]
    parts = [f"**Cycle**: day {day_row['day_in_cycle']}, {phase.replace('_', ' ')} phase"]
    if day_row.get("days_to_next_cycle"):
        parts.append(f"next period in ~{day_row['days_to_next_cycle']} d")
    if day_row.get("symptoms"):
        parts.append("logged: " + ", ".join(str(s).lower().replace("_", " ") for s in day_row["symptoms"][:6]))
    lines = [" · ".join(parts), f"- {_NOTES.get(phase, '')}"]
    if "amenorrhea" in day_row.get("flags", []):
        lines.append("- ⚠ Garmin flags missing periods. In an athlete this is a classic sign of low energy availability "
                     "(RED-S): check she is eating enough for her training and recommend a sports-medicine doctor. "
                     "Do not increase load until this is addressed.")
    elif "atypical_cycle" in day_row.get("flags", []):
        lines.append("- Cycle flagged atypical by Garmin — watch energy availability, stress and sleep.")
    return "\n".join(lines)
