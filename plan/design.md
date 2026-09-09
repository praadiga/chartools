# Characterization Tool — Design Document

## Goal

A filesystem + daemon-based tool to benchmark CloudPort playout players across different
AWS instance types. The user SSHes into the server, creates a testsuite folder with a
`config.yaml`, and manually triggers the daemon to deploy and start collecting — metrics,
termination, and reports all stay on the server filesystem.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Local Laptop                                               │
│  ssh user@server                                            │
└─────────────────────┬───────────────────────────────────────┘
                      │ SSH
┌─────────────────────▼───────────────────────────────────────┐
│  Remote Server                                              │
│                                                             │
│  chartools daemon  (always-running background process)      │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Manually triggered via CLI                          │   │
│  │  `chartools testsuite add <dir>` → registers it      │   │
│  │  → deploy + collect tasks start immediately          │   │
│  └──────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Polling threads (one per running testcase run)      │   │
│  │  → kubectl exec top -b -n 1 every X seconds         │   │
│  │  → handles pod/container crashes, resumes on restart │   │
│  └──────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Termination scheduler                               │   │
│  │  → checks termination_date per run every 5 min      │   │
│  │  → generates report, destroys playout               │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  Filesystem  /home/amagi/characterization/                  │
│  ├── ts_c7a_20260901/    ← testsuite folders                │
│  ├── ts_c8a_20260910/                                       │
│  └── ...          (all data stays here, no S3)             │
│                                                             │
│  Tools: amgctl, kubectl, kubeconfig.yaml                   │
└─────────────────────────────────────────────────────────────┘
                      │ kubectl
         ┌────────────▼──────────────┐
         │  K8s Cluster              │
         │  player-* pods            │
         │  containers monitored:    │
         │    player1                │
         │    .*tardis.*             │
         │    .*vanxio.*             │
         └───────────────────────────────┘
          all metrics + logs stay on server
          filesystem inside testsuite dir
          (no S3)
```

---

## amgctl Management

### Binary path

chartools uses a fixed path: `/home/amagi/bin/amgctl`

This avoids the `./amgctl` working-directory dependency in the original script. The
directory is created on first download if it doesn't exist.

### Version check + download flow

`cp_release` and amgctl versions are **independent**:
- `cp_release` (e.g. `cp_4.18.2.5`) is the CloudPort platform release — used only for
  `amgctl cp app playout create -r <cp_release>`. Derived from the reference player, not config.
- amgctl `CLI version` vs `API version` (e.g. `1.6.4`) are amgctl-internal versions that
  must match each other. The S3 download key uses the API version.

Runs **once at daemon start**.

```
1. Check if /home/amagi/bin/amgctl exists:
     NOT FOUND  → go to step 3 (prompt for creds + download)
     FOUND      → run `/home/amagi/bin/amgctl version`
                  parse CLI version and API version from output
                  if CLI version == API version → proceed, done
                  if MISMATCH → go to step 3 (prompt for creds + download)

2. (done — versions match)

3. Prompt user for AWS credentials:
     "amgctl binary not found / CLI version mismatch. AWS credentials needed to download."
     "AWS Access Key ID: "         (input, hidden)
     "AWS Secret Access Key: "     (input, hidden)
     "AWS Session Token (blank if none): "
     "AWS Region [us-east-1]: "
     → set as env vars for this session only, never written to disk

4. Download using the API version from step 1 (or latest if binary was missing):
     AWS_ACCESS_KEY_ID=... aws s3 cp \
       s3://iota-non-prod-artifacts/ieg-core_services/<api_version>/binaries/amgctl_Linux_x86_64.tar.gz \
       /tmp/amgctl_Linux_x86_64.tar.gz

5. Extract and install:
     tar -xvzf /tmp/amgctl_Linux_x86_64.tar.gz -C /tmp/
     mkdir -p /home/amagi/bin/
     mv /tmp/amgctl /home/amagi/bin/amgctl
     chmod +x /home/amagi/bin/amgctl

6. Verify:
     /home/amagi/bin/amgctl version → confirm CLI == API version, else abort
```

### cp_release derivation

`cp_release` in `config.yaml` is **optional**. If set, it is used as-is. If omitted,
it is derived automatically after `amgctl cp app playout get` — the value is logged in
the `amgctl get` output and also visible in `amgctl cp app playout list` for the
reference player. The derived value is used for all testcase deploys in that testsuite
and written to `status.yaml` so it is visible in `chartools status`.

### Project structure addition

`core/amgctl.py` — wraps all amgctl commands and owns the version check + download logic.
Ported and cleaned up from `scripts/new_amgctl_upgrade_playout.py`
(`install_latest_amgctl`, `copy_amgctl_using_aws_profile`).

---

## Config File — `config.yaml`

One YAML file per testsuite. User creates the folder and this file, then manually
triggers the daemon with `chartools testsuite add <dir>`.

`reference_player` is optional. If omitted, the user places amgctl files
(`topology.yaml`, `coreservice.yaml`, `chart.yaml`) directly into the player subfolders
before running, and the deploy step skips `amgctl get` and only applies the overrides.

```yaml
# testsuite-level settings
nodetaint: player-characterisation-c7axlarge   # maps to params.player.deployment_config.nodeTaintName
reference_player: hypeus2_661_020              # full player name (namespace_feedid_headend) — optional, omit if placing amgctl files manually
cp_release: cp_4.18.2.5                        # optional — if omitted, derived from reference player via amgctl list

testcases:
  - name: 1080i50_16x9
    num_days: 7
    poll_interval_seconds: 5
    license_key: XXXX-YYYY-1111
    overrides:
      params.common.enableTopologyCpuOptimization: false
      params.player.ops_config.business_config.BlipFeedResolution: "1920x1080i50"
      params.player.ops_config.business_config.OutputAR: "16:9"

  - name: 720p50_16x9
    num_days: 5
    poll_interval_seconds: 10
    license_key: XXXX-YYYY-2222
    overrides:
      params.common.enableTopologyCpuOptimization: false
      params.player.ops_config.business_config.BlipFeedResolution: "1280x720p50"
      params.player.ops_config.business_config.OutputAR: "16:9"
```

---

## Filesystem Layout

```
/home/amagi/characterization/
└── ts_c7a_20260901/                        ← testsuite folder (user creates this)
    │
    ├── config.yaml                         ← user-written config (input)
    ├── status.yaml                         ← daemon-written status (do not edit)
    │
    ├── sigmalh_661_003/                    ← amgctl working folder per testcase
    │   ├── topology.yaml                   ← patched topology
    │   ├── coreservice.yaml                ← generated with hooks.license_handler
    │   └── chart.yaml
    │
    ├── sigmalh_661_004/
    │   ├── topology.yaml
    │   ├── coreservice.yaml
    │   └── chart.yaml
    │
    ├── logs/
    │   ├── sigmalh_661_003/
    │   │   ├── deploy.log                  ← amgctl playout logs at create time
    │   │   ├── destroy.log                 ← amgctl playout logs at destroy time
    │   │   ├── events.log                  ← crash events, restarts, gaps
    │   │   ├── top_player1.log             ← raw top output for player1
    │   │   ├── top_tardis1.log             ← raw top output for tardis container
    │   │   └── top_vanxio1.log             ← raw top output for vanxio container
    │   └── sigmalh_661_004/
    │       └── ...
    │
    └── report/
        ├── sigmalh_661_003.json            ← per-run report with percentiles
        ├── sigmalh_661_004.json
        └── summary.yaml                   ← cross-testcase comparison
```

---

## `status.yaml` — Daemon-written state file

The daemon writes and updates this file. It is the source of truth for run state.
Never edit by hand.

Multiple threads write to this file (deploy thread, sync threads, scheduler thread).
All writes go through `core/status.py` which holds an `fcntl.flock` exclusive lock for
the duration of each read-modify-write cycle. `chartools status` (read-only) also uses
a shared lock so it never reads a half-written file.

```yaml
testsuite_id: ts_c7a_20260901
status: RUNNING            # INITIATED | RUNNING | PARTIAL_FAILURE | SUCCESS | TERMINATED
created_at: "2026-09-01T10:00:00Z"

runs:
  - testcase: 1080i50_16x9
    player_name: sigmalh_661_003
    headend_id: "003"
    status: RUNNING        # INITIATED | DEPLOYING | PENDING | RUNNING | FAILURE | SUCCESS | TERMINATED
    started_at: "2026-09-01T10:05:00Z"
    terminates_at: "2026-09-08T10:05:00Z"
    samples_collected: 12540
    crash_events: 2        # number of pod/container crashes detected during collection
    error_msg: null

  - testcase: 720p50_16x9
    player_name: sigmalh_661_004
    headend_id: "004"
    status: DEPLOYING
    started_at: null
    terminates_at: null
    samples_collected: 0
    crash_events: 0
    error_msg: null
```

---

## CLI Commands

```bash
# Daemon management
chartools daemon start                 # start daemon in background (writes PID to ~/.chartools/daemon.pid)
chartools daemon stop                  # gracefully stop daemon
chartools daemon status                # is daemon running?

# Testsuite management
chartools testsuite add <dir>          # manually register a testsuite with the daemon → starts deploy immediately
chartools status                       # list all testsuites the daemon is managing
chartools status <dir>                 # detailed status for one testsuite (reads status.yaml)
chartools report <dir>                 # print summary table from report/summary.yaml
chartools terminate <dir>              # force-terminate all running runs in a testsuite
chartools retry <dir>                  # retry all FAILURE runs in a testsuite (skips file generation, goes to review gate)
```

### `chartools status` output (all testsuites)

```
TESTSUITE              STATUS    CREATED      TESTCASES   RUNNING  SUCCESS  FAILED
ts_c7a_20260901        RUNNING   2026-09-01   2           1        0        0
ts_c8a_20260910        SUCCESS   2026-09-10   3           0        3        0
```

### `chartools status <dir>` output (one testsuite)

```
TESTSUITE: ts_c7a_20260901  STATUS: RUNNING  NODETAINT: player-characterisation-c7axlarge

TESTCASE         PLAYER           STATUS     SAMPLES   CRASHES   TERMINATES
1080i50_16x9     sigmalh_661_003  RUNNING    12540     2         2026-09-08
720p50_16x9      sigmalh_661_004  DEPLOYING  0         0         -
```

---

## Daemon Lifecycle

### 1. Manual trigger

User runs `chartools testsuite add <dir>` which signals the daemon via a **Unix domain
socket** at `~/.chartools/daemon.sock`. The foreground CLI sends a JSON message
`{"action": "add", "dir": "<abs_path>"}` and waits for `{"ok": true}` or an error.
The daemon's socket listener thread enqueues the testsuite dir for the deploy loop.

The daemon does NOT watch folders automatically — every testsuite must be explicitly added.

On daemon restart, it resumes any testsuites where `status.yaml` exists with
status=RUNNING or DEPLOYING by scanning `/home/amagi/characterization/` for subdirectories
containing a `status.yaml`. No external registry file — the filesystem is the registry.

### 2. Deploy flow (serial per testsuite, one testcase at a time)

Only the `reference_player is set` path is implemented in v1. The manual-files path
will be designed and added later.

```
For each testcase in config.yaml:
  1.  Write run to status.yaml with status=DEPLOYING

  2.  Parse reference_player from config.yaml (e.g. "hypeus2_661_020"):
        → split on "_": namespace="hypeus2", feed_id="661", ref_headend="020"

      amgctl cp app playout list
        → strip ANSI colour codes from output
        → parse all "name" fields matching hypeus2_661_* pattern
        → collect used headend_ids as integers (including ref_headend): {1, 3, 4, 20, ...}
        → find lowest free: iterate 1,2,3... pick first NOT in used set
          e.g. used={1,3,4,5,6,7,8,9,11,12,16,20,30,900,911} → free=002
        → player_name = f"{namespace}_{feed_id}_{headend_id:03d}"

        ALSO parse cp_release if not set in config.yaml:
        → find entry with "name":"hypeus2_661_020", read its "release":"cp_4.22.0.0"
        → use this as cp_release for all testcase deploys and write to status.yaml

  3.  if os.path.exists(<testsuite_dir>/<player_name>/):
          shutil.rmtree(<testsuite_dir>/<player_name>/)
          # amgctl get fails with FATAL if export dir already exists

  4.  amgctl cp app playout get -n <reference_player> -e <testsuite_dir>/<player_name>/
        → stdout also contains "release":"cp_4.22.0.0" (backup source for cp_release)

  5.  generate/update coreservice.yaml:
        NOTE: actual format from amgctl get (sample_amgclt/coreservice.yaml) is simpler
        than originally designed — dependencies have only "name", no type/product/cloud.
        Also includes last_applied_migration and last_state from the exported file.
        The tool OVERWRITES only these fields, preserving everything else amgctl exported:
          - dependencies.plsh.name: <namespace>@<feed_id>   (already correct from get)
          - hooks.license_handler.parameters.LICENSE_KEY: <testcase.license_key>
          - (do NOT regenerate the full file — patch the exported one)
  6.  patch topology.yaml:
        - apply testcase.overrides
        - set nodeTaintName: <config.nodetaint>
        - set enableTopologyCpuOptimization: false
        - set headendId, DDFeedDisplayTitle

  *** REVIEW GATE ***
  7.  Print:
        "Files ready for testcase '<name>' at <testsuite_dir>/<player_name>/"
        "  topology.yaml    → <testsuite_dir>/<player_name>/topology.yaml"
        "  coreservice.yaml → <testsuite_dir>/<player_name>/coreservice.yaml"
        "Edit files in another terminal if needed."
        "Options: [Enter] deploy now  |  'wait' pause indefinitely  |  'skip' cancel"
        "Auto-deploying in 5 minutes..."

      Timeout behaviour:
        - 5 min with no input  → proceed with deployment (same as Enter)
        - user types "wait"    → switches to indefinite block; prints "Waiting... press Enter when done editing."
                                  blocks until user presses Enter, then deploys
        - user presses Enter   → deploy immediately
        - user types "skip"    → set status=CANCELLED, move to next testcase

  8.  amgctl cp app playout create -r <cp_release> -i <player_name>/ -q
      → capture logs → save to logs/<player_name>/deploy.log
      → poll `amgctl cp app playout logs -n <player_name>` until "PR created" or FATAL
      → on FATAL: set status=FAILURE, write error_msg, move to next testcase
  9.  poll kubectl get pod until Running (20min timeout):
        pod_name = f"player-{namespace}-{feed_id}-{headend_id:03d}-player-0"
        kubectl_namespace = f"{namespace}-playout"
        e.g. pod "player-hypeus2-661-002-player-0" in namespace "hypeus2-playout"
        kubectl --kubeconfig <path> -n <kubectl_namespace> get pod <pod_name>
      → on timeout: set status=FAILURE
  10. set terminates_at = now + testcase.num_days
      update run status → RUNNING
  11. inject collect_top.sh into each monitored container (player1, .*tardis.*, .*vanxio.*)
      → script runs in background inside container, writes to /mnt/top_<container>.log
      → start server sync thread for this run (periodically kubectl cp logs to server)
  12. move to next testcase
```

### 2b. Retry flow — `chartools retry <dir>`

For runs that ended in status=FAILURE (deploy failed or pod timed out), the user can:
1. Check `logs/<player_name>/deploy.log` to understand the failure
2. Edit `<testsuite_dir>/<player_name>/topology.yaml` or `coreservice.yaml` manually
3. Run `chartools retry <dir>` to re-attempt deployment

The retry flow **skips** steps 1–6 (amgctl get + file generation) since the files already
exist and the user may have edited them. It picks up from the review gate:

```
For each run in status.yaml where status=FAILURE:
  Print: "Retrying testcase '<name>' — player_name: <player_name>"
         "Files at <testsuite_dir>/<player_name>/ (edit if needed before proceeding)"
  → REVIEW GATE (same as step 7 above — 5 min timeout, 'wait', 'skip' options)

  → amgctl cp app playout create -r <cp_release> -i <player_name>/ -q
    (same deploy steps 8–12 as normal flow)
```

If the previous failed attempt created a partial playout, the retry uses `create` not
`update` — amgctl will return an "already exists" error in that case, which the user
must clear manually by destroying first (`amgctl cp app playout destroy -n <player_name>`).
The deploy log will make this clear.

---

### 3. Metrics collection — hybrid container + server sync

**Two responsibilities, separated:**

**A. Container side — `collect_top.sh` (injected once after pod is Running)**

Runs as a background process inside each monitored container. Writes continuously to
`/mnt/top_<container>.log`. Keeps collecting even when the daemon is down.

```bash
#!/bin/sh
CONTAINER=$1
while true; do
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> /mnt/top_${CONTAINER}.log
  top -b -n 1 >> /mnt/top_${CONTAINER}.log
  sleep <poll_interval_seconds>
done &
echo $! > /mnt/top_${CONTAINER}.pid
```

**B. Server side — sync thread (one per monitored container per run)**

Periodically `kubectl cp`s the log file from the container to the server. Acts as a
rolling backup — daemon downtime causes no data loss because the container kept writing.

```python
SYNC_INTERVAL = 60  # seconds — sync frequency independent of poll_interval

while not past_termination_date:
    for container in monitored_containers_for_run:
        try:
            kubectl_cp(pod, container,
                       src=f"/mnt/top_{container}.log",
                       dst=f"logs/{player_name}/top_{container}.log")
            status.samples_collected = count_samples(f"logs/{player_name}/top_{container}.log")
        except (PodNotRunning, ContainerNotRunning):
            log_event(f"logs/{player_name}/events.log", "CRASH", container)
            status.crash_events += 1
            wait_for_pod_running(timeout=1200)
            re_inject_collect_top(pod, container)   # re-inject script after pod recovers
            log_event(f"logs/{player_name}/events.log", "RECOVERED", container)
    sleep(SYNC_INTERVAL)
```

**On daemon restart:** daemon reads `status.yaml`, finds RUNNING runs, re-attaches sync
threads for each. Container has been collecting uninterrupted — server just resumes syncing.

**Top data captured per sample:**
- Timestamp prefix per sample block
- `%Cpu(s)` summary line: `us` (user), `sy` (system), `id` (idle), `wa` (iowait)
- Per-process rows matching monitored containers: `%CPU`, `RES` (resident mem KB)

### 4. Termination flow (scheduler checks every 5 min)

```
For each run where terminates_at <= now AND status=RUNNING:
  1.  Stop polling thread for this run
  2.  Parse top_<container>.log for each container:
        → compute min/max/avg/p99/p999/p9999 for: cpu_us%, cpu_sy%, cpu_id%, mem_kb
        → count crash_events from events.log
  3.  Write report/< player_name>.json
  4.  amgctl cp app playout destroy -n <player_name> -q
      → poll logs until destroy PR merges
      → save to logs/<player_name>/destroy.log
  5.  Update run status → SUCCESS
  6.  If all runs done: write report/summary.yaml, update testsuite status → SUCCESS
      (everything stays on server filesystem — no S3)
```

---

## Report Format

### `report/<player_name>.json`

```json
{
  "testsuite": "ts_c7a_20260901",
  "testcase": "1080i50_16x9",
  "player_name": "sigmalh_661_003",
  "nodetaint": "player-characterisation-c7axlarge",
  "cp_release": "cp_4.18.2.5",
  "num_days": 7,
  "poll_interval_seconds": 5,
  "total_samples": 120960,
  "crash_events": 2,
  "containers": {
    "player1": {
      "cpu_us_pct":  { "min": 10.2, "max": 85.3, "avg": 42.1, "p99": 80.0, "p999": 84.0, "p9999": 85.2 },
      "cpu_sy_pct":  { "min": 1.0,  "max": 12.4, "avg": 4.3,  "p99": 11.0, "p999": 12.0, "p9999": 12.3 },
      "cpu_id_pct":  { "min": 2.3,  "max": 88.8, "avg": 53.6, "p99": 87.0, "p999": 88.0, "p9999": 88.7 },
      "mem_kb":      { "min": 204800, "max": 512000, "avg": 350000, "p99": 490000, "p999": 505000, "p9999": 511000 }
    },
    "tardis1": {
      "cpu_us_pct":  { "min": 1.0, "max": 20.0, "avg": 8.0, "p99": 18.0, "p999": 19.5, "p9999": 20.0 },
      "mem_kb":      { "min": 51200, "max": 102400, "avg": 71000, "p99": 98000, "p999": 101000, "p9999": 102000 }
    },
    "vanxio1": {
      "cpu_us_pct":  { "min": 5.0, "max": 60.0, "avg": 25.0, "p99": 55.0, "p999": 58.0, "p9999": 59.5 },
      "mem_kb":      { "min": 102400, "max": 307200, "avg": 180000, "p99": 290000, "p999": 305000, "p9999": 307000 }
    }
  }
}
```

### `report/summary.yaml` — cross-testcase comparison table

```yaml
testsuite: ts_c7a_20260901
nodetaint: player-characterisation-c7axlarge
cp_release: cp_4.18.2.5
generated_at: "2026-09-08T10:05:00Z"

results:
  - testcase: 1080i50_16x9
    player_name: sigmalh_661_003
    crash_events: 2
    player1_cpu_avg: 42.1
    player1_cpu_p99: 80.0
    player1_mem_avg_kb: 350000
    player1_mem_p99_kb: 490000

  - testcase: 720p50_16x9
    player_name: sigmalh_661_004
    crash_events: 0
    player1_cpu_avg: 28.5
    player1_cpu_p99: 61.0
    player1_mem_avg_kb: 245000
    player1_mem_p99_kb: 380000
```

---

## Project Structure

```
characterization-tool/
├── chartools.py                   ← CLI entrypoint (Typer)
│
├── daemon/
│   ├── runner.py                  ← main daemon loop (manual trigger handler + scheduler)
│   ├── deploy.py                  ← amgctl + kubectl deploy/destroy wrappers
│   ├── collect.py                 ← per-run polling thread (top collection)
│   └── terminate.py               ← termination + report generation flow
│
├── core/
│   ├── amgctl.py                  ← amgctl binary management (version check, download, all amgctl commands)
│   ├── config.py                  ← parse and validate config.yaml
│   ├── status.py                  ← read/write status.yaml
│   ├── topology.py                ← topology.yaml + coreservice.yaml generation
│   └── metrics.py                 ← top log parser + percentile calculation
│
├── scripts/
│   └── new_amgctl_upgrade_playout.py  ← existing, logic reused by deploy.py
│
└── plan/
    ├── plan.md
    ├── design.md
    └── playout _ amgctl.html
```

---

## Security Callout

No authentication in v1 — the tool runs on the server and is accessed via SSH.
Ensure the server is on a private/VPN-accessible network. The kubeconfig is expected
at `~/lh_upgrade/k8s_player/kubeconfig.yaml`. The CLI can override with `--kubeconfig`.

---

## Deferred — To Design Later

- `reference_player` omitted path: user places amgctl files manually before running.
  Need to decide how player subfolder is named and how headend_id is determined.

## Decisions

| # | Decision |
|---|---|
| 1 | "already exists" on retry — left to user to fix manually (`amgctl cp app playout destroy -n <player_name>` then retry). Deploy log will surface the error clearly. |
