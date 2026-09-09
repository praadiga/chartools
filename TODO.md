# Characterization Tool — Implementation TODO

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

---

## Phase 1 — Project Scaffolding

- [x] Create directory structure (`daemon/`, `core/`, `chartools.py`)
- [x] Add `pyproject.toml` / `requirements.txt` (typer, pyyaml, rich)
- [x] Wire up `chartools` CLI entrypoint with Typer

---

## Phase 2 — Core Modules

### `core/config.py`
- [x] Load and validate `config.yaml`
- [x] Enforce required fields: `nodetaint`, at least one testcase with `license_key`, `num_days`, `poll_interval_seconds`
- [x] Parse `reference_player` (split `hypeus2_661_020` → namespace, feed_id, headend)
- [x] Handle optional `cp_release` override

### `core/status.py`
- [x] Read `status.yaml` → dataclass
- [x] Write `status.yaml` with `fcntl.flock` exclusive lock
- [x] Shared-lock read path for `chartools status`
- [x] Helper: scan `/home/amagi/characterization/` for all testsuite dirs (for global `chartools status`)

### `core/amgctl.py`
- [x] Version check: `/home/amagi/bin/amgctl version` → parse CLI version + API version
- [x] Prompt for AWS creds if binary missing or version mismatch → set as env vars only
- [x] Download amgctl from S3 (`iota-non-prod-artifacts/ieg-core_services/<api_version>/binaries/`)
- [x] `amgctl cp app playout list` → strip ANSI → parse name + release fields
- [x] Headend allocation: collect used headend_ids, gap-fill lowest free
- [x] cp_release derivation: find reference_player entry by name, read `release` field
- [x] `amgctl cp app playout get -n <player> -e <dir>` (pre-delete dir if exists)
- [x] `amgctl cp app playout create -r <cp_release> -i <dir> -q`
- [x] `amgctl cp app playout destroy -n <player> -q`
- [x] Poll `amgctl cp app playout logs -n <player>` → detect `"PR created in Github"` / `FATAL` / `"No changes detected"`
- [x] ANSI strip utility: `re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', text)`

### `core/topology.py`
- [x] Patch `topology.yaml`: apply `overrides` dict (dot-path keys → nested YAML)
- [x] Set `nodeTaintName`, `enableTopologyCpuOptimization: false`, `headendId`
- [x] Patch `coreservice.yaml`: overwrite only `hooks.license_handler.parameters.LICENSE_KEY`, preserve everything else

### `core/metrics.py`
- [x] Parse raw `top -b -n 1` log file (timestamped blocks)
- [x] Extract per-sample: `cpu_us%`, `cpu_sy%`, `cpu_id%`, `mem_kb` (RES column sum)
- [x] Compute: min, max, avg, p99, p99.9, p99.99 for each metric
- [x] Write `report/<player_name>.json`
- [x] Write `report/summary.yaml` (cross-testcase table)

---

## Phase 3 — Daemon

### `daemon/runner.py`
- [x] Main loop: Unix socket listener at `~/.chartools/daemon.sock`
- [x] Handle `{"action": "add", "dir": "..."}` → enqueue testsuite
- [x] On start: scan `/home/amagi/characterization/` for RUNNING/DEPLOYING testsuites → re-attach
- [x] PID file at `~/.chartools/daemon.pid`
- [x] Graceful shutdown on SIGTERM

### `daemon/deploy.py`
- [x] Full deploy flow for one testcase (steps 1–12 from design)
- [x] Review gate: 5 min auto-deploy timeout, `wait` for indefinite block, `skip` to cancel
- [x] Write/update `status.yaml` at each step
- [x] Save `amgctl create` output → `logs/<player_name>/deploy.log`
- [x] Retry flow (`chartools retry`): skip file generation, start from review gate

### `daemon/collect.py`
- [x] `collect_top.sh` script template (injected via `kubectl exec`)
- [x] Inject script into monitored containers: `player1`, `.*tardis.*`, `.*vanxio.*` (regex match)
- [x] Sync thread: `kubectl cp` log from container to server every 60s
- [x] Detect pod/container crash → log to `events.log`, wait for recovery, re-inject script
- [x] Update `samples_collected` and `crash_events` in `status.yaml` after each sync

### `daemon/terminate.py`
- [x] Scheduler: check termination dates every 5 min
- [x] On expiry: stop sync threads → parse logs → write reports → destroy playout → update status
- [x] Poll `amgctl destroy` logs until PR merges, save to `destroy.log`
- [x] If all runs done: write `summary.yaml`, set testsuite status → SUCCESS

---

## Phase 4 — CLI Commands

- [x] `chartools daemon start` — start daemon (foreground, run in screen/tmux), write PID file
- [x] `chartools daemon stop` — send SIGTERM via PID file
- [x] `chartools daemon status` — check if PID is alive
- [x] `chartools testsuite add <dir>` — send add message via Unix socket
- [x] `chartools status` — scan dirs, print all testsuite table
- [x] `chartools status <dir>` — read status.yaml, print detailed run table
- [x] `chartools report <dir>` — print summary.yaml as formatted table
- [x] `chartools terminate <dir>` — force-terminate all RUNNING runs
- [x] `chartools retry <dir>` — retry all FAILURE runs

---

## Phase 5 — Testing & Hardening

- [ ] Test ANSI stripping on real `amgctl list` output
- [ ] Test headend allocation gap-fill with known used set
- [ ] Test `top` log parser against real container output
- [ ] Test review gate: timeout, `wait`, `skip`, Enter paths
- [ ] Test daemon restart resume: kill daemon mid-run, restart, verify re-attach
- [ ] Confirm `amgctl cp app playout logs` success/failure strings against live run
- [ ] End-to-end dry run with a real testsuite (1 testcase, 1 day)
