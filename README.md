# Personal AI Ironman Coach

Sync your Garmin Connect data locally and get evidence-based Ironman coaching from any AI — Cursor, ChatGPT, Claude, or anything that reads markdown.

## How It Works

```
Garmin Connect ──▶ coach sync ──▶ SQLite DB ──▶ coach context ──▶ training_context.md
                    (API)          (local)        (query+build)         │
                                                                       ▼
                                                              Cursor AI Chat
                                                          (@training_context.md
                                                            + @athlete_docs)
```

1. **`coach sync`** pulls your training activities and health metrics (HRV, sleep, stress, body battery, training readiness) from Garmin Connect into a local SQLite database, then analyzes the laps and second-by-second data of your recent runs and rides.
2. **`coach context`** generates a compact `training_context.md`:
   - **Readiness, day by day** — HRV, resting HR, sleep and readiness judged against *your own* 30-day baseline (GREEN / AMBER / RED), not hidden in weekly averages.
   - **Fitness / Fatigue / Form** — CTL, ATL, TSB and ramp rate from TSS (HR-based estimate where there's no power).
   - **Session breakdowns** — every rep of last week's key sessions with power/pace, %FTP and HR, plus flags: first rep too hard, fading, HR creep, stops inside a rep, surges on "easy" rides, easy runs drifting above your HR cap, aerobic decoupling.
   - Recent activities, weekly volume, 4-week intensity distribution (too much Z3?), fatigue flags.
   - A **Coaching Brief** so any AI reading the file coaches with the same personality and method.
3. **In Cursor**, reference `@training_context.md` (and optionally `@athlete_docs`) in chat — the Cursor rule activates automatically and makes the AI respond as an evidence-based Ironman coach grounded in your actual data.

## Prerequisites

- **Python 3.11+** — check with `python3 --version`. Install via [python.org](https://www.python.org/downloads/) or `brew install python` on macOS.
- **Garmin Connect account** — with a Garmin device syncing your training data.
- **Any AI chat** — Cursor (has auto-activating coaching rule), ChatGPT, Claude, etc.

## Setup

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/garmin-coach-context.git
cd garmin-coach-context

# Create virtual environment and install
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Log in to Garmin Connect (credentials stored in system keychain, 2FA supported)
coach login

# Configure athlete profile, thresholds (FTP, threshold HR, easy HR cap), coach tone and units
coach config
```

### Athlete Context (optional but recommended)

Create an `athlete_docs/` directory with markdown files describing your profile, race targets, fueling strategy, injury history, etc. The more context you provide, the better the coaching. See `config.example.yaml` for the config format.

## Daily Workflow

```bash
# Activate the virtual environment
source .venv/bin/activate

# Sync latest data from Garmin (do this after each training session)
coach sync

# Generate context file for Cursor AI
coach context

# Quick status check in terminal
coach status

# Break down your latest run/ride (or: coach analyze <activity_id>, coach analyze --date 2026-10-20)
coach analyze
```

Then ask your AI for coaching:

**Cursor** (recommended) — the `.cursor/rules/ironman-coach.mdc` rule auto-activates when you reference the files:
- `@training_context.md How should I adjust this week based on my fatigue?`
- `@training_context.md @athlete_docs Review my last week — am I on track?`

**ChatGPT / Claude / any AI** — paste the contents of `training_context.md` (and optionally your `athlete_docs/`) into the chat, then ask your questions. The context file is compact (~2-5k tokens) so it's cheap to include.

## Commands

| Command | Description |
|---------|-------------|
| `coach login` | Authenticate with Garmin Connect (saves to system keychain) |
| `coach sync` | Pull latest activities and health data from Garmin |
| `coach context` | Generate `training_context.md` for AI (use --live for live API data) |
| `coach analyze` | Rep-by-rep breakdown of a run/ride: pacing, where you went too hard, stops, drift |
| `coach summary` | Print weekly training summary table |
| `coach status` | Show training load, health snapshot, recent activities |
| `coach config` | View/edit athlete profile (race date, goals, etc.) |
| `coach reset` | Reset local database |

### Options

```bash
coach sync --lookback 120       # Re-fetch more history (default: only what changed since the last sync; 90 days on first sync)
coach sync --health-days 30     # Re-fetch more health data (default: since last sync; 45 days on first sync)
coach summary --weeks 12        # Show more weeks
coach context --output ctx.md   # Custom output path
coach context --live            # Generate from live API data instead of database
```

## What Data Is Synced

All data stays on your machine:

- **Activities**: sport type, duration, distance, HR zones, pace, power, TSS, training effect, elevation, cadence
- **Health metrics**: resting HR, HRV, sleep score/duration, body battery, stress, training readiness, VO2 max, weight
- **Cycle phase (optional, opt-in)**: offered only when your Garmin profile is female and you track your cycle in Garmin Connect. When on, readiness allows for expected shifts in resting HR and HRV across the cycle and the coach gets phase-aware context (including a RED-S warning if Garmin flags missing periods). Stored only in the local database, deleted when you turn it off, and kept out of `training_context.md` unless you set `cycle.share_in_context_file: true`.
- **Workout analysis** (runs & rides, last 14 days by default): laps, reps, stops, surges, decoupling — cached in the database; raw streams are not stored

### Where Data Lives

| Location | What |
|----------|------|
| `~/.config/personal-ai-coach/data/coach.db` | SQLite database (activities, health metrics, sync log) |
| `~/.config/personal-ai-coach/garmin_session/` | Garmin auth tokens (avoids re-login) |
| `~/.config/personal-ai-coach/config.yaml` | Athlete profile (race date, goals — no secrets) |
| macOS Keychain | Garmin email + password (service: `personal-ai-coach`) |
| `training_context.md` (project root) | Generated context for Cursor AI (gitignored) |
| `athlete_docs/` (project root) | Your personal coaching docs (gitignored) |

You can query the database directly:

```bash
sqlite3 ~/.config/personal-ai-coach/data/coach.db \
  "SELECT start_time, sport_type, duration_seconds/60 as mins, distance_meters/1000 as km, avg_hr FROM activities ORDER BY start_time DESC LIMIT 5;"
```

## Coach Personality

The coach has one consistent personality everywhere (MCP instructions and the "Coaching Brief" at the end of `training_context.md`), defined in `src/coach/persona.py`:

- **Verdict first, numbers always** — every judgement cites the number behind it.
- **Execution over averages** — reviews look at reps, fades, surges and stops before giving a verdict.
- **Patterns over single data points**, injury/illness flags outrank the plan, no flip-flopping, admits uncertainty.
- Fixed formats for **workout reviews** (verdict → what went well → where it went off → why it matters → next time) and **daily check-ins**.

Pick the tone in `coach config` (or ask the AI to change it via `save_profile`):

| Tone | Style |
|------|-------|
| `direct` (default) | Short, plain, specific. Warm, not soft. |
| `supportive` | Same honesty, gentler delivery; acknowledges effort and life load first. |
| `tough` | Blunt and demanding — never shaming, never unsafe. |

## Security

- Garmin credentials are stored in your OS credential store (macOS Keychain / Linux SecretService / Windows Credential Locker), not in any file
- 2FA/MFA is supported — you'll be prompted in the terminal when needed
- No data is sent anywhere except to Garmin Connect (for sync) and your AI tool (when you chat)
- `athlete_docs/`, `training_context.md`, and `config.yaml` are gitignored — your personal health data never hits the repo

## Development

```bash
python -m unittest discover -s tests    # no extra dependencies needed
```

## Platform Support

Tested on macOS. Should work on Linux and Windows since all dependencies are cross-platform:
- `keyring` uses the native OS credential store on each platform
- `sqlite3` is built into Python
- `garminconnect` is pure Python

## Model Context Protocol (MCP) Server

This project supports the Model Context Protocol (MCP) for seamless context injection into AI tools like Claude Desktop.

### MCP Setup - Local

1. **Install [uv](https://github.com/astral-sh/uv) (fast Python package manager):**

2. **Sync the project:**

   ```bash
   uv sync
   ```

3. **Start the MCP server:**

   ```bash
   uv run coach-mcp
   ```

### MCP Tools

| Tool | What it does |
|------|--------------|
| `get_coach_context` | Readiness vs baseline, fitness/fatigue/form, session breakdowns, recent activities, intensity distribution |
| `analyze_workout` | Full rep-by-rep breakdown of one run/ride (by id, date, or latest) |
| `sync_garmin_data` | Pull new data from Garmin Connect (at most once per hour) and analyze new workouts |
| `get_athlete_docs` | Return everything in `athlete_docs/` (race targets, fueling, injuries) |
| `get_profile` / `save_profile` | Read / update the athlete profile; used for first-run onboarding |

### Claude Desktop Integration

Add to your Claude Desktop MCP settings:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```JSON
{
  "mcpServers": {
    "garmin-coach-context": {
      "command": "uv",
      "args": [
        "--directory",
        "<full path to garmin-coach-context local repo>",
        "run",
        "coach-mcp"
      ]
    }
  }
}
```

> **No `uv` installed?** Apps don't see your shell's PATH, so a bare `uv` command fails with `ENOENT`. Point at the virtualenv binary with an absolute path instead (see `.mcp.json.example` for Claude Code):
>
> ```JSON
> { "mcpServers": { "garmin-coach-context": { "command": "<full path to repo>/.venv/bin/coach-mcp", "args": [], "cwd": "<full path to repo>" } } }
> ```
>
> Keep `cwd` — `get_athlete_docs` looks for `athlete_docs/` relative to the working directory.

To use the MCP server with Claude Desktop:

1. Open Claude Desktop and go to **Settings → Connectors → garmin-coach-context (Configure)**.
2. Select "Always Allow" for the tools you want enabled

3. Save settings. Claude will now automatically generate your training context when it needs it. You can also request that it syncs your data with Garmin Connect. It's limited to syncing with Garmin Connect once every hour.

### Test MCP for Development with Inspector
The MCP inspector lets you inspect and test tools for development

Prerequisites: Node.js

1. Run the MCP inspector directly with npx

```bash
npx @modelcontextprotocol/inspector uv run coach-mcp
```


## Project Structure

```
garmin-coach-context/
  src/
    coach/
      __init__.py           # Package marker
      cli.py                # CLI entry point (Typer)
      garmin_client.py      # Garmin Connect sync engine
      database.py           # SQLite storage layer
      context.py            # Generates training_context.md
      analysis.py           # Per-workout analysis: reps, pacing, surges, stops, decoupling
      persona.py            # Coach personality, method and response formats
      mcp.py                # MCP server implementation
      config.py             # Configuration management
      models.py             # Pydantic data models
      utils.py              # Utility functions
    personal_ai_coach.egg-info/ # Packaging metadata
  athlete_docs/             # Your personal coaching context (gitignored)
  .cursor/rules/            # Cursor AI coaching rule (for Cursor editor)
  .cursor/mcp.json          # Cursor MCP config (relative paths)
  .mcp.json.example         # Claude Code MCP config template (copy to .mcp.json, gitignored)
  training_context.md       # Auto-generated context for AI (gitignored)
  config.example.yaml       # Example configuration
  tests/                    # Unit tests (python -m unittest discover -s tests)
  pyproject.toml            # Python project metadata
  README.md                 # Project documentation
```
