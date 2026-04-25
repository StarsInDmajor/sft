"""Shared shell quoting and command-building utilities."""

from __future__ import annotations

import os
import shlex


def rq(path: str) -> str:
    """Quote a remote shell path.

    Uses double quotes for $HOME/~ paths (so the remote shell expands them),
    shlex.quote for all other paths.
    """
    if path.startswith("$HOME") or path.startswith("~"):
        path = path.replace("~", "$HOME")
        return f'"{path}"'
    return shlex.quote(path)


def build_env_exports(env_list: list[str] | None) -> str:
    """Build 'export KEY=VALUE ...' string from --env argument list."""
    if not env_list:
        return ""
    parts = []
    for e in env_list:
        if "=" in e:
            key, value = e.split("=", 1)
            parts.append(f"export {shlex.quote(key)}={shlex.quote(value)}")
        else:
            local_val = os.environ.get(e, "")
            parts.append(f"export {shlex.quote(e)}={shlex.quote(local_val)}")
    return " ".join(parts) + " "
