from mcp.server.fastmcp import FastMCP
from coach import context, garmin_client
from coach.config import load_config, save_config
from coach.persona import coaching_brief

WORKFLOW = """
## On first use or when the profile is incomplete

Call get_profile first. If sport, race_name, race_date, or goals are empty, onboard the user by asking these questions one at a time (don't dump them all at once):

1. "What are you training for?" — running / cycling / triathlon
2. Based on answer:
   - Running: "What distance?" — 5K, 10K, Half Marathon, Marathon, Ultra
   - Triathlon: "What distance?" — Sprint, Olympic, 70.3 Half Ironman, or Full Ironman (140.6)
   - Cycling: "What kind of event?" — gran fondo, road race, century, gravel, etc.
3. "What's your goal race and when is it?"
4. "What's your goal — finish strong, a specific time, or something else?"
5. "First time at this distance, or have you done it before?"
6. "How many hours per week can you realistically train?"
7. "Any injury history or areas to be careful about?"
8. "Do you know your FTP, threshold heart rate, or an easy-run HR cap?" (skip if they don't — Garmin's values are used)

Then call save_profile with their answers before proceeding.

If get_profile lists `cycle_tracking: available`, ask once — privately, neutrally, without assumptions — whether she
wants her Garmin cycle data used to personalize readiness and coaching, and save the answer with
save_profile(cycle_tracking="on" or "off"). Never raise the topic if get_profile doesn't list it, and don't
bring it up again after an answer. When it's on, use the cycle line in the readiness section as context, never
as a reason to label her or to lower ambitions on its own; how she feels and her markers decide.

## Tools

- get_coach_context — always call first for any coaching question: readiness vs baseline, fitness/fatigue/form, last week's session breakdowns, recent activities, intensity distribution.
- analyze_workout — call before judging any specific run or ride (by activity_id, date, or latest). It shows every rep, where the athlete went too hard or faded, stops and surges. Never judge a workout from its averages alone.
- sync_garmin_data — when the athlete asks to sync, mentions a new workout, or data looks stale.
- get_athlete_docs — for race strategy, fueling, gear, injury history and long-term plans.
"""


def _instructions() -> str:
    tone = load_config().coach.tone
    return coaching_brief(tone) + "\n" + WORKFLOW


def _register_profile_tools(mcp: FastMCP) -> FastMCP:
    @mcp.tool()
    def get_profile() -> str:
        """Get the current athlete profile, thresholds and coaching style to check if onboarding is complete."""
        cfg = load_config()
        a, t, c = cfg.athlete, cfg.thresholds, cfg.coach
        fields = {
            "sport": a.sport or "NOT SET",
            "race_distance": a.race_distance or "NOT SET",
            "race_name": a.race_name or "NOT SET",
            "race_date": a.race_date or "NOT SET",
            "experience": a.experience or "NOT SET",
            "max_weekly_hours": str(a.max_weekly_hours) if a.max_weekly_hours else "NOT SET",
            "goals": a.goals or "NOT SET",
            "injury_history": ", ".join(a.injury_history) if a.injury_history else "none",
            "ftp_watts": t.ftp_watts or "not set (Garmin value used)",
            "lthr_run": t.lthr_run or "not set (Garmin value used)",
            "lthr_bike": t.lthr_bike or "not set (uses lthr_run)",
            "easy_hr_cap": t.easy_hr_cap or "not set",
            "coach_tone": c.tone,
            "units": c.units,
        }
        if cfg.cycle.eligible:
            fields["cycle_tracking"] = "available (not asked yet)" if cfg.cycle.tracking == "unasked" else cfg.cycle.tracking
        return "\n".join(f"{k}: {v}" for k, v in fields.items())

    @mcp.tool()
    def save_profile(
        sport: str = "",
        race_distance: str = "",
        race_name: str = "",
        race_date: str = "",
        experience: str = "",
        max_weekly_hours: float = 0.0,
        goals: str = "",
        injury_history: str = "",
        ftp_watts: int = 0,
        lthr_run: int = 0,
        lthr_bike: int = 0,
        easy_hr_cap: int = 0,
        coach_tone: str = "",
        units: str = "",
        cycle_tracking: str = "",
    ) -> str:
        """Save athlete profile answers. Only pass fields you want to update.

        coach_tone: direct (default), supportive or tough. units: metric or imperial.
        cycle_tracking: on or off — only when get_profile lists it; off deletes the locally stored data.
        Thresholds (ftp_watts, lthr_run, lthr_bike, easy_hr_cap) are used to judge workouts — update them after every test.
        """
        cfg = load_config()
        a, t, c = cfg.athlete, cfg.thresholds, cfg.coach
        if sport:
            a.sport = sport
        if race_distance:
            a.race_distance = race_distance
        if race_name:
            a.race_name = race_name
        if race_date:
            a.race_date = race_date
        if experience:
            a.experience = experience
        if max_weekly_hours:
            a.max_weekly_hours = max_weekly_hours
        if goals:
            a.goals = goals
        if injury_history:
            a.injury_history = [i.strip() for i in injury_history.split(",") if i.strip()]
        if ftp_watts:
            t.ftp_watts = ftp_watts
        if lthr_run:
            t.lthr_run = lthr_run
        if lthr_bike:
            t.lthr_bike = lthr_bike
        if easy_hr_cap:
            t.easy_hr_cap = easy_hr_cap
        if coach_tone in ("direct", "supportive", "tough"):
            c.tone = coach_tone
        if units in ("metric", "imperial"):
            c.units = units
        if cycle_tracking in ("on", "off") and cfg.cycle.eligible:
            cfg.cycle.tracking = cycle_tracking
            if cycle_tracking == "off":
                from coach import cycle
                from coach.database import Database
                db = Database()
                cycle.purge(db.conn)
                db.close()
        save_config(cfg)
        return "Profile saved."

    return mcp


def main():
    mcp = FastMCP("Coach", instructions=_instructions())
    mcp = context.register_tools(mcp)
    mcp = garmin_client.register_tools(mcp)
    mcp = _register_profile_tools(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
