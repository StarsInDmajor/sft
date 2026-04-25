"""Run command: execute a command on a remote host."""

from __future__ import annotations

import os
import subprocess
import sys

from sft.background import launch_background_job
from sft.config import ParsedTarget
from sft.context import ExecutionContext
from sft.env import (
    compute_remote_project_dir,
    find_envrc_dir_remote,
    parse_envrc_flake_path_remote,
    resolve_env_source,
    sync_env_payload,
    build_remote_execution_command,
)
from sft.shell import build_env_exports
from sft.state import check_singleton
from sft.ui import Theme

from sft.commands.fetch import (
    fetch_auto_detected,
    fetch_results,
    resolve_fetch_dest,
    snapshot_remote_directory,
)


def _resolve_host(ctx, target_spec):
    from sft.commands.mount import resolve_target
    return resolve_target(target_spec, ctx, allow_empty_path=True)


def cmd_run(args, ctx: ExecutionContext) -> None:
    host_info, remote_path = _resolve_host(ctx, args.target)
    is_host_only = ":" not in args.target

    env_source = None
    should_sync_env = not getattr(args, "no_sync_env", False)
    auto_env = not getattr(args, "no_auto_env", False)

    if is_host_only:
        env_source = resolve_env_source(
            explicit_from=getattr(args, "sync_env_from", None),
        )
        if not env_source:
            Theme.error(
                "Cannot infer project root from current directory or shell"
            )
            Theme.info(
                "Hint",
                "Run from within a project directory, use --sync-env-from, or specify host:/path",
            )
            sys.exit(1)

    sync_mode = getattr(args, "env_sync_mode", None) or "full-flake"
    if getattr(args, "no_sync_env", False):
        sync_mode = "none"

    remote_project_dir = None
    remote_flake_dir = None

    if is_host_only and env_source:
        remote_project_dir = compute_remote_project_dir(
            env_source.project_root, remote_path
        )
        if should_sync_env and env_source.flake_path:
            remote_flake_dir = sync_env_payload(
                env_source, host_info, remote_project_dir, sync_mode, ctx
            )
            if remote_flake_dir and ctx.dry_run:
                ctx.log(
                    f"Dry-run: remote flake dir would be {host_info.name}:{remote_flake_dir}",
                    always=True,
                )
    else:
        if remote_path:
            remote_project_dir = remote_path
        else:
            remote_project_dir = "~"

        if should_sync_env and not is_host_only:
            explicit_from = getattr(args, "sync_env_from", None)
            if explicit_from:
                env_source = resolve_env_source(explicit_from=explicit_from)
            else:
                env_source = resolve_env_source()
            if env_source and env_source.flake_path:
                remote_flake_dir = sync_env_payload(
                    env_source, host_info, remote_project_dir, sync_mode, ctx
                )

    if not remote_flake_dir and auto_env:
        remote_envrc = find_envrc_dir_remote(
            ParsedTarget(
                is_remote=True,
                path=remote_project_dir,
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
                    path=remote_project_dir,
                    host=host_info,
                    user_override=None,
                ),
                remote_envrc,
                ctx,
            )
            if remote_flake_dir:
                remote_flake_dir = os.path.expanduser(remote_flake_dir)

    remote_cwd = args.cwd or remote_project_dir or "~"
    command = " ".join(args.run_command)

    env_exports = build_env_exports(args.env)

    singleton_key = (
        f"{host_info.name}:{remote_cwd}" if args.singleton else None
    )
    if singleton_key:
        if check_singleton(ctx, host_info, remote_cwd, singleton_key):
            Theme.error(
                f"A job is already running in {host_info.name}:{remote_cwd}"
            )
            Theme.info("Hint", "Use 'sft jobs' to see running tasks")
            sys.exit(1)

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
                f"Remote Project: {host_info.name}:{remote_project_dir}",
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
        fetch_patterns = getattr(args, "fetch", None)
        if fetch_patterns:
            local_dest = resolve_fetch_dest(
                getattr(args, "fetch_to", None), env_source
            )
            for p in fetch_patterns:
                ctx.log(
                    f"Dry-run: would fetch {host_info.name}:{remote_cwd}/{p} -> {local_dest}/",
                    always=True,
                )
        if args.background:
            log_path = args.log or os.path.join(remote_cwd, "sft-job.log")
            ctx.log(
                f"Dry-run: log at {host_info.name}:{log_path}", always=True
            )
        return

    if args.retry and args.retry > 0:
        retry_cmd = f"last_rc=0; for i in $(seq 1 {args.retry}); do {full_cmd}; last_rc=$?; [ $last_rc -eq 0 ] && break; echo 'Retry $i/{args.retry} failed (exit $last_rc), retrying...' >&2; sleep $((i * 5)); done; exit $last_rc"
        full_cmd = retry_cmd

    if args.background:
        log_path = args.log or os.path.join(remote_cwd, "sft-job.log")
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
            name_prefix="job",
        )
    else:
        ctx.log(f"Running on {host_info.name}:{remote_cwd}")
        if ctx.verbose:
            Theme.command(full_cmd)
        ctx._ensure_ssh_control_master(host_info)

        fetch_auto = getattr(args, "fetch_auto", False)
        before_snapshot = {}
        if fetch_auto:
            before_snapshot = snapshot_remote_directory(
                ctx, host_info, remote_cwd
            )

        ssh_cmd = (
            ["ssh", "-p", str(host_info.port)]
            + ctx._ssh_options(host_info)
            + [host_info.ssh_target(), full_cmd]
        )
        result = subprocess.run(ssh_cmd)
        if result.returncode != 0:
            fetch_patterns = getattr(args, "fetch", None)
            if fetch_patterns:
                Theme.warning(
                    f"Remote command failed (exit {result.returncode}), skipping result fetch"
                )
            raise SystemExit(result.returncode)

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
