"""
Main daemon process.

Start with: chartools daemon start
Communicates via Unix socket at ~/.chartools/daemon.sock.
Accepts: {"action": "add", "dir": "<abs_path>"}
"""

import json
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Dict

from core.amgctl import ensure_amgctl
from core.status import RunStatus, read_status, scan_all_testsuites, update_run
from daemon.collect import CollectManager
from daemon.deploy import deploy_testsuite
from daemon.terminate import TerminationScheduler

log = logging.getLogger(__name__)

SOCKET_PATH = Path.home() / ".chartools" / "daemon.sock"
PID_PATH    = Path.home() / ".chartools" / "daemon.pid"


class Daemon:
    def __init__(self):
        self._stop = threading.Event()
        self._collect = CollectManager()
        self._scheduler = TerminationScheduler(self._collect)
        self._deploy_threads: Dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------ start

    def start(self) -> None:
        _setup_logging()
        _write_pid()

        try:
            ensure_amgctl()
        except Exception as e:
            log.error("amgctl setup failed: %s", e)
            sys.exit(1)

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT,  self._on_signal)

        self._resume_running()

        sched_thread = threading.Thread(
            target=self._scheduler.run, args=(self._stop,),
            name="scheduler", daemon=True,
        )
        sched_thread.start()

        log.info("Daemon started (PID %d)", os.getpid())
        self._listen()   # blocks until stop event

    # ------------------------------------------------------------------ resume

    def _resume_running(self) -> None:
        for ts_dir in scan_all_testsuites():
            try:
                state = read_status(ts_dir)
            except Exception:
                continue
            for run in state.runs:
                if run.status == RunStatus.RUNNING:
                    try:
                        self._collect.reattach_run(ts_dir, run)
                        log.info("Reattached %s", run.player_name)
                    except Exception as e:
                        log.error("Reattach failed for %s: %s", run.player_name, e)
                elif run.status == RunStatus.PROVISIONING:
                    # amgctl already done; pod still coming up — restart monitor from phase 1
                    try:
                        from core.config import load_config
                        cfg = load_config(ts_dir / "config.yaml")
                        tc = next((t for t in cfg.testcases if t.name == run.testcase), None)
                        if tc:
                            parts = run.player_name.split("_", 2)
                            pod_name = f"player-{parts[0]}-{parts[1]}-{parts[2]}-player-0"
                            kubectl_ns = f"{parts[0]}-playout"
                            self._collect.start_run(
                                ts_dir, run.testcase, run.player_name, pod_name, kubectl_ns,
                                tc.poll_interval_seconds, tc.num_days,
                            )
                            log.info("Restarted monitor thread for PROVISIONING run %s",
                                     run.player_name)
                    except Exception as e:
                        log.error("Could not restart monitor for %s: %s", run.player_name, e)
                elif run.status == RunStatus.DEPLOYING:
                    # Daemon restarted mid-deploy — we can't recover the deploy
                    # thread, so mark it FAILURE so it can be retried.
                    log.warning("Run %s stuck in DEPLOYING on restart — marking FAILURE",
                                run.player_name)
                    update_run(ts_dir, run.player_name, status=RunStatus.FAILURE,
                               error_msg="daemon restarted mid-deploy")

    # ------------------------------------------------------------------ socket

    def _listen(self) -> None:
        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCKET_PATH))
        srv.listen(5)
        srv.settimeout(1.0)

        log.info("Listening on %s", SOCKET_PATH)
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=self._handle_conn, args=(conn,),
                    daemon=True,
                ).start()
        finally:
            srv.close()
            _cleanup()

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            data = conn.recv(4096).decode()
            msg  = json.loads(data)
            action = msg.get("action")

            if action == "add":
                ts_dir = Path(msg["dir"]).resolve()
                reply  = self._add_testsuite(ts_dir)
            elif action == "retry":
                ts_dir = Path(msg["dir"]).resolve()
                reply  = self._retry_testsuite(ts_dir)
            else:
                reply = {"ok": False, "error": f"unknown action: {action}"}

            conn.sendall(json.dumps(reply).encode())
        except Exception as e:
            try:
                conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode())
            except Exception:
                pass
        finally:
            conn.close()

    def _add_testsuite(self, ts_dir: Path) -> dict:
        if not ts_dir.exists():
            return {"ok": False, "error": f"directory not found: {ts_dir}"}
        config_path = ts_dir / "config.yaml"
        if not config_path.exists():
            return {"ok": False, "error": f"config.yaml not found in {ts_dir}"}

        key = str(ts_dir)
        if key in self._deploy_threads and self._deploy_threads[key].is_alive():
            return {"ok": False, "error": f"{ts_dir.name} is already being deployed"}

        t = threading.Thread(
            target=deploy_testsuite,
            args=(ts_dir, self._collect),
            name=f"deploy-{ts_dir.name}",
            daemon=True,
        )
        self._deploy_threads[key] = t
        t.start()
        log.info("Started deploy thread for %s", ts_dir.name)
        return {"ok": True}

    def _retry_testsuite(self, ts_dir: Path) -> dict:
        if not ts_dir.exists():
            return {"ok": False, "error": f"directory not found: {ts_dir}"}

        key = f"retry-{ts_dir}"
        if key in self._deploy_threads and self._deploy_threads[key].is_alive():
            return {"ok": False, "error": f"{ts_dir.name} retry is already running"}

        from daemon.deploy import retry_testsuite
        t = threading.Thread(
            target=retry_testsuite,
            args=(ts_dir, self._collect),
            name=f"retry-{ts_dir.name}",
            daemon=True,
        )
        self._deploy_threads[key] = t
        t.start()
        log.info("Started retry thread for %s", ts_dir.name)
        return {"ok": True}

    # ------------------------------------------------------------------ signal

    def _on_signal(self, signum, frame) -> None:
        log.info("Signal %d received — shutting down", signum)
        self._stop.set()


# ---------------------------------------------------------------------------
# Module-level helpers (used by CLI commands too)
# ---------------------------------------------------------------------------

def _send(msg: dict) -> dict:
    """Send a message to the running daemon. Returns response dict."""
    if not SOCKET_PATH.exists():
        raise ConnectionRefusedError(
            "Daemon is not running. Start it with: chartools daemon start"
        )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(SOCKET_PATH))
    sock.sendall(json.dumps(msg).encode())
    data = sock.recv(4096)
    sock.close()
    return json.loads(data)


def send_add(ts_dir: Path) -> dict:
    """Send 'add' message to running daemon."""
    return _send({"action": "add", "dir": str(ts_dir)})


def send_retry(ts_dir: Path) -> dict:
    """Send 'retry' message to running daemon."""
    return _send({"action": "retry", "dir": str(ts_dir)})


def is_running() -> bool:
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def stop_daemon() -> None:
    if not PID_PATH.exists():
        print("Daemon is not running.")
        return
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to daemon (PID {pid})")
    except (ValueError, ProcessLookupError):
        print("No running daemon found (stale PID file).")
        PID_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_pid() -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))


def _cleanup() -> None:
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink(missing_ok=True)
    if PID_PATH.exists():
        PID_PATH.unlink(missing_ok=True)
    log.info("Daemon stopped")


LOG_PATH = Path.home() / ".chartools" / "daemon.log"

def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S"
    logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt)
    fh = logging.FileHandler(str(LOG_PATH))
    fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    logging.getLogger().addHandler(fh)
    logging.getLogger().info("Logging to %s", LOG_PATH)
