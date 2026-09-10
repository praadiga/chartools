"""Parse top log files and compute percentile reports."""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# timestamp line written by collect_top.sh before each `top -b -n 1`
_TIMESTAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')

# %Cpu(s):  10.2 us,  2.3 sy,  0.0 ni, 85.3 id, ...
_CPU_RE = re.compile(
    r'%Cpu\(s\):\s*([\d.]+)\s*us,\s*([\d.]+)\s*sy,\s*[\d.]+\s*ni,\s*([\d.]+)\s*id'
)

# process table line — columns: PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND
# RES is in KB by default
_PROC_RE = re.compile(
    r'^\s*\d+\s+\S+\s+\S+\s+[-\d]+\s+\S+\s+(\d+)\s+\S+\s+\S+\s+([\d.]+)\s'
)


class Sample:
    __slots__ = ("cpu_us", "cpu_sy", "cpu_id", "mem_kb")

    def __init__(self, cpu_us: float, cpu_sy: float, cpu_id: float, mem_kb: int):
        self.cpu_us = cpu_us
        self.cpu_sy = cpu_sy
        self.cpu_id = cpu_id
        self.mem_kb = mem_kb


def parse_top_log(log_path: Path) -> List[Sample]:
    """
    Parse a top log file produced by collect_top.sh.

    The file looks like:
        2026-09-01T10:00:00Z
        top - 10:00:00 ...
        %Cpu(s): 10.2 us, 2.3 sy, ...
        ...process rows...
        2026-09-01T10:00:05Z
        ...

    Returns one Sample per timestamp block.
    """
    if not log_path.exists():
        return []

    samples: List[Sample] = []
    in_block = False
    cpu_us = cpu_sy = cpu_id = 0.0
    mem_kb = 0

    def _flush() -> None:
        if in_block:
            samples.append(Sample(cpu_us, cpu_sy, cpu_id, mem_kb))

    with open(log_path, errors="replace") as f:
        for line in f:
            line = line.rstrip()
            if _TIMESTAMP_RE.match(line):
                _flush()
                in_block = True
                cpu_us = cpu_sy = cpu_id = 0.0
                mem_kb = 0
                continue

            if not in_block:
                continue

            m = _CPU_RE.search(line)
            if m:
                cpu_us = float(m.group(1))
                cpu_sy = float(m.group(2))
                cpu_id = float(m.group(3))
                continue

            m = _PROC_RE.match(line)
            if m:
                mem_kb += int(m.group(1))

    _flush()
    return samples


# ---------------------------------------------------------------------------
# Percentile calculation (no numpy)
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (idx - lo) * (sorted_vals[hi] - sorted_vals[lo])


def _stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"min": 0.0, "max": 0.0, "avg": 0.0, "p99": 0.0, "p999": 0.0, "p9999": 0.0}
    s = sorted(values)
    return {
        "min":   round(s[0], 3),
        "max":   round(s[-1], 3),
        "avg":   round(sum(s) / len(s), 3),
        "p99":   round(_percentile(s, 99.0), 3),
        "p999":  round(_percentile(s, 99.9), 3),
        "p9999": round(_percentile(s, 99.99), 3),
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def compute_container_stats(samples: List[Sample]) -> Dict[str, Any]:
    return {
        "cpu_us_pct": _stats([s.cpu_us for s in samples]),
        "cpu_sy_pct": _stats([s.cpu_sy for s in samples]),
        "cpu_id_pct": _stats([s.cpu_id for s in samples]),
        "mem_kb":     _stats([float(s.mem_kb) for s in samples]),
    }


def write_run_report(
    report_dir: Path,
    player_name: str,
    testsuite_id: str,
    testcase_name: str,
    nodetaint: str,
    cp_release: Optional[str],
    num_days: int,
    poll_interval_seconds: int,
    crash_events: int,
    container_log_paths: Dict[str, Path],  # container_name → top log path
) -> Path:
    """
    Parse all container logs and write report/<player_name>.json.
    Returns the report path.
    """
    report_dir.mkdir(parents=True, exist_ok=True)

    containers: Dict[str, Any] = {}
    total_samples = 0
    for container_name, log_path in container_log_paths.items():
        samples = parse_top_log(log_path)
        containers[container_name] = compute_container_stats(samples)
        total_samples = max(total_samples, len(samples))

    report = {
        "testsuite":            testsuite_id,
        "testcase":             testcase_name,
        "player_name":          player_name,
        "nodetaint":            nodetaint,
        "cp_release":           cp_release,
        "num_days":             num_days,
        "poll_interval_seconds": poll_interval_seconds,
        "total_samples":        total_samples,
        "crash_events":         crash_events,
        "containers":           containers,
    }

    out_path = report_dir / "report.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    return out_path


def write_summary(
    report_dir: Path,
    testsuite_id: str,
    nodetaint: str,
    cp_release: Optional[str],
    generated_at: str,
    run_reports: List[Dict[str, Any]],
) -> Path:
    """
    Write report/summary.yaml from a list of loaded run report dicts.
    Each entry in run_reports is the JSON from write_run_report.
    """
    results = []
    for r in run_reports:
        containers = r.get("containers", {})
        player1 = containers.get("player1", {})
        cpu_avg = player1.get("cpu_us_pct", {}).get("avg", 0.0)
        cpu_p99 = player1.get("cpu_us_pct", {}).get("p99", 0.0)
        mem_avg = player1.get("mem_kb", {}).get("avg", 0.0)
        mem_p99 = player1.get("mem_kb", {}).get("p99", 0.0)
        results.append({
            "testcase":         r.get("testcase"),
            "player_name":      r.get("player_name"),
            "crash_events":     r.get("crash_events", 0),
            "player1_cpu_avg":  cpu_avg,
            "player1_cpu_p99":  cpu_p99,
            "player1_mem_avg_kb": mem_avg,
            "player1_mem_p99_kb": mem_p99,
        })

    summary = {
        "testsuite":    testsuite_id,
        "nodetaint":    nodetaint,
        "cp_release":   cp_release,
        "generated_at": generated_at,
        "results":      results,
    }
    out_path = report_dir / "summary.yaml"
    with open(out_path, "w") as f:
        yaml.safe_dump(summary, f, default_flow_style=False)
    return out_path
