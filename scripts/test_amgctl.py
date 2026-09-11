#!/usr/bin/env python3
"""
Standalone script to test amgctl flows up to dry-run.

Usage:
    # get + create dry-run (same player)
    python scripts/test_amgctl.py --player sigma8_660_011 --dir /tmp/test_export

    # get sigma8_660_011, rename to sigma8_660_420, create dry-run as 420
    python scripts/test_amgctl.py --player sigma8_660_011 --dir /tmp/test_export \\
        --new-player sigma8_660_420

    # get + update dry-run on existing player
    python scripts/test_amgctl.py --player sigma8_660_420 --dir /tmp/test_export --update
"""

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.amgctl import (
    DeployResult,
    extract_cp_release,
    playout_create_dryrun,
    playout_get,
    playout_update_dryrun,
    poll_playout_logs,
    strip_ansi,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("test_amgctl")

SEP = "=" * 60


def step(title: str) -> None:
    print(f"\n{SEP}\n{title}\n{SEP}")


def _rename_player_in_files(export_dir: Path, old_name: str, new_name: str) -> None:
    """
    Replace all occurrences of old_name (and its hyphenated form) in all
    files under export_dir.  e.g. sigma8_660_011 → sigma8_660_420
                                   sigma8-660-011 → sigma8-660-420
    """
    old_hyphen = old_name.replace("_", "-")
    new_hyphen = new_name.replace("_", "-")

    for path in export_dir.iterdir():
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace")
            updated = text.replace(old_name, new_name).replace(old_hyphen, new_hyphen)
            if updated != text:
                path.write_text(updated)
                print(f"  patched: {path.name}")
        except Exception as e:
            print(f"  WARNING: could not patch {path.name}: {e}")


def _print_result(result: str, action: str) -> None:
    labels = {
        DeployResult.PR_CREATED:     f"✓ PR created — {action} dry-run succeeded",
        DeployResult.NO_CHANGE:      f"✓ No changes detected",
        DeployResult.ALREADY_EXISTS: f"✗ Player already exists in cloud — destroy it first",
        DeployResult.SUCCEEDED:      f"✓ Deployment succeeded (dry-run job completed)",
        DeployResult.FATAL:          f"✗ FATAL error — check logs above",
        DeployResult.TIMEOUT:        f"⚠ Timed out waiting for terminal condition",
    }
    print(labels.get(result, f"? Unknown result: {result}"))


def run(source_player: str, export_dir: Path, new_player: str, do_update: bool) -> None:
    target_player = new_player or source_player

    # ------------------------------------------------------------------ 1. get
    step(f"1. amgctl get — exporting {source_player} → {export_dir}")
    try:
        get_output = playout_get(source_player, export_dir)
        print(get_output[:600] + ("..." if len(get_output) > 600 else ""))
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # ------------------------------------------------------------------ 2. rename
    if new_player and new_player != source_player:
        step(f"2. Renaming {source_player} → {new_player} in exported files")
        _rename_player_in_files(export_dir, source_player, new_player)

    # ------------------------------------------------------------------ 3. cp_release
    step("3. Extracting cp_release")
    cp_release = extract_cp_release(get_output, export_dir)
    if cp_release:
        print(f"cp_release: {cp_release}")
    else:
        print("WARNING: could not extract cp_release")
        print("Exported files:")
        for f in sorted(export_dir.iterdir()):
            print(f"  {f.name}")
        sys.exit(1)

    # ------------------------------------------------------------------ 4. dry-run
    action = "update" if do_update else "create"
    step(f"4. amgctl {action} --dry-run — {target_player} (cp_release={cp_release})")

    if do_update:
        res = playout_update_dryrun(cp_release, export_dir)
    else:
        res = playout_create_dryrun(cp_release, export_dir)

    output = strip_ansi(res.stdout + res.stderr)
    print(f"returncode: {res.returncode}")
    print(output[:1000] + ("..." if len(output) > 1000 else ""))

    # ------------------------------------------------------------------ 5. poll logs
    step(f"5. poll amgctl logs — {target_player} [{action} dry-run]")
    print("(streaming — press Ctrl+C to abort)\n")
    result, log_text = poll_playout_logs(target_player)

    print(f"\nResult: {result}")
    _print_result(result, action)
    print(f"\n--- last 800 chars of log ---\n{log_text[-800:]}")
    print(f"\n{SEP}\nDone.\n{SEP}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test amgctl get + dry-run flows")
    parser.add_argument("--player", required=True,
                        help="Source player to get config from, e.g. sigma8_660_011")
    parser.add_argument("--dir", required=True,
                        help="Directory to export player config into")
    parser.add_argument("--new-player",
                        help="Rename to this player name before dry-run, e.g. sigma8_660_420")
    parser.add_argument("--update", action="store_true",
                        help="Use amgctl update --dry-run instead of create --dry-run")
    args = parser.parse_args()

    export_dir = Path(args.dir).expanduser().resolve()
    run(args.player, export_dir, args.new_player, args.update)


if __name__ == "__main__":
    main()
