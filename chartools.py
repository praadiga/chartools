"""chartools — CloudPort characterization tool CLI."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="chartools",
    help="Characterization tool for benchmarking CloudPort playout players.",
    no_args_is_help=True,
)

daemon_app = typer.Typer(help="Manage the characterization daemon.")
app.add_typer(daemon_app, name="daemon")

testsuite_app = typer.Typer(help="Manage testsuites.")
app.add_typer(testsuite_app, name="testsuite")

console = Console()


def _fmt_since(since_iso: Optional[str]) -> str:
    """Format 'time in current status' as e.g. '5m', '2h30m'."""
    if not since_iso:
        return "-"
    from datetime import datetime, timezone
    try:
        since = datetime.fromisoformat(since_iso)
        delta = int((datetime.now(timezone.utc) - since).total_seconds())
        if delta < 60:
            return f"{delta}s"
        elif delta < 3600:
            return f"{delta // 60}m"
        else:
            h, m = divmod(delta // 60, 60)
            return f"{h}h{m}m"
    except Exception:
        return "-"


# ---------------------------------------------------------------------------
# daemon subcommands
# ---------------------------------------------------------------------------

@daemon_app.command("start")
def daemon_start():
    """Start the daemon in the foreground (run inside screen/tmux)."""
    from daemon.runner import Daemon, is_running
    if is_running():
        console.print("[yellow]Daemon is already running.[/yellow]")
        raise typer.Exit(1)
    Daemon().start()


@daemon_app.command("stop")
def daemon_stop():
    """Gracefully stop the daemon."""
    from daemon.runner import stop_daemon
    stop_daemon()


@daemon_app.command("status")
def daemon_status():
    """Check whether the daemon is running."""
    from daemon.runner import is_running, PID_PATH
    if is_running():
        pid = PID_PATH.read_text().strip()
        console.print(f"[green]Daemon is running[/green] (PID {pid})")
    else:
        console.print("[red]Daemon is not running.[/red]")


# ---------------------------------------------------------------------------
# testsuite subcommands
# ---------------------------------------------------------------------------

@testsuite_app.command("add")
def testsuite_add(
    directory: Path = typer.Argument(..., help="Path to testsuite folder containing config.yaml"),
):
    """Register a testsuite with the daemon and start deployment."""
    from daemon.runner import send_add
    ts_dir = directory.resolve()
    try:
        reply = send_add(ts_dir)
    except ConnectionRefusedError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if reply.get("ok"):
        console.print(f"[green]Testsuite '{ts_dir.name}' queued — watch: chartools status {ts_dir}[/green]")
    else:
        console.print(f"[red]Error: {reply.get('error')}[/red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# top-level commands
# ---------------------------------------------------------------------------

@app.command("status")
def status(
    directory: Optional[Path] = typer.Argument(
        None, help="Testsuite folder for detailed view. Omit to list all."
    ),
):
    """Show status of all testsuites, or detailed view for one."""
    from core.status import read_status, scan_all_testsuites

    if directory is None:
        # global view
        dirs = scan_all_testsuites()
        if not dirs:
            from core.status import CHARACTERIZATION_BASE
            console.print(f"No testsuites found under {CHARACTERIZATION_BASE}")
            return

        tbl = Table(show_header=True, header_style="bold")
        tbl.add_column("TESTSUITE")
        tbl.add_column("STATUS")
        tbl.add_column("CREATED")
        tbl.add_column("TESTCASES", justify="right")
        tbl.add_column("RUNNING",  justify="right")
        tbl.add_column("SUCCESS",  justify="right")
        tbl.add_column("FAILED",   justify="right")

        for d in dirs:
            try:
                s = read_status(d)
            except Exception:
                continue
            running = sum(1 for r in s.runs if r.status in ("RUNNING", "PROVISIONING", "DEPLOYING"))
            success = sum(1 for r in s.runs if r.status == "SUCCESS")
            failed  = sum(1 for r in s.runs if r.status == "FAILURE")
            tbl.add_row(s.testsuite_id, s.status, s.created_at[:10],
                        str(len(s.runs)), str(running), str(success), str(failed))
        console.print(tbl)
    else:
        # detailed view
        ts_dir = directory.resolve()
        try:
            s = read_status(ts_dir)
        except FileNotFoundError:
            console.print(f"[red]No status.yaml found in {ts_dir}[/red]")
            raise typer.Exit(1)

        console.print(f"\nTESTSUITE: [bold]{s.testsuite_id}[/bold]  "
                      f"STATUS: {s.status}  CP_RELEASE: {s.cp_release or 'unknown'}\n")

        tbl = Table(show_header=True, header_style="bold")
        tbl.add_column("TESTCASE")
        tbl.add_column("PLAYER")
        tbl.add_column("STATUS")
        tbl.add_column("SINCE")
        tbl.add_column("SAMPLES",  justify="right")
        tbl.add_column("CRASHES",  justify="right")
        tbl.add_column("TERMINATES")
        tbl.add_column("ERROR")

        for r in s.runs:
            terminates = r.terminates_at[:10] if r.terminates_at else "-"
            error_col = r.error_msg or ""
            if r.pod_status and r.status != "RUNNING":
                error_col = f"[pod:{r.pod_status}] {error_col}".strip()
            tbl.add_row(
                r.testcase, r.player_name, r.status,
                _fmt_since(r.status_since),
                str(r.samples_collected), str(r.crash_events),
                terminates, error_col,
            )
        console.print(tbl)


@app.command("report")
def report(
    directory: Path = typer.Argument(..., help="Testsuite folder"),
):
    """Print the summary report for a completed testsuite."""
    import yaml
    ts_dir = directory.resolve()
    summary_path = ts_dir / "report" / "summary.yaml"
    if not summary_path.exists():
        console.print(f"[red]No summary.yaml found at {summary_path}[/red]")
        raise typer.Exit(1)

    with open(summary_path) as f:
        data = yaml.safe_load(f)

    console.print(f"\nTESTSUITE: [bold]{data.get('testsuite')}[/bold]  "
                  f"NODETAINT: {data.get('nodetaint')}  "
                  f"CP_RELEASE: {data.get('cp_release') or 'unknown'}\n")

    tbl = Table(show_header=True, header_style="bold")
    tbl.add_column("TESTCASE")
    tbl.add_column("PLAYER")
    tbl.add_column("CRASHES", justify="right")
    tbl.add_column("CPU_AVG%", justify="right")
    tbl.add_column("CPU_P99%", justify="right")
    tbl.add_column("MEM_AVG_MB", justify="right")
    tbl.add_column("MEM_P99_MB", justify="right")

    for r in data.get("results", []):
        tbl.add_row(
            r.get("testcase", ""),
            r.get("player_name", ""),
            str(r.get("crash_events", 0)),
            f"{r.get('player1_cpu_avg', 0):.1f}",
            f"{r.get('player1_cpu_p99', 0):.1f}",
            f"{r.get('player1_mem_avg_kb', 0) / 1024:.0f}",
            f"{r.get('player1_mem_p99_kb', 0) / 1024:.0f}",
        )
    console.print(tbl)


@app.command("terminate")
def terminate(
    directory: Path = typer.Argument(..., help="Testsuite folder"),
):
    """Force-terminate all running runs in a testsuite."""
    from core.status import RunStatus, read_status, update_run, update_testsuite_status
    from daemon.runner import is_running

    ts_dir = directory.resolve()
    try:
        state = read_status(ts_dir)
    except FileNotFoundError:
        console.print(f"[red]No status.yaml in {ts_dir}[/red]")
        raise typer.Exit(1)

    running = [r for r in state.runs if r.status == RunStatus.RUNNING]
    if not running:
        console.print("No RUNNING runs to terminate.")
        return

    typer.confirm(f"Force-terminate {len(running)} running run(s) in {ts_dir.name}?", abort=True)

    if is_running():
        # signal daemon to stop sync threads via socket (simple approach: stop via daemon)
        # for now update status directly — daemon will skip terminated runs on next scheduler tick
        console.print("[yellow]Daemon is running — sync threads will stop on next scheduler cycle.[/yellow]")

    for r in running:
        update_run(ts_dir, r.player_name, status=RunStatus.TERMINATED)
        console.print(f"Marked [bold]{r.player_name}[/bold] → TERMINATED")

    update_testsuite_status(ts_dir, "TERMINATED")


@app.command("retry")
def retry(
    directory: Path = typer.Argument(..., help="Testsuite folder"),
):
    """Retry all FAILURE runs in a testsuite (dispatches to daemon, returns immediately)."""
    from daemon.runner import is_running, send_retry
    if not is_running():
        console.print("[red]Daemon is not running. Start it with: chartools daemon start[/red]")
        raise typer.Exit(1)

    ts_dir = directory.resolve()
    try:
        reply = send_retry(ts_dir)
    except ConnectionRefusedError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if reply.get("ok"):
        console.print(
            f"[green]Retry queued for '{ts_dir.name}' — watch: chartools status {ts_dir}[/green]"
        )
    else:
        console.print(f"[red]Error: {reply.get('error')}[/red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
