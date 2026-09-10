"""
Termination scheduler — checks every 5 minutes for runs that have passed
their terminates_at date and closes them out.

Flow per expired run:
  1. Stop sync threads via CollectManager
  2. Parse top logs → compute percentiles → write report JSON
  3. amgctl destroy
  4. Update run status → SUCCESS
  5. If all runs done → write summary.yaml, set testsuite status → SUCCESS
"""

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from core.amgctl import DeployResult, playout_destroy, poll_playout_logs, strip_ansi
from core.metrics import write_run_report, write_summary
from core.status import (
    RunStatus, TestsuiteStatus, compute_testsuite_status,
    read_status, scan_all_testsuites, update_run, update_testsuite_status,
)

if TYPE_CHECKING:
    from daemon.collect import CollectManager

log = logging.getLogger(__name__)

SCHEDULER_INTERVAL = 300  # 5 minutes


class TerminationScheduler:
    def __init__(self, collect_manager: "CollectManager"):
        self._collect = collect_manager

    def run(self, stop_event: threading.Event) -> None:
        """Run loop — called in a daemon thread by runner.py."""
        log.info("Termination scheduler started (interval=%ds)", SCHEDULER_INTERVAL)
        while not stop_event.is_set():
            try:
                self._check_all()
            except Exception as e:
                log.error("Scheduler check error: %s", e)
            stop_event.wait(SCHEDULER_INTERVAL)

    def _check_all(self) -> None:
        now = datetime.now(timezone.utc)
        for ts_dir in scan_all_testsuites():
            try:
                self._check_testsuite(ts_dir, now)
            except Exception as e:
                log.error("Error processing %s: %s", ts_dir, e)

    def _check_testsuite(self, ts_dir: Path, now: datetime) -> None:
        state = read_status(ts_dir)
        if state.status not in (TestsuiteStatus.RUNNING, TestsuiteStatus.PARTIAL_FAILURE):
            return

        any_terminated = False
        for run in state.runs:
            if run.status != RunStatus.RUNNING:
                continue
            if not run.terminates_at:
                continue
            try:
                terminates_at = datetime.fromisoformat(run.terminates_at)
            except ValueError:
                log.warning("Invalid terminates_at for %s: %s", run.player_name, run.terminates_at)
                continue

            # ensure timezone-aware comparison
            if terminates_at.tzinfo is None:
                terminates_at = terminates_at.replace(tzinfo=timezone.utc)

            if now >= terminates_at:
                log.info("Run %s has expired — terminating", run.player_name)
                self._terminate_run(ts_dir, run, state)
                any_terminated = True

        if any_terminated:
            # re-read state after updates and recompute testsuite status
            try:
                state = read_status(ts_dir)
                new_status = compute_testsuite_status(state.runs)
                update_testsuite_status(ts_dir, new_status)

                # if everything is done, write the summary
                if new_status == TestsuiteStatus.SUCCESS:
                    self._write_summary(ts_dir, state)
            except Exception as e:
                log.error("Error finalising testsuite %s: %s", ts_dir, e)

    def _terminate_run(self, ts_dir: Path, run, state) -> None:
        player_name = run.player_name

        # 1. stop sync threads
        self._collect.stop_run(player_name)

        # 2. build container log map → write per-run report
        log_dir = ts_dir / run.testcase / "logs"
        container_logs = {}
        if log_dir.exists():
            for p in log_dir.glob("top_*.log"):
                container_name = p.stem[4:]  # strip "top_"
                container_logs[container_name] = p

        try:
            write_run_report(
                report_dir=ts_dir / run.testcase / "report",
                player_name=player_name,
                testsuite_id=state.testsuite_id,
                testcase_name=run.testcase,
                nodetaint=_get_nodetaint(ts_dir),
                cp_release=state.cp_release,
                num_days=_get_num_days(ts_dir, run.testcase),
                poll_interval_seconds=_get_poll_interval(ts_dir, run.testcase),
                crash_events=run.crash_events,
                container_log_paths=container_logs,
            )
            log.info("Report written for %s", player_name)
        except Exception as e:
            log.error("Report generation failed for %s: %s", player_name, e)

        # 3. amgctl destroy
        try:
            log.info("Destroying playout %s...", player_name)
            destroy_res = playout_destroy(player_name)
            destroy_log = strip_ansi(destroy_res.stdout + destroy_res.stderr)
            destroy_log_path = ts_dir / run.testcase / "logs" / "destroy.log"
            destroy_log_path.parent.mkdir(parents=True, exist_ok=True)
            destroy_log_path.write_text(destroy_log)

            # poll until destroy PR merges
            result, full_log = poll_playout_logs(player_name, timeout_seconds=1200)
            with open(destroy_log_path, "a") as f:
                f.write("\n--- destroy log poll ---\n")
                f.write(full_log)

            if result not in (DeployResult.PR_CREATED, DeployResult.NO_CHANGE):
                log.warning("Destroy for %s ended with result=%s", player_name, result)
        except Exception as e:
            log.error("Destroy failed for %s: %s — marking SUCCESS anyway", player_name, e)

        # 4. mark run SUCCESS
        update_run(ts_dir, player_name, status=RunStatus.SUCCESS)
        log.info("Run %s marked SUCCESS", player_name)

    def _write_summary(self, ts_dir: Path, state) -> None:
        run_reports = []
        for run in state.runs:
            report_path = ts_dir / run.testcase / "report" / "report.json"
            if report_path.exists():
                import json
                try:
                    run_reports.append(json.loads(report_path.read_text()))
                except Exception:
                    pass

        nodetaint = _get_nodetaint(ts_dir)
        generated_at = datetime.now(timezone.utc).isoformat()
        write_summary(report_dir, state.testsuite_id, nodetaint,
                      state.cp_release, generated_at, run_reports)
        log.info("Summary written for testsuite %s", state.testsuite_id)


# ---------------------------------------------------------------------------
# Config helpers — read from config.yaml without raising
# ---------------------------------------------------------------------------

def _get_nodetaint(ts_dir: Path) -> str:
    try:
        from core.config import load_config
        return load_config(ts_dir / "config.yaml").nodetaint
    except Exception:
        return "unknown"


def _get_num_days(ts_dir: Path, testcase_name: str) -> int:
    try:
        from core.config import load_config
        cfg = load_config(ts_dir / "config.yaml")
        tc = next((t for t in cfg.testcases if t.name == testcase_name), None)
        return tc.num_days if tc else 0
    except Exception:
        return 0


def _get_poll_interval(ts_dir: Path, testcase_name: str) -> int:
    try:
        from core.config import load_config
        cfg = load_config(ts_dir / "config.yaml")
        tc = next((t for t in cfg.testcases if t.name == testcase_name), None)
        return tc.poll_interval_seconds if tc else 5
    except Exception:
        return 5
