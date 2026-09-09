"""
Per-run metrics collection.

Responsibilities:
  - Inject collect_top.sh into each monitored container after pod is Running
  - Run a sync thread per container that kubectl-cp's top logs to the server every 60s
  - Detect pod/container crashes, wait for recovery, re-inject script
  - Allow terminate.py to stop all threads for a run
  - On daemon restart, re-attach sync threads without re-injecting (container kept writing)
"""

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from core.status import RunState, update_run

log = logging.getLogger(__name__)

# kubeconfig path — override with CHARTOOLS_KUBECONFIG env var
KUBECONFIG = Path(
    os.environ.get("CHARTOOLS_KUBECONFIG",
                   os.path.expanduser("~/lh_upgrade/k8s_player/kubeconfig.yaml"))
)

SYNC_INTERVAL = 60  # seconds between kubectl cp syncs

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

_POD_CRASH_ERRORS = ("not found", "container not running", "error from server", "not running")


# ---------------------------------------------------------------------------
# kubectl helpers
# ---------------------------------------------------------------------------

def _kubectl(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", f"--kubeconfig={KUBECONFIG}"] + list(args),
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


def _is_pod_crash_error(stderr: str) -> bool:
    lower = stderr.lower()
    return any(kw in lower for kw in _POD_CRASH_ERRORS)


def _inject_script(kubectl_ns: str, pod_name: str, container: str,
                   poll_interval_seconds: int) -> None:
    """Copy collect_top.sh to pod and start it in the background."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(COLLECT_TOP_SCRIPT)
        script_path = f.name
    try:
        # copy script to pod
        res = _kubectl("cp", "-n", kubectl_ns, "-c", container,
                       script_path,
                       f"{kubectl_ns}/{pod_name}:/mnt/collect_top.sh")
        if res.returncode != 0:
            raise RuntimeError(f"kubectl cp script failed: {res.stderr.strip()}")

        # chmod +x
        res = _kubectl("exec", "-n", kubectl_ns, pod_name, "-c", container,
                       "--", "chmod", "+x", "/mnt/collect_top.sh")
        if res.returncode != 0:
            raise RuntimeError(f"chmod failed: {res.stderr.strip()}")

        # start in background
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
    import datetime
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
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
# Sync thread
# ---------------------------------------------------------------------------

def _sync_loop(
    stop_event: threading.Event,
    ts_dir: Path,
    player_name: str,
    container: str,
    pod_name: str,
    kubectl_ns: str,
    poll_interval_seconds: int,
) -> None:
    local_log = ts_dir / "logs" / player_name / f"top_{container}.log"
    events_log = ts_dir / "logs" / player_name / "events.log"
    local_log.parent.mkdir(parents=True, exist_ok=True)

    while not stop_event.is_set():
        try:
            res = _kubectl("cp", "-n", kubectl_ns, "-c", container,
                           f"{kubectl_ns}/{pod_name}:/mnt/top_{container}.log",
                           str(local_log))
            if res.returncode != 0:
                stderr_lower = res.stderr.lower()
                if any(kw in stderr_lower for kw in _POD_CRASH_ERRORS):
                    raise _ContainerCrash(res.stderr.strip())
                log.warning("kubectl cp non-fatal error for %s/%s: %s",
                            pod_name, container, res.stderr.strip())
            else:
                count = _count_samples(local_log)
                update_run(ts_dir, player_name, samples_collected=count)

        except _ContainerCrash as e:
            log.warning("Crash detected for %s/%s: %s", pod_name, container, e)
            _log_event(events_log, "CRASH", f"{container}: {e}")

            # read current crash_events count and increment
            try:
                from core.status import read_status
                state = read_status(ts_dir)
                for r in state.runs:
                    if r.player_name == player_name:
                        new_count = r.crash_events + 1
                        update_run(ts_dir, player_name, crash_events=new_count)
                        break
            except Exception:
                pass

            log.info("Waiting for pod %s to recover...", pod_name)
            recovered = wait_for_pod_running(kubectl_ns, pod_name, timeout=1200)
            if not recovered:
                log.error("Pod %s did not recover within timeout", pod_name)
                _log_event(events_log, "RECOVERY_TIMEOUT", pod_name)
                # keep looping — the scheduler will handle termination
            else:
                log.info("Pod %s recovered, re-injecting script into %s", pod_name, container)
                try:
                    _inject_script(kubectl_ns, pod_name, container, poll_interval_seconds)
                    _log_event(events_log, "RECOVERED", container)
                except Exception as ex:
                    log.error("Re-inject failed for %s/%s: %s", pod_name, container, ex)

        except Exception as e:
            log.error("Unexpected error in sync thread %s/%s: %s", pod_name, container, e)

        stop_event.wait(SYNC_INTERVAL)


class _ContainerCrash(Exception):
    pass


# ---------------------------------------------------------------------------
# CollectManager
# ---------------------------------------------------------------------------

class _RunCollector:
    """Holds sync threads for a single run."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.threads: List[threading.Thread] = []

    def stop(self) -> None:
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=10)


class CollectManager:
    def __init__(self):
        self._runs: Dict[str, _RunCollector] = {}
        self._lock = threading.Lock()

    def start_run(
        self,
        ts_dir: Path,
        player_name: str,
        pod_name: str,
        kubectl_ns: str,
        poll_interval_seconds: int,
    ) -> None:
        """Inject scripts and start sync threads for a newly Running pod."""
        containers = get_monitored_containers(kubectl_ns, pod_name)
        if not containers:
            log.warning("No monitored containers found in %s", pod_name)

        collector = _RunCollector()
        for container in containers:
            try:
                _inject_script(kubectl_ns, pod_name, container, poll_interval_seconds)
            except Exception as e:
                log.error("Inject failed for %s/%s: %s", pod_name, container, e)

            t = threading.Thread(
                target=_sync_loop,
                args=(collector.stop_event, ts_dir, player_name,
                      container, pod_name, kubectl_ns, poll_interval_seconds),
                name=f"sync-{player_name}-{container}",
                daemon=True,
            )
            t.start()
            collector.threads.append(t)
            log.info("Sync thread started for %s/%s", player_name, container)

        with self._lock:
            self._runs[player_name] = collector

    def reattach_run(
        self,
        ts_dir: Path,
        run: RunState,
    ) -> None:
        """
        Re-attach sync threads for a RUNNING run after daemon restart.
        Does NOT re-inject the script — the container has been collecting uninterrupted.
        """
        namespace, feed_id, headend = run.player_name.split("_", 2)
        pod_name = f"player-{namespace}-{feed_id}-{headend}-player-0"
        kubectl_ns = f"{namespace}-playout"

        # get poll_interval from config.yaml
        try:
            from core.config import load_config
            cfg = load_config(ts_dir / "config.yaml")
            tc = next((t for t in cfg.testcases if t.name == run.testcase), None)
            poll_interval = tc.poll_interval_seconds if tc else 5
        except Exception:
            poll_interval = 5

        containers = get_monitored_containers(kubectl_ns, pod_name)
        if not containers:
            log.warning("No containers found for reattach of %s", run.player_name)

        collector = _RunCollector()
        for container in containers:
            t = threading.Thread(
                target=_sync_loop,
                args=(collector.stop_event, ts_dir, run.player_name,
                      container, pod_name, kubectl_ns, poll_interval),
                name=f"sync-{run.player_name}-{container}",
                daemon=True,
            )
            t.start()
            collector.threads.append(t)
            log.info("Reattached sync thread for %s/%s", run.player_name, container)

        with self._lock:
            self._runs[run.player_name] = collector

    def stop_run(self, player_name: str) -> None:
        with self._lock:
            collector = self._runs.pop(player_name, None)
        if collector:
            collector.stop()
            log.info("Stopped sync threads for %s", player_name)
