"""Project-level .sftrc.toml configuration loading."""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

from sft.ui import Theme


# Supported keys and their expected types for validation
_SCHEMA: Dict[str, type] = {
    "post_sync": list,
    "reinstall_pkg": list,
    "exclude": list,
}


def load_project_config(src_dir: str) -> Dict[str, Any]:
    """Load .sftrc.toml from the project source directory.

    Returns an empty dict if the file does not exist or cannot be parsed.
    """
    config_path = os.path.join(src_dir, ".sftrc.toml")
    if not os.path.isfile(config_path):
        return {}

    if not tomllib:
        Theme.warning(
            ".sftrc.toml found but no TOML parser available (install tomli for Python < 3.11)"
        )
        return {}

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        Theme.warning(f"Failed to parse .sftrc.toml: {exc}")
        return {}

    # Validate known keys
    result: Dict[str, Any] = {}
    for key, expected_type in _SCHEMA.items():
        if key in data:
            value = data[key]
            if isinstance(value, expected_type):
                result[key] = value
            else:
                Theme.warning(
                    f".sftrc.toml: '{key}' should be a "
                    f"{expected_type.__name__}, got {type(value).__name__}"
                )

    return result


def merge_project_config(
    project_cfg: Dict[str, Any],
    args,
) -> tuple[list[str], list[str], list[str] | None]:
    """Merge .sftrc.toml values with CLI args. CLI flags take precedence.

    Returns (post_sync_cmds, reinstall_pkgs, excludes).
    """
    # post_sync: CLI overrides config
    cli_post_sync = getattr(args, "post_sync", None)
    post_sync = list(cli_post_sync) if cli_post_sync else list(
        project_cfg.get("post_sync", [])
    )

    # reinstall_pkg: CLI overrides config
    cli_reinstall = getattr(args, "reinstall_pkg", None)
    reinstall_pkgs = list(cli_reinstall) if cli_reinstall else list(
        project_cfg.get("reinstall_pkg", [])
    )

    # exclude: CLI overrides config (only relevant for --project-sync)
    cli_exclude = getattr(args, "exclude", None)
    excludes = cli_exclude if cli_exclude else project_cfg.get("exclude")

    return post_sync, reinstall_pkgs, excludes
