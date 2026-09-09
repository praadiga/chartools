"""Parse and validate config.yaml for a testsuite."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ConfigError(ValueError):
    pass


@dataclass
class TestcaseConfig:
    name: str
    num_days: int
    poll_interval_seconds: int
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
        for fname in ("name", "num_days", "poll_interval_seconds", "license_key"):
            if tc.get(fname) is None:
                raise ConfigError(f"testcases[{i}]: '{fname}' is required")
        testcases.append(TestcaseConfig(
            name=str(tc["name"]),
            num_days=int(tc["num_days"]),
            poll_interval_seconds=int(tc["poll_interval_seconds"]),
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
