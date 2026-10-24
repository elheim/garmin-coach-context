from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from mcp.server.fastmcp import FastMCP

from .config import (
    CONFIG_PATH,
    CONTEXT_FILE,
    has_garmin_credentials,
    load_config,
    save_config,
    save_garmin_credentials,
)
from .database import Database

app = typer.Typer(
    name="coach",
    help="Personal AI endurance coach — sync Garmin data, analyze workouts and generate context for AI coaching.",
    no_args_is_help=True,
)
console = Console()

@app.command()
def login(
    email: str = typer.Option(None, prompt="Garmin Connect email"),
    password: str = typer.Option(None, prompt="Garmin Connect password", hide_input=True),
) -> None:
    """Authenticate with Garmin Connect and save credentials to system keychain."""
    from .config import delete_garmin_credentials
    from .garmin_client import _get_client

    save_garmin_credentials(email, password)

    try:
        rprint("Authenticating with Garmin Connect...")
        rprint("[dim]If you have 2FA enabled, you'll be prompted for the code.[/dim]")
        _get_client()
        rprint("[bold green]Logged in successfully.[/bold green]")
        rprint("Credentials stored in system keychain (macOS Keychain / OS credential store).")
    except Exception as e:
        delete_garmin_credentials()  # don't keep credentials that don't work
        rprint(f"[bold red]Login failed:[/bold red] {e}")
        raise typer.Exit(1)


@app.command()
def sync(
    lookback: int = typer.Option(None, "--lookback", "-l", help="Days of activities to fetch (default: since last sync; full history from config on first sync)"),
    health_days: int = typer.Option(None, "--health-days", help="Days of health data to fetch (default: since last sync; 45 on first sync for baselines)"),
) -> None:
    """Sync training data from Garmin Connect."""
    from .garmin_client import analyze_recent, incremental_lookback, sync_activities, sync_cycle, sync_health

    cfg = load_config()
    db = Database()
    days = lookback or incremental_lookback(db, cfg.sync.lookback_days)
    health_days = health_days or incremental_lookback(db, 45, "health")

    try:
        rprint(f"\n[bold]Syncing activities ({days} days)...[/bold]")
        act_count = sync_activities(db, lookback_days=days)
        rprint(f"  [green]{act_count}[/green] activities synced.")

        rprint(f"\n[bold]Syncing health metrics ({health_days} days)...[/bold]")
        health_count = sync_health(db, lookback_days=health_days)
        rprint(f"  [green]{health_count}[/green] days of health data synced.")
        try:
            sync_cycle(db, lookback_days=health_days)
        except Exception:
            pass

        rprint(f"\n[bold]Analyzing runs & rides ({cfg.sync.analyze_days} days)...[/bold]")
        analyzed = analyze_recent(db, days=cfg.sync.analyze_days)
        rprint(f"  [green]{analyzed}[/green] new workouts analyzed (laps, reps, pacing).")

        rprint(f"\n[bold]Database totals:[/bold] {db.activity_count()} activities, {db.health_count()} health records")
        db.close()

        rprint("\n[dim]Run [bold]coach context[/bold] to generate the training context file.[/dim]")
    except Exception as e:
        rprint(f"\n[bold red]Sync failed:[/bold red] {e}")
        rprint("[dim]Try [bold]coach login[/bold] to re-authenticate.[/dim]")
        raise typer.Exit(1)

@app.command()
def context(
    output: Path = typer.Option(None, "--output", "-o", help="Output path (default: training_context.md)"),
    live: bool = typer.Option(False, "--live", help="Fetch from Garmin API instead of local database"),
) -> None:
    """Generate the training context markdown file for Cursor AI."""
    from .context import build_context

    try:
        result = build_context(output_path=output, use_db=not live)
        rprint(f"\n[bold green]Context file generated:[/bold green] {result}")
        rprint(f"\n[dim]Reference it in Cursor chat with [bold]@{result.name}[/bold] to get AI coaching.[/dim]")
    except Exception as e:
        rprint(f"[bold red]Failed to generate context:[/bold red] {e}")
        raise typer.Exit(1)


@app.command()
def analyze(
    activity_id: int = typer.Argument(None, help="Garmin activity id (default: latest run/ride)"),
    day: str = typer.Option(None, "--date", "-d", help="Analyze every run/ride on this date (YYYY-MM-DD)"),
    force: bool = typer.Option(False, "--force", help="Re-fetch from Garmin instead of using the cache"),
) -> None:
    """Break down a run or ride: reps, pacing, where you went too hard, stops, drift."""
    from rich.markdown import Markdown

    from .analysis import classify, render
    from .garmin_client import analyze_activity

    db = Database()
    try:
        if activity_id:
            a = db.get_activity(activity_id)
            targets = [a] if a else []
        elif day:
            targets = db.find_activities_on(day)
        else:
            targets = [x for x in db.get_recent_activities(limit=20)
                       if classify(x.sport_type or x.activity_type) in ("run", "bike")][:1]
        shown = 0
        for a in targets:
            result = analyze_activity(db, a, force=force)
            if result:
                console.print(Markdown(render(result)))
                console.print()
                shown += 1
        if not shown:
            rprint("[yellow]No run or ride found. Run [bold]coach sync[/bold] first or check the id/date.[/yellow]")
    finally:
        db.close()


@app.command()
def summary(
    weeks: int = typer.Option(8, "--weeks", "-w", help="Number of weeks to show"),
) -> None:
    """Show weekly training summary table."""
    db = Database()
    week_data = db.get_weekly_summaries(weeks=weeks)
    db.close()

    if not week_data:
        rprint("[yellow]No training data yet. Run [bold]coach sync[/bold] first.[/yellow]")
        raise typer.Exit(0)

    table = Table(title=f"Weekly Training Summary (last {weeks} weeks)", show_lines=True)
    table.add_column("Week", style="bold")
    table.add_column("Swim", justify="right")
    table.add_column("Bike", justify="right")
    table.add_column("Run", justify="right")
    table.add_column("Strength", justify="center")
    table.add_column("Total Hrs", justify="right", style="bold cyan")
    table.add_column("TSS", justify="right")

    for w in week_data:
        swim = f"{w.swim_hours:.1f}h / {w.swim_km:.1f}km ({w.swim_sessions}x)" if w.swim_sessions else "-"
        bike = f"{w.bike_hours:.1f}h / {w.bike_km:.0f}km ({w.bike_sessions}x)" if w.bike_sessions else "-"
        run = f"{w.run_hours:.1f}h / {w.run_km:.1f}km ({w.run_sessions}x)" if w.run_sessions else "-"
        strength = f"{w.strength_sessions}x" if w.strength_sessions else "-"
        tss = f"{w.total_tss:.0f}" if w.total_tss else "-"
        table.add_row(str(w.week_start), swim, bike, run, strength, f"{w.total_hours:.1f}", tss)

    console.print(table)


@app.command()
def status() -> None:
    """Show current training status, load, and health indicators."""
    cfg = load_config()
    db = Database()
    weeks = db.get_weekly_summaries(weeks=8)
    health = db.get_health_metrics(since_days=60)
    activities = db.get_recent_activities(limit=5)
    history = db.get_activities(since_days=120)
    db.close()

    # Race countdown
    if cfg.athlete.race_date:
        try:
            rd = date.fromisoformat(cfg.athlete.race_date)
            days_out = (rd - date.today()).days
            rprint(Panel(
                f"[bold]{cfg.athlete.race_name or 'Race'}[/bold]\n"
                f"{cfg.athlete.race_date} — [bold cyan]{days_out} days[/bold cyan] to go",
                title="Race Countdown",
            ))
        except ValueError:
            pass

    # Readiness today (vs personal baseline) and fitness/fatigue/form
    from .analysis import Thresholds
    from .context import build_fitness_form, build_readiness

    if health:
        line = build_readiness(health).splitlines()[0].replace("**", "")
        color = "green" if ": GREEN" in line else "yellow" if ": AMBER" in line else "red"
        rprint(f"\n[bold]Readiness[/bold]\n  [{color}]{line}[/{color}]")
    if history:
        t = cfg.thresholds
        th = Thresholds(ftp_watts=t.ftp_watts, lthr_bike=t.lthr_bike or t.lthr_run, lthr_run=t.lthr_run)
        rprint("\n[bold]Fitness / Fatigue / Form[/bold]")
        for line in build_fitness_form(history, th).splitlines()[:2]:
            rprint("  " + line.lstrip("- ").replace("**", ""))

    # Training load
    if weeks:
        rprint("\n[bold]Volume[/bold]")
        if weeks:
            acute = weeks[0].total_hours
            chronic_hours = [w.total_hours for w in weeks[:6]]
            chronic = sum(chronic_hours) / max(len(chronic_hours), 1)
            ratio = acute / chronic if chronic > 0 else 0

            color = "green"
            if ratio > 1.3:
                color = "red"
            elif ratio < 0.8:
                color = "yellow"

            rprint(f"  This week: [bold]{acute:.1f}[/bold] hours")
            rprint(f"  6-week avg: [bold]{chronic:.1f}[/bold] hours/week")
            rprint(f"  Acute:Chronic ratio: [bold {color}]{ratio:.2f}[/bold {color}]")

    # Health snapshot
    if health:
        latest = health[0]
        rprint("\n[bold]Health Snapshot (latest)[/bold]")
        if latest.resting_hr:
            rprint(f"  Resting HR: {latest.resting_hr} bpm")
        if latest.hrv_last_night:
            status_str = f" ({latest.hrv_status})" if latest.hrv_status else ""
            rprint(f"  HRV: {latest.hrv_last_night:.0f} ms{status_str}")
        if latest.sleep_score:
            rprint(f"  Sleep score: {latest.sleep_score}/100")
        if latest.body_battery_high is not None:
            rprint(f"  Body battery: {latest.body_battery_low}-{latest.body_battery_high}")
        if latest.training_readiness:
            rprint(f"  Training readiness: {latest.training_readiness}")
        if latest.stress_avg:
            rprint(f"  Stress avg: {latest.stress_avg}")

    # Recent activities
    if activities:
        rprint("\n[bold]Recent Activities[/bold]")
        for a in activities[:5]:
            date_str = a.start_time.strftime("%m/%d") if a.start_time else "?"
            sport = a.sport_type or "?"
            dur_h = int(a.duration_seconds // 3600)
            dur_m = int((a.duration_seconds % 3600) // 60)
            dur = f"{dur_h}h{dur_m:02d}m" if dur_h else f"{dur_m}m"
            km = a.distance_meters / 1000
            hr_str = f"  HR {a.avg_hr:.0f}/{a.max_hr:.0f}" if a.avg_hr else ""
            rprint(f"  {date_str}  {sport:<12} {dur:>7}  {km:>6.1f}km{hr_str}")

    if not weeks and not health and not activities:
        rprint("[yellow]No data yet. Run [bold]coach sync[/bold] first.[/yellow]")


@app.command()
def config() -> None:
    """Show or edit athlete configuration."""
    cfg = load_config()

    rprint(Panel(
        f"[bold]Sport:[/bold] {cfg.athlete.sport or 'Not set'}\n"
        f"[bold]Race distance:[/bold] {cfg.athlete.race_distance or 'Not set'}\n"
        f"[bold]Race:[/bold] {cfg.athlete.race_name or 'Not set'}\n"
        f"[bold]Date:[/bold] {cfg.athlete.race_date or 'Not set'}\n"
        f"[bold]Experience:[/bold] {cfg.athlete.experience or 'Not set'}\n"
        f"[bold]Max weekly hours:[/bold] {cfg.athlete.max_weekly_hours or 'Not set'}\n"
        f"[bold]Goals:[/bold] {cfg.athlete.goals or 'Not set'}\n"
        f"[bold]Injuries:[/bold] {', '.join(cfg.athlete.injury_history) if cfg.athlete.injury_history else 'None'}\n"
        f"[bold]FTP / LTHR run / LTHR bike / easy HR cap:[/bold] "
        f"{cfg.thresholds.ftp_watts or '-'} W / {cfg.thresholds.lthr_run or '-'} / {cfg.thresholds.lthr_bike or '-'} / {cfg.thresholds.easy_hr_cap or '-'}\n"
        f"[bold]Coach tone / units:[/bold] {cfg.coach.tone} / {cfg.coach.units}\n"
        f"\n[dim]Config file: {CONFIG_PATH}[/dim]",
        title="Athlete Configuration",
    ))

    if typer.confirm("\nEdit configuration?", default=False):
        cfg.athlete.sport = typer.prompt("Sport (running/cycling/triathlon)", default=cfg.athlete.sport)
        cfg.athlete.race_distance = typer.prompt("Race distance (e.g. ironman, 70.3, marathon)", default=cfg.athlete.race_distance)
        cfg.athlete.race_name = typer.prompt("Race name", default=cfg.athlete.race_name)
        cfg.athlete.race_date = typer.prompt("Race date (YYYY-MM-DD)", default=cfg.athlete.race_date)
        cfg.athlete.experience = typer.prompt("Experience level", default=cfg.athlete.experience)
        cfg.athlete.max_weekly_hours = float(typer.prompt("Max weekly hours", default=str(cfg.athlete.max_weekly_hours)))
        cfg.athlete.goals = typer.prompt("Goals", default=cfg.athlete.goals)

        def _opt_int(label: str, current: int | None) -> int | None:
            v = typer.prompt(f"{label} (blank = use Garmin/none)", default=str(current or ""), show_default=bool(current))
            return int(v) if v.strip().isdigit() else None

        cfg.thresholds.ftp_watts = _opt_int("FTP (W)", cfg.thresholds.ftp_watts)
        cfg.thresholds.lthr_run = _opt_int("Lactate threshold HR, run", cfg.thresholds.lthr_run)
        cfg.thresholds.lthr_bike = _opt_int("Lactate threshold HR, bike", cfg.thresholds.lthr_bike)
        cfg.thresholds.easy_hr_cap = _opt_int("Easy HR cap", cfg.thresholds.easy_hr_cap)
        cfg.coach.tone = typer.prompt("Coach tone (direct/supportive/tough)", default=cfg.coach.tone)
        cfg.coach.units = typer.prompt("Units (metric/imperial)", default=cfg.coach.units)
        if cfg.cycle.eligible:
            on = typer.confirm("Use your Garmin cycle tracking to personalize readiness and coaching? "
                               "(stays on this computer)", default=cfg.cycle.tracking == "on")
            cfg.cycle.tracking = "on" if on else "off"
            if not on:
                from . import cycle
                db = Database()
                cycle.purge(db.conn)
                db.close()
        save_config(cfg)
        rprint("[bold green]Configuration saved.[/bold green]")


@app.command()
def reset() -> None:
    """Reset the local database (keeps config)."""
    from .config import DB_PATH

    if DB_PATH.exists():
        if typer.confirm(f"Delete database at {DB_PATH}?", default=False):
            DB_PATH.unlink()
            rprint("[green]Database reset.[/green]")
    else:
        rprint("[yellow]No database found.[/yellow]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
