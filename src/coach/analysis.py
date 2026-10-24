"""Per-workout analysis: laps, reps, pacing, surges, stops and coaching flags.

Pure functions — no Garmin or database access. Inputs are the raw activity
summary (list-endpoint shape), the lap list from `get_activity_splits()` and,
optionally, the second-by-second stream from `get_activity_details()`.
The result is a plain dict so it can be cached as JSON.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median, pstdev

ANALYSIS_VERSION = 1

M_PER_MI = 1609.344


@dataclass
class Thresholds:
    ftp_watts: float | None = None
    lthr_bike: float | None = None
    lthr_run: float | None = None
    easy_hr_cap: float | None = None
    units: str = "metric"  # metric | imperial


# ── formatting ──

def fmt_clock(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_pace(speed_mps: float | None, units: str = "metric") -> str:
    if not speed_mps or speed_mps <= 0.3:
        return "-"
    per = M_PER_MI if units == "imperial" else 1000
    return f"{fmt_clock(per / speed_mps)}/{'mi' if units == 'imperial' else 'km'}"


def fmt_dist(meters: float | None, units: str = "metric") -> str:
    if not meters:
        return "-"
    if units == "imperial":
        return f"{meters / M_PER_MI:.2f} mi"
    return f"{meters / 1000:.2f} km"


# ── stream ──

class Stream:
    """1 Hz grid over elapsed time. Missing samples (watch paused) are None."""

    def __init__(self, details: dict):
        idx = {m["key"]: m["metricsIndex"] for m in details.get("metricDescriptors", [])}
        rows = [r["metrics"] for r in details.get("activityDetailMetrics", []) if r.get("metrics")]
        ts_i = idx.get("directTimestamp")
        if ts_i is None or not rows:
            self.n = 0
            self.power = self.hr = self.speed = self.cad = []
            self.gaps: list[tuple[int, int]] = []
            return

        def col(*keys):
            for k in keys:
                if k in idx:
                    return idx[k]
            return None

        p_i = col("directPower")
        h_i = col("directHeartRate")
        s_i = col("directGradeAdjustedSpeed", "directSpeed")
        c_i = col("directBikeCadence", "directRunCadence")
        t0 = rows[0][ts_i] / 1000
        samples = {}
        for r in rows:
            if r[ts_i] is None:
                continue
            samples[int(r[ts_i] / 1000 - t0)] = r
        self.n = max(samples) + 1
        self.power: list[float | None] = [None] * self.n
        self.hr: list[float | None] = [None] * self.n
        self.speed: list[float | None] = [None] * self.n
        self.cad: list[float | None] = [None] * self.n
        self.gaps = []
        keys = sorted(samples)
        prev = None
        for k in keys:
            r = samples[k]
            if prev is not None and k - prev > 1:
                gap = k - prev
                if gap <= 3:  # sampling jitter: hold the last value
                    for j in range(prev + 1, k):
                        self._set(j, samples[prev], p_i, h_i, s_i, c_i)
                else:
                    self.gaps.append((prev + 1, gap - 1))
            self._set(k, r, p_i, h_i, s_i, c_i)
            prev = k

    def _set(self, j, r, p_i, h_i, s_i, c_i):
        g = lambda i: r[i] if i is not None and i < len(r) else None  # noqa: E731
        self.power[j], self.hr[j], self.speed[j], self.cad[j] = g(p_i), g(h_i), g(s_i), g(c_i)

    def has_power(self) -> bool:
        return any(p for p in self.power if p)

    def stops(self, min_s: int = 10) -> list[tuple[int, int]]:
        """(start, duration) of pauses/standstills ≥ min_s, excluding the very start/end."""
        out = list(self.gaps)
        run_start = None
        for i, s in enumerate(self.speed):
            stopped = s is not None and s < 0.5
            if stopped and run_start is None:
                run_start = i
            elif not stopped and run_start is not None:
                if i - run_start >= min_s:
                    out.append((run_start, i - run_start))
                run_start = None
        out = [(a, d) for a, d in out if d >= min_s and a > 60 and a + d < self.n - 60]
        return sorted(out)


def _mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _rolling(vals: list[float | None], w: int) -> list[float]:
    """Trailing w-second average; missing samples count as 0 (coasting/paused)."""
    out, acc, q = [], 0.0, []
    for v in vals:
        v = v or 0.0
        q.append(v)
        acc += v
        if len(q) > w:
            acc -= q.pop(0)
        out.append(acc / len(q))
    return out


def _np(power: list[float | None]) -> float | None:
    if len([p for p in power if p is not None]) < 30:
        return None
    r = _rolling(power, 30)[29:]
    return (sum(x ** 4 for x in r) / len(r)) ** 0.25 if r else None


def _segments(mask: list[bool], min_len: int, merge_gap: int) -> list[tuple[int, int]]:
    segs, start = [], None
    for i, m in enumerate(mask + [False]):
        if m and start is None:
            start = i
        elif not m and start is not None:
            segs.append([start, i])
            start = None
    merged: list[list[int]] = []
    for s in segs:
        if merged and s[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    return [(a, b) for a, b in merged if b - a >= min_len]


# ── laps ──

def _lap_offset(lap: dict, first: dict) -> int:
    try:
        a = datetime.fromisoformat(lap["startTimeGMT"].replace("Z", ""))
        b = datetime.fromisoformat(first["startTimeGMT"].replace("Z", ""))
        return int((a - b).total_seconds())
    except Exception:
        return 0


def _norm_laps(laps: list[dict], sport: str) -> list[dict]:
    if not laps:
        return []
    first = laps[0]
    out = []
    for l in laps:
        out.append({
            "n": l.get("lapIndex"),
            "type": (l.get("intensityType") or "").upper(),
            "step": l.get("wktStepIndex"),
            "start": _lap_offset(l, first),
            "dur": l.get("movingDuration") or l.get("duration") or 0,
            "elapsed": l.get("elapsedDuration") or l.get("duration") or 0,
            "dist": l.get("distance") or 0,
            "avg_hr": l.get("averageHR"),
            "max_hr": l.get("maxHR"),
            "avg_power": l.get("averagePower"),
            "np": l.get("normalizedPower"),
            "speed": l.get("averageMovingSpeed") or l.get("averageSpeed"),
            "cad": l.get("averageBikeCadence") if sport == "bike" else l.get("averageRunCadence"),
        })
    return out


def _main_set(reps: list[dict]) -> list[dict]:
    """Reps of similar duration grouped together; the group with the most work time wins.

    Keeps 1-minute openers from being compared with a 20-minute test effort.
    """
    groups: list[list[dict]] = []
    for r in reps:
        for g in groups:
            if abs(r["dur"] - g[0]["dur"]) <= 0.25 * max(r["dur"], g[0]["dur"]):
                g.append(r)
                break
        else:
            groups.append([r])
    return max(groups, key=lambda g: sum(x["dur"] for x in g)) if groups else []


def _structured_reps(laps: list[dict]) -> list[dict]:
    """Group consecutive ACTIVE laps of the same workout step into one rep."""
    if not any(l["step"] is not None for l in laps):
        return []
    reps: list[dict] = []
    prev = None
    for l in laps:
        if l["type"] != "ACTIVE":
            prev = None
            continue
        if prev is not None and prev["step"] == l["step"]:
            r = reps[-1]
            tot = r["dur"] + l["dur"]
            for k in ("avg_hr", "avg_power", "np", "speed", "cad"):
                if r.get(k) is not None and l.get(k) is not None and tot:
                    r[k] = (r[k] * r["dur"] + l[k] * l["dur"]) / tot
            r["max_hr"] = max(x for x in (r.get("max_hr"), l.get("max_hr"), 0) if x is not None)
            r["dur"] = tot
            r["end"] = l["start"] + l["elapsed"]
        else:
            r = dict(l)
            r["end"] = l["start"] + l["elapsed"]
            reps.append(r)
        prev = l
    return reps


# ── main ──

def classify(sport_type: str) -> str:
    """Same categories as the weekly summaries (single source of truth in utils)."""
    from .utils import classify_sport

    return classify_sport(sport_type or "")


def analyze(summary: dict, laps_raw: list[dict], details: dict | None, th: Thresholds) -> dict:
    sport = classify((summary.get("activityType") or {}).get("typeKey", ""))
    st = Stream(details) if details else None
    laps = _norm_laps(laps_raw, sport)
    moving = summary.get("movingDuration") or summary.get("duration") or 0
    elapsed = summary.get("elapsedDuration") or summary.get("duration") or 0
    lthr = th.lthr_bike if sport == "bike" else th.lthr_run
    ftp = th.ftp_watts if sport == "bike" else None
    flags: list[dict] = []

    def flag(level: str, text: str):
        flags.append({"level": level, "text": text})

    a: dict = {
        "version": ANALYSIS_VERSION,
        "activity_id": summary.get("activityId"),
        "sport": sport,
        "name": summary.get("activityName", ""),
        "start": summary.get("startTimeLocal"),
        "moving_s": moving,
        "elapsed_s": elapsed,
        "units": th.units,
        "ftp": ftp,
        "lthr": lthr,
    }

    np_ = summary.get("normPower")
    a["summary"] = {
        "dist": summary.get("distance"),
        "avg_hr": summary.get("averageHR"),
        "max_hr": summary.get("maxHR"),
        "avg_power": summary.get("avgPower"),
        "np": np_,
        "if": (np_ / ftp) if np_ and ftp else summary.get("intensityFactor"),
        "vi": (np_ / summary["avgPower"]) if np_ and summary.get("avgPower") else None,
        "tss": summary.get("trainingStressScore"),
        "speed": summary.get("averageSpeed"),
        "rpe": summary.get("directWorkoutRpe"),
        "feel": summary.get("directWorkoutFeel"),
    }

    # Garmin's own zone times (no extra API call)
    hz = [summary.get(f"hrTimeInZone_{i}") or 0 for i in range(1, 6)]
    pz = [summary.get(f"powerTimeInZone_{i}") or 0 for i in range(1, 8)]
    a["zones"] = {
        "hr_pct": [round(100 * z / sum(hz)) for z in hz] if sum(hz) else None,
        "power_pct": [round(100 * z / sum(pz)) for z in pz] if sum(pz) else None,
    }
    a["best_power"] = {
        label: summary.get(f"maxAvgPower_{sec}")
        for label, sec in (("5s", 5), ("1m", 60), ("5m", 300), ("20m", 1200), ("60m", 3600))
        if summary.get(f"maxAvgPower_{sec}")
    }
    a["laps"] = laps

    # ── stops ──
    stops = st.stops() if st and st.n else []
    a["stops"] = [{"at": s, "dur": d} for s, d in stops]
    stopped_total = sum(d for _, d in stops)

    # ── reps ──
    reps = _structured_reps(laps)
    rep_source = "workout steps" if reps else None
    if not reps and st and st.n:
        if sport == "bike" and ftp and st.has_power():
            r30 = _rolling(st.power, 30)
            segs = _segments([x >= 0.88 * ftp for x in r30], min_len=90, merge_gap=20)
            reps = [{"start": max(0, s - 30), "end": e, "dur": e - max(0, s - 30)} for s, e in segs]
        elif sport == "run" and lthr:
            hr = [h or 0 for h in st.hr]
            segs = _segments([h >= 0.92 * lthr for h in hr], min_len=180, merge_gap=30)
            reps = [{"start": s, "end": e, "dur": e - s} for s, e in segs]
        # A lone short block in an unstructured session is a surge, not an interval set
        main = _main_set(reps)
        if len(main) < 2 and sum(r["dur"] for r in main) < 0.15 * moving:
            reps = []
        rep_source = "auto-detected" if reps else None
    for i, r in enumerate(reps, 1):
        r["i"] = i
        if st and st.n:
            seg = slice(int(r["start"]), int(min(r["end"], st.n)))
            if sport == "bike" and st.has_power():
                r["avg_power"] = _mean(st.power[seg]) if r.get("avg_power") is None else r["avg_power"]
                r["np"] = r.get("np") or _np(st.power[seg])
            r["avg_hr"] = r.get("avg_hr") or _mean(st.hr[seg])
            hr_seg = [h for h in st.hr[seg] if h]
            r["max_hr"] = r.get("max_hr") or (max(hr_seg) if hr_seg else None)
            r["hr_end"] = _mean(st.hr[max(seg.start, seg.stop - 30):seg.stop])
            r["speed"] = r.get("speed") or _mean(st.speed[seg])
            r["cad"] = r.get("cad") or _mean([c for c in st.cad[seg] if c])
            r["stops_inside"] = [(s, d) for s, d in stops if seg.start <= s < seg.stop]
            if r["dur"] >= 480 and sport == "bike" and st.has_power():
                thirds = [_mean(st.power[seg.start + k * (seg.stop - seg.start) // 3:
                                         seg.start + (k + 1) * (seg.stop - seg.start) // 3]) for k in range(3)]
                r["thirds"] = thirds
        if ftp and r.get("avg_power"):
            r["pct_ftp"] = 100 * (r.get("np") or r["avg_power"]) / ftp
        if lthr and r.get("avg_hr"):
            r["pct_lthr"] = 100 * r["avg_hr"] / lthr
    main = _main_set(reps)
    for r in main:
        r["main"] = True
    a["reps"] = reps
    a["rep_source"] = rep_source

    # ── rep execution flags (main set only: like is compared with like) ──
    metric = "avg_power" if sport == "bike" and main and main[0].get("avg_power") else "speed"
    vals = [r.get(metric) for r in main if r.get(metric)]
    if len(vals) >= 3:
        rest_med = median(vals[1:])
        if rest_med and vals[0] > rest_med * 1.05:
            flag("warn", f"Rep 1 went out too hard: {_fmtv(vals[0], metric, th)} vs {_fmtv(rest_med, metric, th)} median of the rest (+{100 * (vals[0] / rest_med - 1):.0f}%).")
        if vals[-1] < vals[0] * 0.95:
            flag("warn", f"Faded: last rep {_fmtv(vals[-1], metric, th)} vs first {_fmtv(vals[0], metric, th)} ({100 * (vals[-1] / vals[0] - 1):.0f}%).")
        cv = pstdev(vals) / (sum(vals) / len(vals))
        if cv <= 0.03:
            flag("good", f"Even reps: {len(vals)} reps within ±{100 * cv:.0f}% of each other.")
        elif cv > 0.06:
            flag("warn", f"Uneven reps: variation {100 * cv:.0f}% between reps.")
        hrs = [r.get("avg_hr") for r in main if r.get("avg_hr")]
        if len(hrs) == len(main) and hrs[-1] - hrs[0] >= 6 and vals[-1] >= vals[0] * 0.97:
            flag("info", f"HR crept {hrs[0]:.0f} → {hrs[-1]:.0f} bpm at the same output — cardiovascular drift/fatigue.")
    for r in reps:
        for s, d in r.get("stops_inside", []):
            flag("warn", f"Rep {r['i']} was not continuous: stopped {d}s at {fmt_clock(s)} — numbers for that rep are flattered by the rest.")
        th3 = r.get("thirds")
        if th3 and all(th3):
            if th3[0] > th3[2] * 1.06:
                flag("warn", f"Rep {r['i']} ({fmt_clock(r['dur'])}) started too hard: {th3[0]:.0f} W first third → {th3[2]:.0f} W last third.")
            elif th3[2] > th3[0] * 1.04:
                flag("good", f"Rep {r['i']} paced well: built {th3[0]:.0f} → {th3[2]:.0f} W (negative split).")
        if sport == "bike" and r.get("cad") and r["dur"] >= 300 and r["cad"] < 82:
            flag("info", f"Rep {r['i']} cadence {r['cad']:.0f} rpm — low for sustained work (aim ≥85).")
    if reps and ftp:
        top = max(reps, key=lambda r: r.get("pct_ftp") or 0)
        a["max_rep_pct_ftp"] = top.get("pct_ftp")

    # ── surges / matches (bike) ──
    if sport == "bike" and st and st.n and ftp and st.has_power():
        r30 = _rolling(st.power, 30)
        match_segs = _segments([x >= 1.2 * ftp for x in r30], min_len=10, merge_gap=10)
        above = sum(1 for p in st.power if p and p > ftp)
        a["surges"] = {
            "matches": len(match_segs),
            "above_ftp_s": above,
            "top": sorted(
                [{"at": s, "dur": e - s, "w30": max(r30[s:e])} for s, e in match_segs],
                key=lambda m: -m["w30"])[:5],
        }
        if not reps and (len(match_segs) >= 4 or above >= 600):
            flag("warn", f"Surgy ride: {len(match_segs)} efforts ≥120% FTP (30-s power) and {fmt_clock(above)} above FTP in total — this was not a steady endurance ride.")

    # ── strides (run) ──
    if sport == "run" and st and st.n:
        sp = [s for s in st.speed if s and s > 0.5]
        if sp:
            base = median(sp)
            r5 = _rolling(st.speed, 5)
            strides = _segments([x >= base * 1.25 for x in r5], min_len=8, merge_gap=3)
            a["strides"] = len([1 for s, e in strides if e - s <= 45])

    # ── easy / steady checks ──
    steady = not reps or sum(r["dur"] for r in reps) < 0.2 * moving
    if steady and st and st.n and moving >= 2400:
        skip = 600
        idx = [i for i in range(skip, st.n) if st.hr[i] and (st.speed[i] or 0) > 0.5]
        if len(idx) > 1200:
            h1, h2 = idx[: len(idx) // 2], idx[len(idx) // 2:]
            if sport == "bike" and st.has_power():
                ef = lambda ix: (_mean([st.power[i] for i in ix]) or 0) / (_mean([st.hr[i] for i in ix]) or 1)  # noqa: E731
            else:
                ef = lambda ix: (_mean([st.speed[i] for i in ix]) or 0) / (_mean([st.hr[i] for i in ix]) or 1)  # noqa: E731
            e1, e2 = ef(h1), ef(h2)
            if e1:
                dec = 100 * (e1 - e2) / e1
                a["decoupling_pct"] = dec
                if dec > 5:
                    flag("warn", f"Aerobic decoupling {dec:.1f}% (output per heartbeat dropped in the 2nd half) — durability/fueling/heat limit at this intensity.")
                elif 0 <= dec < 3:
                    flag("good", f"Decoupling {dec:.1f}% — aerobically solid at this intensity.")
    if steady and th.easy_hr_cap and st and st.n and sport in ("run", "bike"):
        hr_vals = [h for h in st.hr if h]
        if hr_vals:
            over = sum(1 for h in hr_vals if h > th.easy_hr_cap)
            pct = 100 * over / len(hr_vals)
            a["pct_above_easy_cap"] = pct
            if pct > 15 and not a.get("surges", {}).get("matches"):
                flag("warn", f"Easy session drifted: {pct:.0f}% of the time above your easy cap of {th.easy_hr_cap:.0f} bpm.")

    if stopped_total >= 120:
        flag("info", f"{len(stops)} stops, {fmt_clock(stopped_total)} standing still (lights, traffic or paused watch).")
    if elapsed and moving and elapsed - moving > 900 and not stops:
        flag("info", f"Elapsed {fmt_clock(elapsed)} vs moving {fmt_clock(moving)} — watch likely left running after the session.")

    a["session_type"] = _session_type(a, reps, sport, ftp)
    a["flags"] = flags
    return a


def _fmtv(v: float, metric: str, th: Thresholds) -> str:
    return f"{v:.0f} W" if metric == "avg_power" else fmt_pace(v, th.units)


def _session_type(a: dict, reps: list[dict], sport: str, ftp: float | None) -> str:
    main = [r for r in reps if r.get("main")]
    if main:
        n, dur = len(main), main[0]["dur"]
        desc = f"{n}×{fmt_clock(dur)}"
        if n == 1 and dur >= 1080 and (main[0].get("pct_ftp") or 0) >= 98:
            return f"test / time trial ({fmt_clock(dur)})"
        pct = max((r.get("pct_ftp") or 0) for r in main)
        if sport == "bike" and pct >= 105 and dur <= 360:
            return f"VO2 intervals {desc}"
        if sport == "bike" and pct >= 94:
            return f"threshold intervals {desc}"
        if sport == "bike" and pct >= 84:
            return f"sweet-spot intervals {desc}"
        return f"intervals {desc}"
    if a.get("surges", {}).get("matches", 0) >= 4:
        return "surgy / group ride"
    if a["moving_s"] >= 5400:
        return "long endurance"
    return "steady / easy"


# ── rendering ──

def render(a: dict, brief: bool = False) -> str:
    u = a.get("units", "metric")
    s = a["summary"]
    sport = a["sport"]
    head = f"### {(a.get('start') or '')[:16]} · {sport} · {a.get('name') or ''} — {a['session_type']}"
    parts = [f"moving {fmt_clock(a['moving_s'])}"]
    if a["elapsed_s"] and a["elapsed_s"] - a["moving_s"] > 120:
        parts.append(f"elapsed {fmt_clock(a['elapsed_s'])}")
    if s.get("dist"):
        parts.append(fmt_dist(s["dist"], u))
    if s.get("avg_hr"):
        parts.append(f"HR {s['avg_hr']:.0f} avg / {s['max_hr']:.0f} max")
    if sport == "bike" and s.get("np"):
        parts.append(f"NP {s['np']:.0f} W (avg {s['avg_power']:.0f})")
        if s.get("if"):
            parts.append(f"IF {s['if']:.2f}")
        if s.get("vi"):
            parts.append(f"VI {s['vi']:.2f}")
    if sport == "run" and s.get("speed"):
        parts.append(f"pace {fmt_pace(s['speed'], u)}")
    if s.get("tss"):
        parts.append(f"TSS {s['tss']:.0f}")
    if s.get("rpe"):
        parts.append(f"RPE {s['rpe'] / 10:.0f}/10" if s["rpe"] > 10 else f"RPE {s['rpe']}/10")
    lines = [head, "- " + " · ".join(parts)]
    if a.get("notes"):
        lines.append(f"- Athlete's note: \"{a['notes'].strip()[:300]}\"")

    reps = a.get("reps") or []
    if reps:
        lines.append(f"- **Reps** ({a.get('rep_source')}):")
        hdr = "| # | at | time | " + ("power (NP) | %FTP | " if sport == "bike" else "pace | ") + "HR avg→end / max | cad |"
        lines += [hdr, "|" + "---|" * (hdr.count("|") - 1)]
        for r in reps[: 12 if brief else 40]:
            row = f"| {r['i']}{'' if r.get('main') else ' (aux)'} | {fmt_clock(r['start'])} | {fmt_clock(r['dur'])} | "
            if sport == "bike":
                pw = f"{r['avg_power']:.0f}" if r.get("avg_power") else "-"
                if r.get("np"):
                    pw += f" ({r['np']:.0f})"
                row += f"{pw} | {r['pct_ftp']:.0f}% | " if r.get("pct_ftp") else f"{pw} | - | "
            else:
                row += f"{fmt_pace(r.get('speed'), u)} | "
            hr = f"{r['avg_hr']:.0f}" if r.get("avg_hr") else "-"
            if r.get("hr_end"):
                hr += f"→{r['hr_end']:.0f}"
            if r.get("max_hr"):
                hr += f" / {r['max_hr']:.0f}"
            row += f"{hr} | {r['cad']:.0f} |" if r.get("cad") else f"{hr} | - |"
            lines.append(row)
    elif a.get("laps") and len(a["laps"]) > 1 and sport in ("run", "bike"):
        laps = a["laps"]
        if sport == "run":
            splits = ", ".join(fmt_pace(l["speed"], u).split("/")[0] for l in laps[:20])
            hrs = " → ".join(f"{l['avg_hr']:.0f}" for l in laps[:20] if l.get("avg_hr"))
            lines.append(f"- Lap splits: {splits}  |  HR: {hrs}")
        elif not brief:
            pw = ", ".join(f"{l['avg_power']:.0f}" for l in laps[:20] if l.get("avg_power"))
            if pw:
                lines.append(f"- Lap avg power: {pw} W")

    sg = a.get("surges")
    if sg and (sg["matches"] or sg["above_ftp_s"] > 60):
        top = "; ".join(f"{fmt_clock(m['at'])} {m['w30']:.0f} W/{m['dur']}s" for m in sg["top"][:3])
        lines.append(f"- Surges ≥120% FTP: {sg['matches']} · time above FTP {fmt_clock(sg['above_ftp_s'])}" + (f" · biggest: {top}" if top else ""))
    if a.get("strides"):
        lines.append(f"- Strides/pickups detected: {a['strides']}")
    if not brief:
        if a.get("best_power"):
            lines.append("- Best power: " + ", ".join(f"{k} {v:.0f} W" for k, v in a["best_power"].items()))
        z = a.get("zones", {})
        if z.get("hr_pct"):
            lines.append("- HR zones (Garmin Z1–Z5): " + " / ".join(f"{p}%" for p in z["hr_pct"]))
        if z.get("power_pct"):
            lines.append("- Power zones (Garmin Z1–Z7): " + " / ".join(f"{p}%" for p in z["power_pct"]))
        if a.get("stops"):
            lines.append("- Stops: " + ", ".join(f"{fmt_clock(x['at'])} ({x['dur']}s)" for x in a["stops"][:8]))
    if a.get("decoupling_pct") is not None:
        lines.append(f"- Aerobic decoupling: {a['decoupling_pct']:.1f}%")
    icon = {"warn": "⚠", "good": "✓", "info": "·"}
    for f in a.get("flags", []):
        if brief and f["level"] == "info":
            continue
        lines.append(f"- {icon.get(f['level'], '-')} {f['text']}")
    return "\n".join(lines)
