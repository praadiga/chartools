"""
Testsuite and testcase deploy flow.

Each testsuite runs in its own thread (dispatched by runner.py).
Testcases within a testsuite are deployed serially.
"""

import logging
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.amgctl import (
    AmgctlError, DeployResult,
    allocate_headend, extract_cp_release,
    playout_create, playout_get,
    poll_playout_logs, strip_ansi,
)
from core.config import TestcaseConfig, TestsuiteConfig, load_config
from core.status import (
    RunState, RunStatus, TestsuiteStatus,
    compute_testsuite_status, read_status, update_run,
    update_testsuite_status, write_status, TestsuiteState,
)
from core.topology import patch_coreservice, patch_topology
from daemon.collect import CollectManager, wait_for_pod_running

log = logging.getLogger(__name__)

_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def _add_testsuite_log(ts_dir: Path) -> logging.FileHandler:
    """Attach a FileHandler that writes all log output to ts_dir/chartools.log."""
    ts_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(str(ts_dir / "chartools.log"))
    fh.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT))
    logging.getLogger().addHandler(fh)
    return fh


def _remove_testsuite_log(fh: logging.FileHandler) -> None:
    logging.getLogger().removeHandler(fh)
    fh.close()



# ---------------------------------------------------------------------------
# Review gate
# ---------------------------------------------------------------------------

def _review_gate(player_name: str, player_dir: Path) -> str:
    """
    Prompt user to review generated files before deployment.

    Returns 'deploy' or 'skip'.

    Behaviour:
      - Auto-deploys after 5 minutes if no input
      - 'wait'  → switches to indefinite block until user presses Enter
      - 'skip'  → cancels this testcase
      - Enter   → deploys immediately
    """
    print(f"\n{'='*60}")
    print(f"Files ready for testcase player: {player_name}")
    print(f"  topology.yaml    → {player_dir}/topology.yaml")
    print(f"  coreservice.yaml → {player_dir}/coreservice.yaml")
    print("Edit files in another terminal if needed.")
    print("Options: [Enter] deploy now  |  'wait' hold indefinitely  |  'skip' cancel")
    print("Auto-deploying in 5 minutes...")
    print("="*60)
    sys.stdout.flush()

    result: list = [None]

    def _read():
        try:
            result[0] = sys.stdin.readline().strip().lower()
        except (EOFError, OSError):
            result[0] = ""

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(timeout=300)  # 5-min timeout

    choice = result[0]

    if choice is None:
        print(f"\n5 minutes elapsed — deploying {player_name} now.")
        sys.stdout.flush()
        return "deploy"

    if choice == "skip":
        print(f"Skipping {player_name}.")
        return "skip"

    if choice == "wait":
        print("Waiting indefinitely... press Enter when editing is done.")
        sys.stdout.flush()
        try:
            sys.stdin.readline()
        except (EOFError, OSError):
            pass
        return "deploy"

    return "deploy"


# ---------------------------------------------------------------------------
# Single testcase deploy
# ---------------------------------------------------------------------------

def _deploy_testcase(
    ts_dir: Path,
    cfg: TestsuiteConfig,
    tc: TestcaseConfig,
    cp_release: Optional[str],
    collect_manager: CollectManager,
    is_retry: bool = False,
) -> Optional[str]:
    """
    Deploy one testcase.

    Returns updated cp_release (derived if missing) or the passed-in value.
    Updates status.yaml at each step.
    On skip or failure the run is marked accordingly and None is returned for
    cp_release only if it was never derived; otherwise the existing value is kept.
    """
    # ------------------------------------------------------------------ step 2
    # Allocate headend + derive cp_release on first testcase
    headend_id  = allocate_headend(cfg.ref_namespace, cfg.ref_feed_id)
    player_name = f"{cfg.ref_namespace}_{cfg.ref_feed_id}_{headend_id}"
    kubectl_ns  = f"{cfg.ref_namespace}-playout"
    pod_name    = f"player-{cfg.ref_namespace}-{cfg.ref_feed_id}-{headend_id}-player-0"
    player_dir  = ts_dir / tc.name / player_name
    log_dir     = ts_dir / tc.name / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    deploy_log  = log_dir / "deploy.log"

    def _append(label: str, output: str) -> None:
        sep = "-" * 60
        with open(deploy_log, "a") as _f:
            _f.write(f"\n{sep}\n{label}\n{sep}\n{output}\n")

    # Write DEPLOYING to status.yaml (add run entry if fresh)
    _ensure_run_entry(ts_dir, tc.name, player_name, headend_id)
    update_run(ts_dir, player_name, status=RunStatus.DEPLOYING)

    if is_retry:
        log.info("Retry: skipping amgctl get — files already exist at %s", player_dir)
        get_output = ""
    else:
        # ---------------------------------------------------------------- step 3+4
        # amgctl get (pre-deletes export_dir if exists)
        log.info("amgctl get for %s → %s", cfg.reference_player, player_dir)
        try:
            get_output = playout_get(cfg.reference_player, player_dir)
        except (AmgctlError, Exception) as e:
            log.error("amgctl get failed: %s", e)
            _append(f"amgctl cp app playout get -n {cfg.reference_player}", str(e))
            update_run(ts_dir, player_name, status=RunStatus.FAILURE, error_msg=str(e))
            return cp_release
        _append(f"amgctl cp app playout get -n {cfg.reference_player}", get_output)

    # Derive cp_release if not yet known — parse from amgctl get output / exported files
    if cp_release is None:
        cp_release = extract_cp_release(get_output, player_dir)
        if cp_release:
            log.info("Derived cp_release: %s", cp_release)
            _write_cp_release(ts_dir, cp_release)
        else:
            log.error("Could not derive cp_release for %s — cannot deploy", player_name)
            update_run(ts_dir, player_name, status=RunStatus.FAILURE,
                       error_msg="could not derive cp_release from amgctl get output")
            return None

    if not is_retry:
        # ---------------------------------------------------------------- step 5+6
        # Patch files (every testcase gets its own fresh export + patch)
        topology_path    = player_dir / "topology.yaml"
        coreservice_path = player_dir / "coreservice.yaml"

        if not topology_path.exists() or not coreservice_path.exists():
            msg = f"topology.yaml or coreservice.yaml missing in {player_dir}"
            log.error(msg)
            update_run(ts_dir, player_name, status=RunStatus.FAILURE, error_msg=msg)
            return cp_release

        patch_coreservice(coreservice_path, tc.license_key)
        patch_topology(topology_path, tc.overrides, cfg.nodetaint, headend_id, cfg.ref_feed_id)
        log.info("Patched topology.yaml and coreservice.yaml for %s", player_name)

    # ----------------------------------------------------------------- review gate
    decision = _review_gate(player_name, player_dir)
    if decision == "skip":
        update_run(ts_dir, player_name, status=RunStatus.CANCELLED)
        return cp_release

    # ----------------------------------------------------------------- step 8
    # amgctl create
    log.info("Running amgctl create for %s (cp_release=%s)", player_name, cp_release)
    create_res = playout_create(cp_release, player_dir)
    _append(
        f"amgctl cp app playout create -r {cp_release} -i {player_dir}",
        strip_ansi(create_res.stdout + create_res.stderr),
    )

    # ----------------------------------------------------------------- step 9 (log poll)
    log.info("Polling amgctl logs for %s...", player_name)
    result, full_log = poll_playout_logs(player_name)
    _append(f"amgctl cp app playout logs -n {player_name}", full_log)

    if result == DeployResult.ALREADY_EXISTS:
        msg = ("player already exists in cloud — destroy it first with: "
               f"amgctl cp app playout destroy -n {player_name}")
        log.error(msg)
        update_run(ts_dir, player_name, status=RunStatus.FAILURE, error_msg=msg)
        return cp_release

    if result != DeployResult.PR_CREATED:
        msg = f"deployment did not succeed (result={result}) — check logs/{player_name}/deploy.log"
        log.error(msg)
        update_run(ts_dir, player_name, status=RunStatus.FAILURE, error_msg=msg)
        return cp_release

    log.info("PR created for %s — waiting for pod to reach Running state...", player_name)

    # ----------------------------------------------------------------- step 9 (kubectl poll)
    reached = wait_for_pod_running(kubectl_ns, pod_name, timeout=1200)
    if not reached:
        msg = f"pod {pod_name} did not reach Running state within 20 min"
        log.error(msg)
        update_run(ts_dir, player_name, status=RunStatus.FAILURE, error_msg=msg)
        return cp_release

    # ----------------------------------------------------------------- step 10
    now = datetime.now(timezone.utc)
    terminates_at = (now + timedelta(days=tc.num_days)).isoformat()
    update_run(
        ts_dir, player_name,
        status=RunStatus.RUNNING,
        started_at=now.isoformat(),
        terminates_at=terminates_at,
    )
    log.info("%s is RUNNING — terminates at %s", player_name, terminates_at)

    # ----------------------------------------------------------------- step 11
    collect_manager.start_run(ts_dir, tc.name, player_name, pod_name, kubectl_ns,
                               tc.poll_interval_seconds)
    return cp_release


def _ensure_run_entry(
    ts_dir: Path,
    testcase_name: str,
    player_name: str,
    headend_id: str,
) -> None:
    """Add a run entry in INITIATED state if one doesn't already exist."""
    import os, fcntl, yaml
    from core.status import _status_path
    path = _status_path(ts_dir)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            raw = yaml.safe_load(f) or {}
            runs = raw.get("runs") or []
            names = {r.get("player_name") for r in runs}
            if player_name not in names:
                runs.append({
                    "testcase":         testcase_name,
                    "player_name":      player_name,
                    "headend_id":       headend_id,
                    "status":           RunStatus.INITIATED,
                    "started_at":       None,
                    "terminates_at":    None,
                    "samples_collected": 0,
                    "crash_events":     0,
                    "error_msg":        None,
                })
                raw["runs"] = runs
            f.seek(0)
            f.truncate()
            yaml.safe_dump(raw, f, default_flow_style=False)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _write_cp_release(ts_dir: Path, cp_release: str) -> None:
    import os, fcntl, yaml
    from core.status import _status_path
    path = _status_path(ts_dir)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            raw = yaml.safe_load(f) or {}
            raw["cp_release"] = cp_release
            f.seek(0)
            f.truncate()
            yaml.safe_dump(raw, f, default_flow_style=False)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Testsuite deploy (called from runner.py in a thread)
# ---------------------------------------------------------------------------

def deploy_testsuite(ts_dir: Path, collect_manager: CollectManager) -> None:
    """
    Deploy all testcases in a testsuite directory.
    Creates status.yaml if not present, then deploys each testcase serially.
    """
    ts_log_fh = _add_testsuite_log(ts_dir)
    log.info("Starting deploy for testsuite: %s", ts_dir)

    # Load config
    try:
        cfg = load_config(ts_dir / "config.yaml")
    except Exception as e:
        log.error("Failed to load config.yaml for %s: %s", ts_dir, e)
        _remove_testsuite_log(ts_log_fh)
        return

    if not cfg.reference_player:
        log.error("reference_player not set in config.yaml — only reference_player path is supported in v1")
        return

    # Create status.yaml if this is a fresh run
    status_path = ts_dir / "status.yaml"
    if not status_path.exists():
        from datetime import datetime, timezone
        state = TestsuiteState(
            testsuite_id=ts_dir.name,
            status=TestsuiteStatus.INITIATED,
            created_at=datetime.now(timezone.utc).isoformat(),
            cp_release=cfg.cp_release,
            runs=[],
        )
        write_status(ts_dir, state)

    update_testsuite_status(ts_dir, TestsuiteStatus.RUNNING)

    cp_release = cfg.cp_release  # may be None — derived on first testcase

    for tc in cfg.testcases:
        log.info("Deploying testcase: %s", tc.name)
        cp_release = _deploy_testcase(ts_dir, cfg, tc, cp_release, collect_manager)
        # cp_release carries forward to subsequent testcases in this testsuite

    # Recompute testsuite status from run statuses
    try:
        state = read_status(ts_dir)
        ts_status = compute_testsuite_status(state.runs)
        update_testsuite_status(ts_dir, ts_status)
    except Exception as e:
        log.error("Could not update testsuite status: %s", e)

    log.info("Deploy complete for %s", ts_dir)
    _remove_testsuite_log(ts_log_fh)


# ---------------------------------------------------------------------------
# Retry flow — called from CLI `chartools retry <dir>`
# ---------------------------------------------------------------------------

def retry_testsuite(ts_dir: Path, collect_manager: CollectManager) -> None:
    """
    Retry all FAILURE runs in a testsuite. Skips file generation and starts
    from the review gate, using the existing files in each player dir.
    """
    log.info("Retrying FAILURE runs in %s", ts_dir)

    try:
        cfg = load_config(ts_dir / "config.yaml")
        state = read_status(ts_dir)
    except Exception as e:
        log.error("Failed to load config or status for %s: %s", ts_dir, e)
        return

    cp_release = state.cp_release or cfg.cp_release

    for run in state.runs:
        if run.status != RunStatus.FAILURE:
            continue
        tc = next((t for t in cfg.testcases if t.name == run.testcase), None)
        if tc is None:
            log.warning("Testcase '%s' not found in config — skipping retry", run.testcase)
            continue

        log.info("Retrying testcase: %s (player %s)", run.testcase, run.player_name)
        cp_release = _deploy_testcase(
            ts_dir, cfg, tc, cp_release, collect_manager, is_retry=True
        )

    try:
        state = read_status(ts_dir)
        update_testsuite_status(ts_dir, compute_testsuite_status(state.runs))
    except Exception as e:
        log.error("Could not update testsuite status after retry: %s", e)
