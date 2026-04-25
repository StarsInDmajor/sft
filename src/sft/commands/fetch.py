"""Fetch helpers: resolve dest, snapshot, diff, fetch results."""

from __future__ import annotations

import os
import shlex
from typing import Any, Dict, List, Optional, Tuple

from sft.config import HostInfo
from sft.context import ExecutionContext
from sft.env import EnvSource
from sft.ui import Theme


def resolve_fetch_dest(
    fetch_to: Optional[str], env_source: Optional[EnvSource]
) -> str:
    if fetch_to:
        return os.path.expanduser(fetch_to)
    if env_source and env_source.project_root:
        return env_source.project_root
    return os.getcwd()


AUTO_FETCH_EXCLUDES = (
    ".sft", ".git", ".direnv", ".venv", "__pycache__",
    "result", ".mypy_cache",
)


def snapshot_remote_directory(
    ctx: ExecutionContext,
    host_info: HostInfo,
    remote_cwd: str,
) -> Dict[str, Any]:
    expanded = os.path.expanduser(remote_cwd)
    exclude_args = " ".join(
        f"-not -path '*/{d}/*'" for d in AUTO_FETCH_EXCLUDES
    )
    cmd = (
        f"cd {shlex.quote(expanded)} && "
        f"find . -type f {exclude_args} -printf '%P\\t%s\\t%T@\\n' 2>/dev/null"
    )
    try:
        output = ctx.run_ssh(
            host_info, cmd, capture=True, allow_dry_run_execute=True
        )
    except RuntimeError:
        return {}
    snapshot: Dict[str, Any] = {}
    if not output:
        return snapshot
    for line in output.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) >= 3:
            rel_path, size, mtime = parts[0], parts[1], parts[2]
            snapshot[rel_path] = {"size": int(size), "mtime": float(mtime)}
    return snapshot


def diff_remote_snapshots(
    before: Dict[str, Any],
    after: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    added = []
    modified = []
    for path, info in after.items():
        if path not in before:
            added.append(path)
        elif (
            before[path].get("size") != info.get("size")
            or before[path].get("mtime") != info.get("mtime")
        ):
            modified.append(path)
    return added, modified


def fetch_auto_detected(
    host_info: HostInfo,
    remote_cwd: str,
    local_dest: str,
    before_snapshot: Dict[str, Any],
    ctx: ExecutionContext,
) -> None:
    after_snapshot = snapshot_remote_directory(
        ctx, host_info, remote_cwd
    )
    added, modified = diff_remote_snapshots(
        before_snapshot, after_snapshot
    )
    all_files = added + modified
    if not all_files:
        Theme.info("Fetch", "No new or modified files detected")
        return
    Theme.info(
        "Fetch",
        f"Auto-detected {len(all_files)} file(s) to fetch",
    )
    for rel_path in all_files:
        remote_file = os.path.join(remote_cwd, rel_path)
        if ctx.dry_run:
            ctx.log(
                f"Dry-run: would fetch {host_info.name}:{remote_file} -> {local_dest}/",
                always=True,
            )
            continue
        local_file = os.path.join(local_dest, rel_path)
        local_parent = os.path.dirname(local_file)
        if local_parent:
            os.makedirs(local_parent, exist_ok=True)
        ssh_cmd_parts = (
            ["ssh", "-p", str(host_info.port)]
            + ctx._ssh_options(host_info)
        )
        ssh_full = " ".join(shlex.quote(p) for p in ssh_cmd_parts)
        rsync_cmd = [
            "rsync",
            "-az",
            "--partial",
            "--info=progress2",
            "-e",
            ssh_full,
            f"{host_info.ssh_target()}:{remote_file}",
            f"{local_file}",
        ]
        ctx.run(rsync_cmd, description=f"Fetch {rel_path}")


def fetch_results(
    host_info: HostInfo,
    remote_cwd: str,
    patterns: List[str],
    local_dest: str,
    ctx: ExecutionContext,
) -> None:
    if not patterns:
        return

    local_dest = os.path.expanduser(local_dest)
    if not os.path.isdir(local_dest):
        os.makedirs(local_dest, exist_ok=True)

    ssh_cmd_parts = (
        ["ssh", "-p", str(host_info.port)] + ctx._ssh_options(host_info)
    )
    ssh_full = " ".join(shlex.quote(p) for p in ssh_cmd_parts)

    for pattern in patterns:
        if os.path.isabs(pattern):
            remote_glob = pattern
        else:
            remote_glob = os.path.join(remote_cwd, pattern)
        remote_glob = os.path.expanduser(remote_glob)

        if ctx.dry_run:
            ctx.log(
                f"Dry-run: would fetch {host_info.name}:{remote_glob} -> {local_dest}/",
                always=True,
            )
            continue

        expanded = ctx.run_ssh(
            host_info,
            f"cd {shlex.quote(remote_cwd)} && ls -1d {pattern} 2>/dev/null",
            capture=True,
        )
        if not expanded or not expanded.strip():
            Theme.warning(f"No remote files matched: {pattern}")
            continue

        matched = [
            line.strip()
            for line in expanded.strip().splitlines()
            if line.strip()
        ]
        if not matched:
            Theme.warning(f"No remote files matched: {pattern}")
            continue

        for item in matched:
            basename = os.path.basename(item)
            ctx.log(f"Fetching {host_info.name}:{item} -> {local_dest}/")
            rsync_cmd = [
                "rsync",
                "-az",
                "--partial",
                "--info=progress2",
                "-e",
                ssh_full,
                f"{host_info.ssh_target()}:{item}",
                f"{local_dest}/",
            ]
            ctx.run(
                rsync_cmd,
                description=f"Fetch {basename} from {host_info.name}",
            )

    if not ctx.dry_run:
        Theme.success(f"Results fetched to {local_dest}/")
