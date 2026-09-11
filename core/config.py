"""Parse and validate config.yaml for a testsuite."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ConfigError(ValueError):
    pass


def _parse_duration(value) -> int:
    """
    Parse a human-friendly duration into seconds.

    Accepts:
      "7d"  → 604800      "2h"  → 7200
      "30m" → 1800        "5s"  → 5
      7     → 7  (plain int treated as seconds)
      "7"   → 7  (plain string int, seconds)

    Raises ConfigError on unrecognised format.
    """
    if isinstance(value, int):
        return value
    s = str(value).strip()
    try:
        if s.endswith("d"):
            return int(s[:-1]) * 86400
        if s.endswith("h"):
            return int(s[:-1]) * 3600
        if s.endswith("m"):
            return int(s[:-1]) * 60
        if s.endswith("s"):
            return int(s[:-1])
        return int(s)
    except ValueError:
        raise ConfigError(f"Cannot parse duration '{value}' — use e.g. '7d', '2h', '30m', '5s'")


@dataclass
class TestcaseConfig:
    name: str
    duration_seconds: int       # total run duration (config key: 'duration', e.g. "7d")
    poll_interval_seconds: int  # top-log collection interval (config key: 'poll_interval', e.g. "5s")
    license_key: str
    overrides: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TestsuiteConfig:
    nodetaint: str
    testcases: List[TestcaseConfig]
    reference_player: Optional[str] = None   # e.g. "hypeus2_661_020"
    cp_release: Optional[str] = None
    # derived from reference_player
    ref_namespace: Optional[str] = None
    ref_feed_id: Optional[str] = None
    ref_headend: Optional[str] = None


def load_config(config_path: Path) -> TestsuiteConfig:
    if not config_path.exists():
        raise ConfigError(f"config.yaml not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a YAML mapping")

    nodetaint = raw.get("nodetaint")
    if not nodetaint:
        raise ConfigError("config.yaml: 'nodetaint' is required")

    raw_testcases = raw.get("testcases")
    if not raw_testcases or not isinstance(raw_testcases, list):
        raise ConfigError("config.yaml: 'testcases' must be a non-empty list")

    testcases: List[TestcaseConfig] = []
    for i, tc in enumerate(raw_testcases):
        for fname in ("name", "license_key"):
            if tc.get(fname) is None:
                raise ConfigError(f"testcases[{i}]: '{fname}' is required")

        # duration — accept new key 'duration' or old key 'num_days' for backwards compat
        raw_duration = tc.get("duration") if tc.get("duration") is not None else tc.get("num_days")
        if raw_duration is None:
            raise ConfigError(f"testcases[{i}]: 'duration' is required (e.g. '7d', '12h')")

        # poll_interval — accept new key 'poll_interval' or old key 'poll_interval_seconds'
        raw_poll = (tc.get("poll_interval")
                    if tc.get("poll_interval") is not None
                    else tc.get("poll_interval_seconds"))
        if raw_poll is None:
            raise ConfigError(f"testcases[{i}]: 'poll_interval' is required (e.g. '5s', '1m')")

        testcases.append(TestcaseConfig(
            name=str(tc["name"]),
            duration_seconds=_parse_duration(raw_duration),
            poll_interval_seconds=_parse_duration(raw_poll),
            license_key=str(tc["license_key"]),
            overrides=tc.get("overrides") or {},
        ))

    cfg = TestsuiteConfig(
        nodetaint=nodetaint,
        testcases=testcases,
        reference_player=raw.get("reference_player"),
        cp_release=raw.get("cp_release"),
    )

    if cfg.reference_player:
        parts = str(cfg.reference_player).split("_")
        if len(parts) != 3:
            raise ConfigError(
                f"reference_player must be 'namespace_feedid_headend' "
                f"(e.g. hypeus2_661_020), got: {cfg.reference_player}"
            )
        cfg.ref_namespace, cfg.ref_feed_id, cfg.ref_headend = parts

    return cfg
