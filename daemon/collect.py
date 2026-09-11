"""
Per-run metrics collection.

Responsibilities:
  - Single monitor thread per player (_monitor_loop) with three phases:
      Phase 1: poll kubectl every POD_STATUS_INTERVAL seconds until Running
      Phase 2: inject collect_top.sh into monitored containers; mark RUNNING
      Phase 3: sync top logs every SYNC_INTERVAL; detect crashes; loop back to 1
  - On daemon restart, reattach_run skips phases 1+2 and goes straight to phase 3
  - Allow terminate.py to stop the monitor thread for a run
"""

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from core.status import RunState, RunStatus, read_status, update_run

log = logging.getLogger(__name__)

# kubeconfig path — override with CHARTOOLS_KUBECONFIG env var
KUBECONFIG = Path(
    os.environ.get("CHARTOOLS_KUBECONFIG",
                   os.path.expanduser("~/lh_upgrade/k8s_player/kubeconfig.yaml"))
)

# kubectl binary path — override with CHARTOOLS_KUBECTL env var
KUBECTL_BIN = os.environ.get("CHARTOOLS_KUBECTL", "kubectl")

SYNC_INTERVAL = 60          # seconds between kubectl cp syncs (phase 3)
POD_STATUS_INTERVAL = 30    # seconds between pod status polls (phase 1)

# Containers to monitor — checked as regex against pod container names
MONITORED_PATTERNS = [r"^player1$", r".*tardis.*", r".*vanxio.*"]

COLLECT_TOP_SCRIPT = """\
#!/bin/sh
# Args: <container_name> <interval_seconds>
CONTAINER=$1
INTERVAL=$2
(
  while true; do
    printf '%s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> /mnt/top_${CONTAINER}.log
    top -b -n 1 >> /mnt/top_${CONTAINER}.log
    sleep ${INTERVAL}
  done
) &
echo $! > /mnt/top_${CONTAINER}.pid
"""

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---------------------------------------------------------------------------
# kubectl helpers
# ---------------------------------------------------------------------------

def _kubectl(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [KUBECTL_BIN, f"--kubeconfig={KUBECONFIG}"] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=timeout,
    )


def get_monitored_containers(kubectl_ns: str, pod_name: str) -> List[str]:
    """Return container names in the pod that match MONITORED_PATTERNS."""
    res = _kubectl("get", "pod", pod_name, "-n", kubectl_ns,
                   "-o", "jsonpath={.spec.containers[*].name}")
    if res.returncode != 0:
        log.warning("Could not list containers for %s: %s", pod_name, res.stderr.strip())
        return []
    containers = res.stdout.strip().split()
    matched = []
    for c in containers:
        for pat in MONITORED_PATTERNS:
            if re.match(pat, c):
                matched.append(c)
                break
    return matched


def _get_pod_status(kubectl_ns: str, pod_name: str) -> str:
    """
    Return a short status string for the pod, e.g. "Running", "Pending/CrashLoopBackOff".
    Returns "Unknown/<error>" when kubectl fails.
    """
    res = _kubectl(
        "get", "pod", pod_name, "-n", kubectl_ns,
        "-o", "jsonpath={.status.phase},{.status.containerStatuses[0].state.waiting.reason}",
    )
    if res.returncode != 0:
        err = res.stderr.strip()[:60]
        return f"Unknown/{err}" if err else "Unknown"
    out = res.stdout.strip()
    if not out:
        return "Unknown"
    parts = out.split(",", 1)
    phase = parts[0].strip() or "Unknown"
    reason = (parts[1].strip() if len(parts) > 1 else "").strip("\"'")
    if reason and reason not in ("null", ""):
        return f"{phase}/{reason}"
    return phase


def _inject_script(kubectl_ns: str, pod_name: str, container: str,
                   poll_interval_seconds: int) -> None:
    """Copy collect_top.sh to pod and start it in the background."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(COLLECT_TOP_SCRIPT)
        script_path = f.name
    try:
        res = _kubectl("cp", "-n", kubectl_ns, "-c", container,
                       script_path,
                       f"{kubectl_ns}/{pod_name}:/mnt/collect_top.sh")
        if res.returncode != 0:
            raise RuntimeError(f"kubectl cp script failed: {res.stderr.strip()}")

        res = _kubectl("exec", "-n", kubectl_ns, pod_name, "-c", container,
                       "--", "chmod", "+x", "/mnt/collect_top.sh")
        if res.returncode != 0:
            raise RuntimeError(f"chmod failed: {res.stderr.strip()}")

        cmd = f"/mnt/collect_top.sh {container} {poll_interval_seconds}"
        res = _kubectl("exec", "-n", kubectl_ns, pod_name, "-c", container,
                       "--", "/bin/sh", "-c", cmd)
        if res.returncode != 0:
            raise RuntimeError(f"script start failed: {res.stderr.strip()}")

        log.info("Injected collect_top.sh into %s/%s", pod_name, container)
    finally:
        os.unlink(script_path)


def _count_samples(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    count = 0
    with open(log_path, errors="replace") as f:
        for line in f:
            if _TIMESTAMP_RE.match(line.rstrip()):
                count += 1
    return count


def _log_event(events_log: Path, event: str, detail: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    events_log.parent.mkdir(parents=True, exist_ok=True)
    with open(events_log, "a") as f:
        f.write(f"{ts} {event} {detail}\n")


def wait_for_pod_running(kubectl_ns: str, pod_name: str, timeout: int = 1200) -> bool:
    """Poll until pod phase == Running. Returns True on success."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = _kubectl("get", "pod", pod_name, "-n", kubectl_ns,
                       "-o", "jsonpath={.status.phase}")
        if res.stdout.strip() == "Running":
            return True
        time.sleep(10)
    return False


# ---------------------------------------------------------------------------
# Monitor loop (single thread per player)
# ---------------------------------------------------------------------------

def _monitor_loop(
    stop_event: threading.Event,
    ts_dir: Path,
    testcase_name: str,
    player_name: str,
    pod_name: str,
    kubectl_ns: str,
    poll_interval_seconds: int,
    num_days: int,
    skip_to_sync: bool = False,
) -> None:
    """
    Single monitoring thread per player.

    Phase 1: Poll pod status every POD_STATUS_INTERVAL until Running (no timeout).
    Phase 2: Inject collect_top.sh; mark run as RUNNING.
    Phase 3: Sync top logs every SYNC_INTERVAL; check pod health; loop back to
             Phase 1 on crash.

    skip_to_sync=True skips phases 1+2 (used on daemon restart for already-Running pods).
    """
    events_log = ts_dir / testcase_name / "logs" / "events.log"

    def _wait_running() -> bool:
        """Poll until Running. Returns False if stop_event fires first."""
        log.info("Phase 1: waiting for pod %s to reach Running state", pod_name)
        while not stop_event.is_set():
            pod_status = _get_pod_status(kubectl_ns, pod_name)
            update_run(ts_dir, player_name, pod_status=pod_status)
            if pod_status.startswith("Running"):
                log.info("Pod %s reached Running state", pod_name)
                return True
            log.info("Pod %s: %s", pod_name, pod_status)
            stop_event.wait(POD_STATUS_INTERVAL)
        return False

    def _inject_and_mark_running(containers: List[str]) -> None:
        """Phase 2: inject scripts and flip status to RUNNING."""
        log.info("Phase 2: injecting collect_top.sh into %s (%d containers)",
                 pod_name, len(containers))
        for container in containers:
            try:
                _inject_script(kubectl_ns, pod_name, container, poll_interval_seconds)
            except Exception as e:
                log.error("Inject failed for %s/%s: %s", pod_name, container, e)

        now = datetime.now(timezone.utc)
        terminates_at = (now + timedelta(days=num_days)).isoformat()
        update_run(ts_dir, player_name,
                   status=RunStatus.RUNNING,
                   started_at=now.isoformat(),
                   terminates_at=terminates_at,
                   pod_status="Running")
        log.info("%s is now RUNNING — terminates at %s", player_name, terminates_at)

    # --- initial setup ---
    if not skip_to_sync:
        if not _wait_running():
            return
        containers = get_monitored_containers(kubectl_ns, pod_name)
        if not containers:
            log.warning("No monitored containers found in %s", pod_name)
        _inject_and_mark_running(containers)
    else:
        containers = get_monitored_containers(kubectl_ns, pod_name)
        log.info("Reattached to %s, containers: %s", pod_name, containers)

    # --- Phase 3: sync loop ---
    log.info("Phase 3: sync loop for %s (every %ds)", player_name, SYNC_INTERVAL)
    while not stop_event.is_set():
        # sync top logs from each container
        for container in containers:
            local_log = ts_dir / testcase_name / "logs" / f"top_{container}.log"
            try:
                res = _kubectl("cp", "-n", kubectl_ns, "-c", container,
                               f"{kubectl_ns}/{pod_name}:/mnt/top_{container}.log",
                               str(local_log))
                if res.returncode == 0:
                    count = _count_samples(local_log)
                    update_run(ts_dir, player_name, samples_collected=count)
                else:
                    log.warning("kubectl cp failed for %s/%s: %s",
                                pod_name, container, res.stderr.strip())
            except Exception as e:
                log.error("Sync error for %s/%s: %s", pod_name, container, e)

        # check pod health
        pod_status = _get_pod_status(kubectl_ns, pod_name)
        update_run(ts_dir, player_name, pod_status=pod_status)

        if not pod_status.startswith("Running"):
            log.warning("Pod %s no longer Running: %s — waiting for recovery",
                        pod_name, pod_status)
            _log_event(events_log, "CRASH", f"pod_status={pod_status}")
            try:
                state = read_status(ts_dir)
                for r in state.runs:
                    if r.player_name == player_name:
                        update_run(ts_dir, player_name, crash_events=r.crash_events + 1)
                        break
            except Exception:
                pass
            # Phase 1 again: wait for recovery
            if not _wait_running():
                return
            # Phase 2 again: re-inject
            _inject_and_mark_running(containers)

        stop_event.wait(SYNC_INTERVAL)


# ---------------------------------------------------------------------------
# CollectManager
# ---------------------------------------------------------------------------

class _RunCollector:
    """Holds the monitor thread for a single run."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)


class CollectManager:
    def __init__(self):
        self._runs: Dict[str, _RunCollector] = {}
        self._lock = threading.Lock()

    def start_run(
        self,
        ts_dir: Path,
        testcase_name: str,
        player_name: str,
        pod_name: str,
        kubectl_ns: str,
        poll_interval_seconds: int,
        num_days: int,
    ) -> None:
        """
        Immediately mark run as PROVISIONING and spawn a monitor thread.
        The thread handles pod polling, script injection, and log sync.
        """
        update_run(ts_dir, player_name, status=RunStatus.PROVISIONING)

        collector = _RunCollector()
        t = threading.Thread(
            target=_monitor_loop,
            args=(collector.stop_event, ts_dir, testcase_name, player_name,
                  pod_name, kubectl_ns, poll_interval_seconds, num_days, False),
            name=f"monitor-{player_name}",
            daemon=True,
        )
        collector.thread = t
        t.start()
        log.info("Monitor thread started for %s (PROVISIONING)", player_name)

        with self._lock:
            self._runs[player_name] = collector

    def reattach_run(
        self,
        ts_dir: Path,
        run: RunState,
    ) -> None:
        """
        Re-attach monitor thread for a RUNNING run after daemon restart.
        Skips phases 1+2 — pod already Running, script already injected.
        """
        parts = run.player_name.split("_", 2)
        namespace, feed_id, headend = parts[0], parts[1], parts[2]
        pod_name = f"player-{namespace}-{feed_id}-{headend}-player-0"
        kubectl_ns = f"{namespace}-playout"

        try:
            from core.config import load_config
            cfg = load_config(ts_dir / "config.yaml")
            tc = next((t for t in cfg.testcases if t.name == run.testcase), None)
            poll_interval = tc.poll_interval_seconds if tc else 5
            num_days = tc.num_days if tc else 1
        except Exception:
            poll_interval = 5
            num_days = 1

        collector = _RunCollector()
        t = threading.Thread(
            target=_monitor_loop,
            args=(collector.stop_event, ts_dir, run.testcase, run.player_name,
                  pod_name, kubectl_ns, poll_interval, num_days, True),
            name=f"monitor-{run.player_name}",
            daemon=True,
        )
        collector.thread = t
        t.start()
        log.info("Reattached monitor thread for %s (skip_to_sync)", run.player_name)

        with self._lock:
            self._runs[run.player_name] = collector

    def stop_run(self, player_name: str) -> None:
        with self._lock:
            collector = self._runs.pop(player_name, None)
        if collector:
            collector.stop()
            log.info("Stopped monitor thread for %s", player_name)
