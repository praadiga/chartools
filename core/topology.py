"""Patch topology.yaml and coreservice.yaml for a testcase deployment."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml


def _set_nested(data: Dict, dot_path: str, value: Any) -> None:
    """Write a dot-path key into a nested dict, coercing 'True'/'False' strings."""
    if isinstance(value, str):
        if value == "True":
            value = True
        elif value == "False":
            value = False

    keys = dot_path.split(".")
    node = data
    for key in keys[:-1]:
        if key not in node or not isinstance(node[key], dict):
            node[key] = {}
        node = node[key]
    node[keys[-1]] = value


def patch_topology(
    topology_path: Path,
    overrides: Dict[str, Any],
    nodetaint: str,
    headend_id: str,
    feed_id: str,
) -> None:
    """
    Apply testcase overrides then force characterization-specific fields.

    Force-set fields always win over overrides:
      - nodeTaintName
      - enableTopologyCpuOptimization = False  (MUST stay False or results are invalid)
      - headendId
      - DDFeedDisplayTitle
    """
    with open(topology_path) as f:
        data = yaml.safe_load(f) or {}

    for dot_path, value in overrides.items():
        _set_nested(data, dot_path, value)

    _set_nested(data, "params.player.deployment_config.nodeTaintName", nodetaint)
    _set_nested(data, "params.common.enableTopologyCpuOptimization", False)
    _set_nested(data, "params.player.deployment_config.headendId", headend_id)
    _set_nested(
        data,
        "params.player.ops_config.business_config.DDFeedDisplayTitle",
        f"{feed_id}_{headend_id}",
    )

    with open(topology_path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False)


def patch_coreservice(coreservice_path: Path, license_key: str) -> None:
    """
    Patch only hooks.license_handler.parameters.LICENSE_KEY.
    All other exported fields (last_applied_migration, last_state, dependencies, etc.)
    are preserved exactly as amgctl get returned them.
    """
    with open(coreservice_path) as f:
        data = yaml.safe_load(f) or {}

    _set_nested(data, "hooks.license_handler.parameters.LICENSE_KEY", license_key)

    with open(coreservice_path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False)
