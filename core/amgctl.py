"""amgctl binary management and all amgctl command wrappers."""

import getpass
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

AMGCTL_BIN    = Path(
    os.environ.get("CHARTOOLS_AMGCTL",
                   os.path.expanduser("~/bin/amgctl"))
)
_S3_BASE      = "s3://iota-non-prod-artifacts/ieg-core_services"
_ANSI_RE      = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Strings to detect in amgctl logs output
LOG_PR_CREATED     = "PR created in Github, PR number"
LOG_ALREADY_EXISTS = "already exist in cloud"
LOG_NO_CHANGE      = "No changes detected to commit"
LOG_FATAL          = "FATAL"


class DeployResult:
    PR_CREATED     = "PR_CREATED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    NO_CHANGE      = "NO_CHANGE"
    FATAL          = "FATAL"
    TIMEOUT        = "TIMEOUT"


class AmgctlError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import logging as _logging
_log = _logging.getLogger(__name__)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _run(cmd: List[str], env: Optional[Dict] = None, timeout: int = 120) -> subprocess.CompletedProcess:
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=merged,
        timeout=timeout,
    )


_SEP = "-" * 60

def _amgctl(*args: str, env: Optional[Dict] = None, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd_str = "amgctl " + " ".join(args)
    _log.info("%s\nRunning: %s", _SEP, cmd_str)
    result = _run([str(AMGCTL_BIN)] + list(args), env=env, timeout=timeout)
    output = strip_ansi(result.stdout + result.stderr).strip()
    _log.info("Output:\n%s\n%s", output or "(empty)", _SEP)
    return result


# ---------------------------------------------------------------------------
# Version check + download
# ---------------------------------------------------------------------------

def _parse_versions(output: str) -> Tuple[Optional[str], Optional[str]]:
    api = re.search(r"API Version:\s*([0-9.]+)", output)
    cli = re.search(r"CLI Version:\s*([0-9.]+)", output)
    if not api:
        api = re.search(r"api_version=([0-9.]+)", output)
    if not cli:
        cli = re.search(r"cli_version=([0-9.]+)", output)
    return (api.group(1) if api else None), (cli.group(1) if cli else None)


def _prompt_aws_creds() -> Dict[str, str]:
    # if creds are already in the environment, use them silently
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        print("Using AWS credentials from environment.")
        return {}   # _run() merges os.environ, so nothing extra needed

    print("\nAWS credentials required to download amgctl.")
    print("Paste your export lines below, then press Enter twice:")
    print('  export AWS_ACCESS_KEY_ID="..."')
    print('  export AWS_SECRET_ACCESS_KEY="..."')
    print('  export AWS_SESSION_TOKEN="..."')
    print()

    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "" and lines:
            break
        lines.append(line)

    env: Dict[str, str] = {}
    _wanted = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
               "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION"}
    for line in lines:
        m = re.match(r'^\s*(?:export\s+)?([A-Z_]+)=(["\']?)(.*?)\2\s*$', line)
        if m and m.group(1) in _wanted:
            env[m.group(1)] = m.group(3)

    if not env.get("AWS_ACCESS_KEY_ID"):
        raise AmgctlError("No AWS_ACCESS_KEY_ID found in pasted credentials.")
    if "AWS_DEFAULT_REGION" not in env:
        env["AWS_DEFAULT_REGION"] = "us-east-1"
    return env


def _download_amgctl(api_version: str, aws_env: Dict[str, str]) -> None:
    s3_key = f"{_S3_BASE}/{api_version}/binaries/amgctl_Linux_x86_64.tar.gz"
    print(f"Downloading amgctl {api_version} from S3...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tar_path = os.path.join(tmpdir, "amgctl.tar.gz")
        res = _run(["aws", "s3", "cp", s3_key, tar_path], env=aws_env, timeout=180)
        if res.returncode != 0:
            raise AmgctlError(f"S3 download failed: {res.stderr.strip()}")
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(tmpdir)
        extracted = os.path.join(tmpdir, "amgctl")
        if not os.path.exists(extracted):
            raise AmgctlError("amgctl binary not found in downloaded archive")
        AMGCTL_BIN.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(extracted, str(AMGCTL_BIN))
        AMGCTL_BIN.chmod(0o755)
    print(f"amgctl installed at {AMGCTL_BIN}")


def ensure_amgctl() -> None:
    """
    Verify amgctl binary is installed. Re-downloads from S3 only when the binary
    is missing or there is a real CLI/API version mismatch.
    When amgctl needs 'amgctl configure' (binary exists but config is absent),
    that is treated as installed — the caller must run configure separately.
    """
    if AMGCTL_BIN.exists():
        res = _amgctl("version")
        output = strip_ansi(res.stdout + res.stderr)
        # Binary present but amgctl configure hasn't been run yet.
        # "failed to load config" / "please run command amgctl configure" appears
        # in stderr — this is not a version mismatch; the binary is fine.
        if "failed to load config" in output or \
                "please run command amgctl configure" in output:
            print(f"amgctl binary found at {AMGCTL_BIN}")
            print("NOTE: run 'amgctl configure' before deploying.")
            return
        api_ver, cli_ver = _parse_versions(output)
        if api_ver and cli_ver and api_ver == cli_ver:
            print(f"amgctl ready: version {api_ver}")
            return
        # Real mismatch — re-download to align CLI with API version.
        print(f"amgctl version mismatch — CLI={cli_ver} API={api_ver}, re-downloading.")
        aws_env = _prompt_aws_creds()
        dl_version = api_ver or cli_ver
    else:
        print("amgctl binary not found.")
        aws_env = _prompt_aws_creds()
        dl_version = os.environ.get("CHARTOOLS_AMGCTL_VERSION", "1.6.4")

    _download_amgctl(dl_version, aws_env)

    res = _amgctl("version")
    output = strip_ansi(res.stdout + res.stderr)
    api_ver, cli_ver = _parse_versions(output)
    if api_ver and cli_ver and api_ver == cli_ver:
        print(f"amgctl ready: version {api_ver}")
    else:
        # Version output format didn't match our regex — binary is installed,
        # log the raw output and continue rather than blocking the daemon.
        print(f"amgctl installed (version output: {output.strip()[:120]})")
        print("Continuing — if amgctl commands fail, check the binary manually.")


# ---------------------------------------------------------------------------
# playout list → headend allocation + cp_release
# ---------------------------------------------------------------------------

def _parse_list_output(raw: str) -> List[Dict]:
    clean = strip_ansi(raw).strip()
    # try full JSON array first
    try:
        result = json.loads(clean)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    # fall back to newline-delimited JSON objects
    entries = []
    for line in clean.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def list_playout(namespace: str, headend: str) -> List[Dict]:
    """Return all amgctl list entries for namespace_headend_* using server-side filter."""
    # naming: namespace_headend_feed  e.g. hypeus2_661_020
    prefix = f"{namespace}_{headend}"
    filter_cue = (
        f'[for d in deployments if strings.HasPrefix(d.name,"{prefix}") {{d}}]'
    )
    res = _amgctl("cp", "app", "playout", "list",
                  f"--filter-cue={filter_cue}", timeout=60)
    # INFO/auth lines go to stderr — parse stdout only to avoid corrupting JSON
    entries = _parse_list_output(res.stdout)
    if not entries:
        _log.warning("list_playout: no entries parsed for prefix %s; stdout=%r",
                     prefix, strip_ansi(res.stdout)[:200])
    return entries


def allocate_feed(namespace: str, headend: str) -> str:
    """Return the lowest free 3-digit feed number for namespace_headend_*."""
    entries = list_playout(namespace, headend)
    used: set = set()
    for e in entries:
        parts = str(e.get("name", "")).split("_")
        if len(parts) == 3:
            try:
                used.add(int(parts[2]))
            except ValueError:
                pass
    existing_names = sorted(str(e.get("name", "")) for e in entries)
    _log.info("Existing players for %s_%s: %s", namespace, headend,
              existing_names if existing_names else "(none found)")
    i = 1
    while i in used:
        i += 1
    chosen = f"{i:03d}"
    _log.info("Allocating feed %s (used feed numbers: %s)", chosen,
              sorted(used) if used else "(none)")
    return chosen


# keep old name as alias so existing callers don't break
allocate_headend = allocate_feed


def get_cp_release(namespace: str, headend: str, ref_feed: str) -> Optional[str]:
    """Return cp_release for the reference player from amgctl list."""
    entries = list_playout(namespace, headend)
    ref_name = f"{namespace}_{headend}_{ref_feed}"
    for e in entries:
        if e.get("name") == ref_name:
            return e.get("release")
    return None


# ---------------------------------------------------------------------------
# amgctl playout commands
# ---------------------------------------------------------------------------

def playout_get(player_name: str, export_dir: Path) -> str:
    """Export a player's amgctl config files. Pre-deletes export_dir if it exists."""
    if export_dir.exists():
        shutil.rmtree(export_dir)
    res = _amgctl("cp", "app", "playout", "get", "-n", player_name, "-e", str(export_dir), timeout=120)
    output = strip_ansi(res.stdout + res.stderr)
    if res.returncode != 0 and LOG_FATAL in output:
        raise AmgctlError(f"amgctl get failed for {player_name}: {output.strip()}")
    return output


def extract_cp_release(get_output: str, export_dir: Path) -> Optional[str]:
    """
    Extract cp_release (e.g. 'cp_4.22.0.0') from amgctl get output text or
    the exported coreservice.yaml.  Returns None if not found.
    """
    import yaml as _yaml

    # Try command output text first — amgctl often prints the release inline
    m = re.search(r'(cp_[\d.]+)', get_output)
    if m:
        return m.group(1)

    # Try coreservice.yaml top-level 'release' field
    for fname in ("coreservice.yaml", "coreservice.yml"):
        cs_path = export_dir / fname
        if cs_path.exists():
            try:
                with open(cs_path) as f:
                    data = _yaml.safe_load(f) or {}
                rel = data.get("release")
                if rel:
                    return str(rel)
            except Exception:
                pass

    return None


def playout_create_dryrun(cp_release: str, player_dir: Path) -> subprocess.CompletedProcess:
    """Create PR (dry-run). Must be called before playout_create."""
    return _amgctl("cp", "app", "playout", "create",
                   "-r", cp_release, "-i", str(player_dir), "-q", "--dry-run", timeout=300)


def playout_create(cp_release: str, player_dir: Path) -> subprocess.CompletedProcess:
    """Merge the PR created by playout_create_dryrun and submit deploy job."""
    return _amgctl("cp", "app", "playout", "create",
                   "-r", cp_release, "-i", str(player_dir), "-q", timeout=300)


def playout_destroy(player_name: str) -> subprocess.CompletedProcess:
    return _amgctl("cp", "app", "playout", "destroy", "-n", player_name, "-q", timeout=300)


# ---------------------------------------------------------------------------
# Log polling
# ---------------------------------------------------------------------------

def poll_playout_logs(
    player_name: str,
    timeout_seconds: int = 1200,
) -> Tuple[str, str]:
    """
    Stream `amgctl cp app playout logs -n <player>` and scan for terminal
    conditions.  The command streams indefinitely — it must NOT be called
    with a short subprocess timeout; instead we Popen it and read line-by-line
    until we see a terminal string or the overall deadline is reached.

    Returns (DeployResult constant, full log text).
    """
    import select as _select

    deadline = time.time() + timeout_seconds
    full_log = ""

    _log.info("%s\nRunning: amgctl cp app playout logs -n %s (streaming)", _SEP, player_name)

    proc = subprocess.Popen(
        [str(AMGCTL_BIN), "cp", "app", "playout", "logs", "-n", player_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        env={**os.environ},
    )

    def _check(text: str) -> Optional[str]:
        if LOG_PR_CREATED     in text: return DeployResult.PR_CREATED
        if LOG_ALREADY_EXISTS in text: return DeployResult.ALREADY_EXISTS
        if LOG_NO_CHANGE      in text: return DeployResult.NO_CHANGE
        if LOG_FATAL          in text: return DeployResult.FATAL
        return None

    try:
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            ready, _, _ = _select.select([proc.stdout], [], [], min(10.0, remaining))
            if ready:
                line = proc.stdout.readline()
                if not line:        # EOF — process exited
                    break
                full_log += line
                clean_line = strip_ansi(line).rstrip()
                if clean_line:
                    _log.info("[logs:%s] %s", player_name, clean_line)
                result = _check(strip_ansi(full_log))
                if result:
                    _log.info("Terminal condition for %s: %s\n%s", player_name, result, _SEP)
                    return result, strip_ansi(full_log)
            elif proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # Drain any remaining buffered output after the process exits
    try:
        tail, _ = proc.communicate(timeout=5)
        if tail:
            full_log += tail
    except Exception:
        pass

    clean = strip_ansi(full_log)
    return _check(clean) or DeployResult.TIMEOUT, clean
