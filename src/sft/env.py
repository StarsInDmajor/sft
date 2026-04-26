"""Environment source detection, resolution, and syncing.

The core sft package provides generic project root detection and a simple
``EnvSource`` dataclass.  Nix-specific logic (.envrc / flake.nix / nix develop)
is provided by the ``sft-nix`` plugin which monkey-patches or extends these
functions when installed.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from sft.config import HostInfo, ParsedTarget
from sft.context import ExecutionContext
from sft.shell import rq as _rq
from sft.ui import Theme


# --- Generic project root detection ---


def find_project_root(path: Optional[str] = None) -> Optional[str]:
    """Walk up from *path* looking for a project marker (.git, etc)."""
    if path is None:
        path = os.getcwd()
    candidate = Path(path).expanduser().resolve()
    while True:
        if (candidate / ".git").exists():
            return str(candidate)
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return None


# --- .envrc / flake detection (stubs — overridden by sft-nix plugin) ---


def find_envrc_dir_local(path: str) -> Optional[str]:
    """Find .envrc directory walking up from *path*.

    Returns ``None`` by default in the core package.
    The ``sft-nix`` plugin overrides this to actually walk .envrc files.
    """
    return None


def find_envrc_dir_remote(
    target: ParsedTarget,
    ctx: ExecutionContext,
    *,
    allow_dry_run_execute: bool = False,
) -> Optional[str]:
    """Find .envrc directory on remote host. Stub — returns ``None``."""
    return None


def parse_envrc_flake_path_local(envrc_dir: str) -> Optional[str]:
    """Parse flake path from .envrc. Stub — returns ``None``."""
    return None


def parse_envrc_flake_path_remote(
    target: ParsedTarget,
    envrc_dir: str,
    ctx: ExecutionContext,
) -> Optional[str]:
    """Parse flake path from remote .envrc. Stub — returns ``None``."""
    return None


def parse_envrc_flake_full_local(envrc_dir: str) -> Optional[tuple]:
    """Parse full ('use flake' path + flags) from local .envrc. Stub."""
    return None


def parse_envrc_flake_full_remote(
    target: ParsedTarget,
    envrc_dir: str,
    ctx: ExecutionContext,
) -> Optional[tuple]:
    """Parse full ('use flake' path + flags) from remote .envrc. Stub."""
    return None


def compute_envrc_target_dir(source_base: str, envrc_dir: str, dest_base: str) -> str:
    """Compute where envrc-related files should land relative to dest."""
    try:
        relative = os.path.relpath(envrc_dir, source_base)
    except ValueError:
        return dest_base
    if relative.startswith(".."):
        return dest_base
    return os.path.normpath(os.path.join(dest_base, relative))


# --- EnvSource dataclass ---


@dataclass
class EnvSource:
    project_root: str
    envrc_dir: Optional[str]
    flake_path: Optional[str]
    flake_flags: List[str]
    flake_mode: str
    source_kind: str


def resolve_env_source(
    src_dir: Optional[str] = None,
    explicit_from: Optional[str] = None,
) -> Optional[EnvSource]:
    """Resolve environment source from the given directory or CWD."""
    if explicit_from:
        src_dir = os.path.expanduser(explicit_from)
        if not os.path.isdir(src_dir):
            Theme.error(f"--sync-env-from directory does not exist: {src_dir}")
            raise SystemExit(1)
        envrc_dir = find_envrc_dir_local(src_dir)
        flake_path = None
        flake_flags: List[str] = []
        if envrc_dir:
            full = parse_envrc_flake_full_local(envrc_dir)
            if full:
                flake_path, flake_flags = full
        return EnvSource(
            project_root=src_dir,
            envrc_dir=envrc_dir,
            flake_path=flake_path,
            flake_flags=flake_flags,
            flake_mode="full-flake",
            source_kind="explicit",
        )

    if src_dir:
        src_dir = os.path.expanduser(src_dir)
        if not os.path.isabs(src_dir):
            src_dir = os.path.abspath(src_dir)
        envrc_dir = find_envrc_dir_local(src_dir)
        flake_path = None
        flake_flags = []
        if envrc_dir:
            full = parse_envrc_flake_full_local(envrc_dir)
            if full:
                flake_path, flake_flags = full
        return EnvSource(
            project_root=src_dir,
            envrc_dir=envrc_dir,
            flake_path=flake_path,
            flake_flags=flake_flags,
            flake_mode="full-flake",
            source_kind="cwd",
        )

    cwd = os.getcwd()
    project_root = find_project_root(cwd)
    if project_root:
        envrc_dir = find_envrc_dir_local(project_root)
        flake_path = None
        flake_flags = []
        if envrc_dir:
            full = parse_envrc_flake_full_local(envrc_dir)
            if full:
                flake_path, flake_flags = full
        return EnvSource(
            project_root=project_root,
            envrc_dir=envrc_dir,
            flake_path=flake_path,
            flake_flags=flake_flags,
            flake_mode="full-flake",
            source_kind="cwd",
        )

    return None


# --- Remote project directory ---


def compute_remote_project_dir(
    local_project_root: str, remote_path: Optional[str]
) -> str:
    """Compute the remote project directory path.

    If *remote_path* is provided, it is returned as-is.  Otherwise the
    local project directory name is used under ``~/Workspace/`` on the
    remote host — this is a convention that matches the default directory
    layout.  Users can override this by passing an explicit remote path.
    """
    if remote_path:
        return remote_path
    project_name = os.path.basename(local_project_root.rstrip("/"))
    return f"~/Workspace/{project_name}"


def compute_remote_flake_dir(
    local_envrc_dir: str,
    local_project_root: str,
    remote_project_dir: str,
) -> str:
    """Compute where to store the synced flake on the remote host."""
    try:
        relative = os.path.relpath(local_envrc_dir, local_project_root)
    except ValueError:
        relative = ""
    if relative.startswith(".."):
        relative = ""
    flake_name = os.path.basename(local_envrc_dir.rstrip("/"))
    return os.path.normpath(
        os.path.join(remote_project_dir, ".sft", "flake-env", flake_name, relative)
    )


# --- Sync (stub — sft-nix plugin provides real implementation) ---


def sync_env_payload(
    env_source: EnvSource,
    host_info: HostInfo,
    remote_project_dir: str,
    sync_mode: str,
    ctx: ExecutionContext,
) -> Optional[str]:
    """Sync environment files to remote. Stub — returns ``None``.

    The ``sft-nix`` plugin overrides this to implement .envrc + flake syncing.
    """
    return None


# --- Remote command building ---


def build_remote_execution_command(
    remote_cwd: str,
    user_command: str,
    env_exports: str,
    auto_env: bool,
    remote_flake_dir: Optional[str],
    flake_flags: Optional[List[str]] = None,
    build_timeout: int = 0,
) -> str:
    """Build the command string to execute on the remote host.

    In the core package, this is a simple ``cd && command`` wrapper.
    The ``sft-nix`` plugin overrides this to add ``nix develop`` wrapping.
    """
    return f"cd {_rq(remote_cwd)} && {env_exports}{user_command}"


# --- Helpers ---


def ensure_remote_parent(
    host_info: HostInfo, path: str, ctx: ExecutionContext
) -> None:
    """Ensure the parent directory of *path* exists on the remote host."""
    parent = os.path.dirname(path.rstrip("/")) or "/"
    ctx.run_ssh(host_info, f"mkdir -p {_rq(parent)}")
