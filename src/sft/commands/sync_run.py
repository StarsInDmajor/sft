"""Sync-run command: sync files then execute remotely."""

from __future__ import annotations

import os
import shlex
import sys

from sft.background import launch_background_job
from sft.config import ParsedTarget
from sft.context import ExecutionContext
from sft.env import (
    build_remote_execution_command,
    find_envrc_dir_remote,
    parse_envrc_flake_path_remote,
    resolve_env_source,
    sync_env_payload,
)
from sft.manifest import (
    build_manifest,
    compare_manifests,
    format_delta,
    load_remote_manifest,
    save_remote_manifest,
)
from sft.shell import build_env_exports, rq as _rq
from sft.state import check_singleton
from sft.ui import Theme
from sft.config_project import load_project_config, merge_project_config

from sft.commands.fetch import (
    fetch_auto_detected,
    fetch_results,
    resolve_fetch_dest,
    snapshot_remote_directory,
)


def _resolve_host(ctx, target_spec):
    from sft.commands.mount import resolve_target
    return resolve_target(target_spec, ctx, allow_empty_path=True)


def cmd_sync_run(args, ctx: ExecutionContext) -> None:
    src_dir = os.path.expanduser(args.src_dir)

    if not os.path.isdir(src_dir):
        Theme.error(f"Source directory does not exist: {src_dir}")
        sys.exit(1)

    host_info, remote_path = _resolve_host(ctx, args.target)
    if not remote_path:
        remote_path = "~"

    if getattr(args, "diff", False):
        old_manifest = load_remote_manifest(
            host_info, remote_path, ctx
        )
        if not old_manifest:
            Theme.info("Sync", "No previous sync manifest found. Run a sync first.")
            return
        excludes = [
            ".git", ".direnv", ".venv", "result",
            "__pycache__", "*.pyc", ".mypy_cache",
        ]
        new_manifest = build_manifest(
            src_dir, remote_path, excludes, "project-sync"
        )
        added, modified, deleted = compare_manifests(
            old_manifest, new_manifest
        )
        print(format_delta(added, modified, deleted))
        return

    singleton_key = (
        f"{host_info.name}:{remote_path}" if args.singleton else None
    )
    if singleton_key:
        if check_singleton(ctx, host_info, remote_path, singleton_key):
            Theme.error(
                f"A job is already running in {host_info.name}:{remote_path}"
            )
            sys.exit(1)

    # --- Load project config early (before sync phase uses args.exclude) ---
    project_cfg = load_project_config(src_dir)
    _post_sync_from_cfg, _reinstall_from_cfg, cfg_excludes = merge_project_config(
        project_cfg, args
    )

    # If .sftrc.toml provides excludes but CLI doesn't, inject them
    if cfg_excludes and not getattr(args, "exclude", None):
        args.exclude = cfg_excludes

    if args.project_sync:
        excludes = [
            ".git",
            ".direnv",
            ".venv",
            "result",
            "__pycache__",
            "*.pyc",
            ".mypy_cache",
        ]
        exclude_args = []
        for exc in excludes:
            exclude_args.extend(["--exclude", exc])
        if args.exclude:
            for exc in args.exclude:
                exclude_args.extend(["--exclude", exc])

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
        ] + exclude_args + [
            "-e",
            ssh_full,
            f"{src_dir}/",
            f"{host_info.ssh_target()}:{remote_path}/",
        ]

        if args.delete:
            rsync_cmd.append("--delete")

        if ctx.dry_run:
            ctx.log(
                f"Dry-run: would sync {src_dir}/ -> {host_info.name}:{remote_path}/",
                always=True,
            )
            Theme.command(
                " ".join(shlex.quote(a) for a in rsync_cmd)
            )
        else:
            old_manifest = load_remote_manifest(
                host_info, remote_path, ctx
            )
            if old_manifest:
                new_manifest = build_manifest(
                    src_dir, remote_path, excludes, "project-sync"
                )
                added, modified, deleted = compare_manifests(
                    old_manifest, new_manifest
                )
                if not added and not modified and not deleted:
                    Theme.info("Sync", "No changes since last sync")
                else:
                    Theme.step(
                        Theme.XFER,
                        "Changes since last sync",
                        format_delta(added, modified, deleted),
                    )
            ctx.log(f"Syncing Project to {host_info.name}:{remote_path}")
            ctx.run(
                rsync_cmd,
                description=f"Sync project to {host_info.name}",
            )
            new_manifest = build_manifest(
                src_dir, remote_path, excludes, "project-sync"
            )
            save_remote_manifest(
                host_info, remote_path, new_manifest, ctx
            )
    elif args.include:
        for rel_path in args.include:
            local_file = os.path.join(src_dir, rel_path)
            if not os.path.exists(local_file):
                Theme.warning(f"File not found, skipping: {rel_path}")
                continue
            remote_dest = f"{remote_path}/{rel_path}"
            remote_dir = os.path.dirname(rel_path)
            if remote_dir:
                if ctx.dry_run:
                    ctx.log(
                        f"Dry-run: would create remote directory {os.path.join(remote_path, remote_dir)}",
                        always=True,
                    )
                else:
                    ctx.run_ssh(
                        host_info,
                        f"mkdir -p {shlex.quote(os.path.join(remote_path, remote_dir))}",
                    )
            if ctx.dry_run:
                ctx.log(
                    f"Dry-run: would upload {rel_path} -> {host_info.name}:{remote_path}/{rel_path}",
                    always=True,
                )
            else:
                ctx.scp_to_remote(local_file, host_info, remote_dest)
                ctx.log(f"Uploaded {rel_path}")
    else:
        Theme.error(
            "No files to sync. Use --include <file> or --project-sync"
        )
        Theme.info(
            "Hint",
            "--include pyproject.toml --include uv.lock --include run_21cmfast.py",
        )
        sys.exit(1)

    # --- Post-sync hook: run commands after file sync, before env setup ---
    post_sync_cmds = list(getattr(args, "post_sync", None) or [])
    reinstall_pkgs = getattr(args, "reinstall_pkg", None) or []

    # Merge with .sftrc.toml defaults (CLI takes precedence)
    if not getattr(args, "post_sync", None):
        post_sync_cmds = list(_post_sync_from_cfg)
    if not getattr(args, "reinstall_pkg", None):
        reinstall_pkgs = list(_reinstall_from_cfg)

    for pkg in reinstall_pkgs:
        post_sync_cmds.append(
            f"uv sync --reinstall-package {shlex.quote(pkg)}"
        )

    if post_sync_cmds:
        for cmd in post_sync_cmds:
            if ctx.dry_run:
                ctx.log(
                    f"Dry-run: would run post-sync on {host_info.name}: {cmd}",
                    always=True,
                )
            else:
                Theme.step(Theme.OK, "Post-sync", cmd)
                ctx.run_ssh(
                    host_info,
                    f"cd {_rq(remote_path)} && {cmd}",
                )

    env_source = resolve_env_source(src_dir=src_dir)
    sync_mode = getattr(args, "env_sync_mode", None) or "full-flake"
    auto_env = not getattr(args, "no_auto_env", False)
    remote_flake_dir = None

    if env_source and env_source.flake_path:
        remote_flake_dir = sync_env_payload(
            env_source, host_info, remote_path, sync_mode, ctx
        )
        if remote_flake_dir and ctx.dry_run:
            ctx.log(
                f"Dry-run: remote flake dir would be {host_info.name}:{remote_flake_dir}",
                always=True,
            )

    if not remote_flake_dir and auto_env:
        remote_envrc = find_envrc_dir_remote(
            ParsedTarget(
                is_remote=True,
                path=remote_path,
                host=host_info,
                user_override=None,
            ),
            ctx,
            allow_dry_run_execute=True,
        )
        if remote_envrc:
            remote_flake_dir = parse_envrc_flake_path_remote(
                ParsedTarget(
                    is_remote=True,
                    path=remote_path,
                    host=host_info,
                    user_override=None,
                ),
                remote_envrc,
                ctx,
            )
            if remote_flake_dir:
                remote_flake_dir = os.path.expanduser(remote_flake_dir)

    command = " ".join(args.run_command)
    remote_cwd = args.cwd or remote_path
    env_exports = build_env_exports(args.env)

    full_cmd = build_remote_execution_command(
        remote_cwd=remote_cwd,
        user_command=command,
        env_exports=env_exports,
        auto_env=auto_env,
        remote_flake_dir=remote_flake_dir,
        flake_flags=env_source.flake_flags if env_source else None,
        build_timeout=getattr(args, "build_timeout", 0),
    )

    if ctx.dry_run:
        if env_source:
            ctx.log(f"Env Source: {env_source.source_kind}", always=True)
            ctx.log(
                f"Project Root: {env_source.project_root}", always=True
            )
            if env_source.flake_path:
                ctx.log(
                    f"Local Flake: {env_source.flake_path}", always=True
                )
            ctx.log(
                f"Remote Project: {host_info.name}:{remote_path}",
                always=True,
            )
            if remote_flake_dir:
                ctx.log(
                    f"Remote Flake: {host_info.name}:{remote_flake_dir}",
                    always=True,
                )
        ctx.log(
            f"Dry-run: would execute on {host_info.name}:{remote_cwd}",
            always=True,
        )
        Theme.command(full_cmd)
        sync_fetch_patterns = getattr(args, "fetch", None)
        if sync_fetch_patterns:
            sync_local_dest = resolve_fetch_dest(
                getattr(args, "fetch_to", None), env_source
            )
            for p in sync_fetch_patterns:
                ctx.log(
                    f"Dry-run: would fetch {host_info.name}:{remote_cwd}/{p} -> {sync_local_dest}/",
                    always=True,
                )
        return

    if args.retry and args.retry > 0:
        max_attempts = args.retry
        retry_cmd = f"last_rc=0; for attempt in $(seq 1 {max_attempts}); do {full_cmd}; last_rc=$?; [ $last_rc -eq 0 ] && break; echo 'Attempt $attempt/{max_attempts} failed (exit $last_rc), retrying...' >&2; sleep $((attempt * 5)); done; exit $last_rc"
        full_cmd = retry_cmd

    if args.background:
        log_path = args.log or os.path.join(remote_path, "sft-job.log")
        launch_background_job(
            ctx=ctx,
            host_info=host_info,
            remote_cwd=remote_cwd,
            full_cmd=full_cmd,
            command=command,
            log_path=log_path,
            singleton_key=singleton_key,
            fetch_patterns=getattr(args, "fetch", None),
            fetch_dest=resolve_fetch_dest(
                getattr(args, "fetch_to", None), env_source
            ),
            name=args.name,
            name_prefix="sync",
        )
    else:
        ctx.log(f"Running on {host_info.name}:{remote_cwd}")
        if ctx.verbose:
            Theme.command(full_cmd)

        fetch_auto = getattr(args, "fetch_auto", False)
        before_snapshot = {}
        if fetch_auto:
            before_snapshot = snapshot_remote_directory(
                ctx, host_info, remote_cwd
            )

        ctx.run_ssh(host_info, full_cmd)

        fetch_patterns = getattr(args, "fetch", None)
        if fetch_patterns:
            local_dest = resolve_fetch_dest(
                getattr(args, "fetch_to", None), env_source
            )
            fetch_results(
                host_info, remote_cwd, fetch_patterns, local_dest, ctx
            )

        if fetch_auto:
            local_dest = resolve_fetch_dest(
                getattr(args, "fetch_to", None), env_source
            )
            fetch_auto_detected(
                host_info, remote_cwd, local_dest, before_snapshot, ctx
            )
