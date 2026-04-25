"""Mount, umount, mounts commands."""

from __future__ import annotations

import datetime
import os
import shlex
import shutil
import subprocess
import sys
from typing import Tuple

from sft.config import HostInfo
from sft.context import ExecutionContext
from sft.state import (
    add_mount_record,
    clean_stale_records,
    derive_mountpoint,
    find_mount_record,
    is_mount_alive,
    load_mounts_state,
    remove_mount_record,
)
from sft.ui import Theme


def resolve_target(
    spec: str, ctx: ExecutionContext, *, allow_empty_path: bool = False
) -> Tuple[HostInfo, str]:
    """Unified host/path resolver for run, sync-run, mount.

    Returns (HostInfo, remote_path). remote_path may be empty string
    when allow_empty_path=True (used by run/sync-run for 'host:' or 'host').
    """
    from sft.config import load_hosts

    if ":" not in spec:
        host_part = spec
        remote_path = ""
    else:
        host_part, remote_path = spec.split(":", 1)
        if not remote_path and not allow_empty_path:
            Theme.error("Remote target must include a path after :")
            sys.exit(1)

    user_override = None
    if "@" in host_part:
        user_override, host_part = host_part.split("@", 1)

    hosts, alias_map = load_hosts(ctx.config)
    host_key = alias_map.get(host_part) or host_part
    host_info = hosts.get(host_key)
    if not host_info:
        Theme.error(f"Unknown host: {host_part}")
        sys.exit(1)

    if user_override:
        host_info = HostInfo(
            name=host_info.name,
            hostname=host_info.hostname,
            port=host_info.port,
            user=user_override,
            aliases=host_info.aliases,
            extra_options=host_info.extra_options,
        )

    return host_info, remote_path


def _resolve_remote_path(
    remote_path: str, host_info: HostInfo, ctx: ExecutionContext
) -> str:
    """Expand ~ and resolve relative paths to absolute via SSH."""
    if remote_path.startswith("/"):
        return remote_path
    try:
        resolved = ctx.run_ssh(
            host_info,
            f"python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' {shlex.quote(remote_path)}",
            capture=True,
        )
        if resolved:
            return resolved.strip()
    except RuntimeError:
        pass
    return remote_path


def ensure_mount(
    host_info: HostInfo,
    remote_path: str,
    ctx: ExecutionContext,
    *,
    mount_point: str | None = None,
    readonly: bool = False,
    allow_other: bool = False,
    no_cache: bool = False,
    mkdir: bool = True,
) -> str:
    """Ensure remote path is mounted locally. Returns local mount path.

    Reuses existing mount if alive. Raises RuntimeError on failure.
    Does not sys.exit — safe for programmatic use.
    """
    remote_path = _resolve_remote_path(remote_path, host_info, ctx)

    local_mount = mount_point or derive_mountpoint(host_info.name, remote_path)
    local_mount = os.path.expanduser(local_mount)

    if is_mount_alive(local_mount):
        ctx.log(f"Already mounted at {local_mount}")
        return local_mount

    if not os.path.exists(local_mount):
        if mkdir:
            os.makedirs(local_mount, exist_ok=True)
            ctx.log(f"Created mountpoint {local_mount}")
        else:
            raise RuntimeError(f"Mountpoint does not exist: {local_mount}")

    if not os.path.isdir(local_mount):
        raise RuntimeError(f"Mountpoint is not a directory: {local_mount}")

    if not shutil.which("sshfs"):
        raise RuntimeError(
            "sshfs not found. Install via your package manager (e.g. apt install sshfs, brew install sshfs)"
        )

    # Build SSH options WITHOUT ControlPath — sshfs forks its own ssh
    # subprocess, and sharing the multiplex socket with sft's regular
    # SSH sessions causes the socket to stall (verified: subsequent
    # "ssh -o ControlPath=..." hangs while sshfs is alive).
    ssh_opts = ["-o", f"ConnectTimeout={ctx.config.ssh_connect_timeout}"]
    for key, value in host_info.extra_options.items():
        ssh_opts += ["-o", f"{key}={value}"]
    # Explicitly disable mux so ~/.ssh/config won't interfere either
    ssh_opts += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]

    sshfs_args = [
        "sshfs",
        f"{host_info.ssh_target()}:{remote_path}",
        local_mount,
        "-p",
        str(host_info.port),
        "-o",
        "reconnect",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ] + ssh_opts

    if readonly:
        sshfs_args.extend(["-o", "ro"])
    if allow_other:
        sshfs_args.extend(["-o", "allow_other"])
    if not no_cache:
        sshfs_args.extend(["-o", "kernel_cache"])

    ctx.log(f"Mounting {host_info.name}:{remote_path} -> {local_mount}")
    if ctx.verbose:
        Theme.command(" ".join(shlex.quote(a) for a in sshfs_args))

    try:
        result = subprocess.run(
            sshfs_args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(f"sshfs failed: {stderr}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("sshfs timed out after 30s")

    abs_mount = os.path.abspath(local_mount)
    record = {
        "host": host_info.name,
        "remote_path": remote_path,
        "local_mount": abs_mount,
        "readonly": readonly,
        "allow_other": allow_other,
        "created_at": datetime.datetime.now().isoformat(),
    }
    add_mount_record(record)
    ctx.log(f"Successfully mounted at {local_mount}")
    return local_mount


def cmd_mount(args, ctx: ExecutionContext) -> None:
    host_info, remote_path = resolve_target(args.remote, ctx, allow_empty_path=False)

    try:
        local_mount = ensure_mount(
            host_info,
            remote_path,
            ctx,
            mount_point=args.local_mount,
            readonly=args.readonly,
            allow_other=args.allow_other,
            no_cache=args.no_cache,
            mkdir=args.mkdir,
        )
    except RuntimeError as e:
        msg = str(e)
        if "Already mounted" in msg:
            Theme.warning(msg)
            sys.exit(0)
        if "FUSE" in msg or "fuse" in msg:
            Theme.error(msg)
            Theme.info("Hint", "On WSL, try: wsl --update")
        else:
            Theme.error(msg)
        sys.exit(1)

    Theme.success(f"Mounted {host_info.name}:{remote_path} at {local_mount}")


def cmd_umount(args, ctx: ExecutionContext) -> None:
    local_mount = os.path.abspath(os.path.expanduser(args.local_mount))

    if not os.path.exists(local_mount):
        Theme.error(f"Path does not exist: {local_mount}")
        sys.exit(1)

    if not is_mount_alive(local_mount):
        record = find_mount_record(local_mount)
        if record:
            Theme.warning(f"Not mounted at {local_mount} (cleaning stale record)")
            remove_mount_record(local_mount)
        else:
            Theme.warning(f"Not mounted at {local_mount}")
        sys.exit(0)

    umount_cmd = None
    if shutil.which("fusermount"):
        umount_cmd = ["fusermount", "-u", local_mount]
    elif shutil.which("fusermount3"):
        umount_cmd = ["fusermount3", "-u", local_mount]
    elif shutil.which("umount"):
        umount_cmd = ["umount", local_mount]
    else:
        Theme.error("No umount command found (tried fusermount, fusermount3, umount)")
        sys.exit(1)

    if ctx.dry_run:
        ctx.log(f"Dry-run: {' '.join(umount_cmd)}")
        return

    ctx.log(f"Unmounting {local_mount}")
    try:
        result = subprocess.run(umount_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            Theme.error(f"Unmount failed: {result.stderr.strip()}")
            sys.exit(1)
    except subprocess.TimeoutExpired:
        Theme.error("Unmount timed out after 30s")
        sys.exit(1)

    remove_mount_record(local_mount)
    Theme.success(f"Unmounted {local_mount}")


def cmd_mounts(args, ctx: ExecutionContext) -> None:
    stale = clean_stale_records()
    mounts = load_mounts_state()

    if not mounts:
        Theme.info("No active mounts", "")
        return

    if stale:
        Theme.warning(f"Cleaned {len(stale)} stale record(s)")

    header = f"{Theme.BOLD}{'HOST':<14} {'REMOTE PATH':<28} {'LOCAL MOUNT':<40} {'STATUS'}{Theme.CLR}"
    print(header)

    for m in mounts:
        alive = is_mount_alive(m["local_mount"])
        status = (
            f"{Theme.GREEN}active{Theme.CLR}"
            if alive
            else f"{Theme.YELLOW}stale{Theme.CLR}"
        )
        print(
            f"  {m['host']:<14} {m['remote_path']:<28} {m['local_mount']:<40} {status}"
        )
