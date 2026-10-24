from mcp.server.fastmcp import FastMCP
from coach import context, garmin_client
from coach.config import load_config, save_config

INSTRUCTIONS = """
You are a personal endurance sports coach with access to the athlete's real Garmin Connect training data.

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

Then call save_profile with their answers before proceeding.

## On every coaching question

Always call get_coach_context first to load current training data (activities, health metrics, load analysis).

If the user asks to sync, mentions stale data, or it's been a while since last sync, call sync_garmin_data first.

For deep questions about race strategy, fueling, or gear, call get_athlete_docs to load the full athlete documentation.

## Coaching style

- Evidence-based and specific — use actual numbers from the data (watts, pace, HR, TSS)
- Quantify recommendations (e.g. "keep this run under 145 bpm", not "run easy")
- Flag overtraining and injury risk proactively based on the data
- Adapt to the athlete's specific sport and race distance
- For Ironman/70.3: emphasize pacing, fueling, and durability over raw speed
- For running: adapt advice to race distance and training phase
- Conservative on injury risk — when in doubt, recommend the safer option
- Be direct — give a clear answer, then explain the reasoning
"""


def _register_profile_tools(mcp: FastMCP) -> FastMCP:
    @mcp.tool()
    def get_profile() -> str:
        """Get the current athlete profile to check if onboarding is complete."""
        cfg = load_config()
        a = cfg.athlete
        fields = {
            "sport": a.sport or "NOT SET",
            "race_distance": a.race_distance or "NOT SET",
            "race_name": a.race_name or "NOT SET",
            "race_date": a.race_date or "NOT SET",
            "experience": a.experience or "NOT SET",
            "max_weekly_hours": str(a.max_weekly_hours) if a.max_weekly_hours else "NOT SET",
            "goals": a.goals or "NOT SET",
            "injury_history": ", ".join(a.injury_history) if a.injury_history else "none",
        }
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
    ) -> str:
        """Save athlete profile answers. Only pass fields you want to update."""
        cfg = load_config()
        a = cfg.athlete
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
        save_config(cfg)
        return "Profile saved."

    return mcp


def main():
    mcp = FastMCP("Coach", instructions=INSTRUCTIONS)
    mcp = context.register_tools(mcp)
    mcp = garmin_client.register_tools(mcp)
    mcp = _register_profile_tools(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
