"""Read/write status.yaml with file locking."""

import fcntl
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

CHARACTERIZATION_BASE = Path(
    os.environ.get("CHARTOOLS_BASE",
                   os.path.expanduser("~/characterization"))
)


class RunStatus:
    INITIATED  = "INITIATED"
    DEPLOYING  = "DEPLOYING"
    PENDING    = "PENDING"
    RUNNING    = "RUNNING"
    FAILURE    = "FAILURE"
    SUCCESS    = "SUCCESS"
    TERMINATED = "TERMINATED"
    CANCELLED  = "CANCELLED"


class TestsuiteStatus:
    INITIATED       = "INITIATED"
    RUNNING         = "RUNNING"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    SUCCESS         = "SUCCESS"
    TERMINATED      = "TERMINATED"


@dataclass
class RunState:
    testcase: str
    player_name: str
    headend_id: str
    status: str
    started_at: Optional[str] = None
    terminates_at: Optional[str] = None
    samples_collected: int = 0
    crash_events: int = 0
    error_msg: Optional[str] = None


@dataclass
class TestsuiteState:
    testsuite_id: str
    status: str
    created_at: str
    cp_release: Optional[str] = None
    runs: List[RunState] = field(default_factory=list)


def _status_path(testsuite_dir: Path) -> Path:
    return testsuite_dir / "status.yaml"


def read_status(testsuite_dir: Path) -> TestsuiteState:
    path = _status_path(testsuite_dir)
    with open(path) as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            raw = yaml.safe_load(f) or {}
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    runs = [RunState(**r) for r in (raw.get("runs") or [])]
    return TestsuiteState(
        testsuite_id=raw["testsuite_id"],
        status=raw["status"],
        created_at=raw["created_at"],
        cp_release=raw.get("cp_release"),
        runs=runs,
    )


def write_status(testsuite_dir: Path, state: TestsuiteState) -> None:
    path = _status_path(testsuite_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {
        "testsuite_id": state.testsuite_id,
        "status":       state.status,
        "created_at":   state.created_at,
        "cp_release":   state.cp_release,
        "runs":         [asdict(r) for r in state.runs],
    }
    _locked_write(path, data)


def update_run(testsuite_dir: Path, player_name: str, **kwargs: Any) -> None:
    """Atomically update fields on a single run entry."""
    path = _status_path(testsuite_dir)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            raw = yaml.safe_load(f) or {}
            for r in raw.get("runs") or []:
                if r.get("player_name") == player_name:
                    r.update(kwargs)
                    break
            f.seek(0)
            f.truncate()
            yaml.safe_dump(raw, f, default_flow_style=False)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def update_testsuite_status(testsuite_dir: Path, status: str) -> None:
    """Atomically update top-level testsuite status."""
    path = _status_path(testsuite_dir)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            raw = yaml.safe_load(f) or {}
            raw["status"] = status
            f.seek(0)
            f.truncate()
            yaml.safe_dump(raw, f, default_flow_style=False)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _locked_write(path: Path, data: Dict[str, Any]) -> None:
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            f.truncate()
            yaml.safe_dump(data, f, default_flow_style=False)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def scan_all_testsuites() -> List[Path]:
    """Return all testsuite dirs under the base that have a status.yaml."""
    if not CHARACTERIZATION_BASE.exists():
        return []
    return sorted(p.parent for p in CHARACTERIZATION_BASE.rglob("status.yaml"))


def compute_testsuite_status(runs: List[RunState]) -> str:
    """Derive aggregate testsuite status from its run statuses."""
    statuses = {r.status for r in runs}
    if not statuses:
        return TestsuiteStatus.INITIATED
    if all(r.status == RunStatus.SUCCESS for r in runs):
        return TestsuiteStatus.SUCCESS
    if all(r.status == RunStatus.TERMINATED for r in runs):
        return TestsuiteStatus.TERMINATED
    if RunStatus.FAILURE in statuses and any(
        r.status in (RunStatus.SUCCESS, RunStatus.RUNNING) for r in runs
    ):
        return TestsuiteStatus.PARTIAL_FAILURE
    if any(r.status == RunStatus.RUNNING for r in runs):
        return TestsuiteStatus.RUNNING
    return TestsuiteStatus.INITIATED
