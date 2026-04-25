"""Submit command: upload a PBS script and submit it to a cluster scheduler."""

from __future__ import annotations

import datetime
import os
import shlex
import sys
import uuid
from typing import Any, Dict, Optional

from sft.context import ExecutionContext
from sft.pbs import build_qsub_command, parse_qsub_output
from sft.state import add_job_record
from sft.ui import Theme


def _resolve_host(
    ctx: ExecutionContext, target_spec: str
) -> tuple:
    from sft.commands.mount import resolve_target

    return resolve_target(target_spec, ctx, allow_empty_path=True)


def cmd_submit(args, ctx: ExecutionContext) -> None:
    """Upload a PBS script and submit it via qsub on a PBS-capable host."""
    script_local = os.path.expanduser(args.script)

    if not os.path.isfile(script_local):
        Theme.error(f"PBS script not found: {script_local}")
        sys.exit(1)

    host_info, remote_path = _resolve_host(ctx, args.target)
    if not remote_path:
        remote_path = "~"

    # Validate host has PBS scheduler
    if host_info.scheduler != "pbs":
        Theme.error(
            f"Host {host_info.name} does not have a PBS scheduler "
            f"(scheduler: {host_info.scheduler or 'none'})"
        )
        sys.exit(1)

    # Read script content
    with open(script_local, "r") as f:
        script_content = f.read()

    # PBS Pro has no qsub flag for working directory — prepend cd to script
    if args.cwd:
        cd_line = f"cd {shlex.quote(args.cwd)}\n"
        script_content = cd_line + script_content

    # Determine remote script path
    script_basename = os.path.basename(script_local)
    remote_script = os.path.join(remote_path, f".sft-submit-{script_basename}")

    # Job name
    job_name = args.name or os.path.splitext(script_basename)[0]

    # Managed output path for sft logs integration
    log_path = args.log or os.path.join(
        remote_path, f"sft-pbs-{job_name}.log"
    )

    # Parse env vars for qsub -v
    env_vars = args.env if args.env else None

    if ctx.dry_run:
        ctx.log(
            f"Dry-run: would upload {script_local} -> "
            f"{host_info.name}:{remote_script}",
            always=True,
        )
        qsub_cmd = build_qsub_command(
            script_path=remote_script,
            name=job_name,
            output_path=log_path,
            join_output=True,
            env_vars=env_vars,
            queue=args.queue,
            resources=args.resources,
        )
        Theme.command(f"ssh {host_info.name} -- {qsub_cmd}")
        ctx.log(
            f"Dry-run: would record PBS job for {host_info.name}",
            always=True,
        )
        return

    # Upload script
    ctx.log(f"Uploading PBS script to {host_info.name}:{remote_script}")
    if args.cwd:
        # Write modified script with cd prefix to a temp file
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=f"-{script_basename}", delete=False,
        ) as tmp:
            tmp.write(script_content)
            upload_path = tmp.name
    else:
        upload_path = script_local
    try:
        ctx.scp_to_remote(upload_path, host_info, remote_script)
    finally:
        if upload_path != script_local:
            os.unlink(upload_path)

    # Build qsub command
    qsub_cmd = build_qsub_command(
        script_path=remote_script,
        name=job_name,
        output_path=log_path,
        join_output=True,
        env_vars=env_vars,
        queue=args.queue,
        resources=args.resources,
    )

    # Submit via SSH
    ctx.log(f"Submitting PBS job on {host_info.name}")
    try:
        raw_output = ctx.run_ssh(
            host_info, qsub_cmd, capture=True,
        )
    except RuntimeError as e:
        # qsub failed — report error, do NOT record a job
        Theme.error(f"qsub failed: {e}")
        # Clean up uploaded script
        try:
            ctx.run_ssh(
                host_info, f"rm -f {shlex.quote(remote_script)}", silent=True,
            )
        except RuntimeError:
            pass
        sys.exit(1)

    pbs_job_id = parse_qsub_output(raw_output or "")
    if not pbs_job_id:
        Theme.error(f"Could not parse qsub output: {raw_output!r}")
        sys.exit(1)

    # Clean up uploaded script (qsub has already read it)
    try:
        ctx.run_ssh(
            host_info, f"rm -f {shlex.quote(remote_script)}", silent=True,
        )
    except RuntimeError:
        pass

    # Record job locally
    sft_job_id = uuid.uuid4().hex[:8]
    record: Dict[str, Any] = {
        "id": sft_job_id,
        "name": job_name,
        "host": host_info.name,
        "remote_cwd": remote_path,
        "command": script_basename,
        "log_path": log_path,
        "pid": None,
        "created_at": datetime.datetime.now().isoformat(),
        "singleton_key": None,
        "fetch_patterns": None,
        "fetch_dest": None,
        # PBS-specific fields
        "job_type": "pbs",
        "pbs_job_id": pbs_job_id,
        "scheduler_state": "Q",
    }
    add_job_record(record)

    Theme.success(
        f"PBS job submitted: {pbs_job_id} (sft job {sft_job_id})"
    )
    Theme.info("Log", f"{host_info.name}:{log_path}")
    Theme.info("View", f"sft logs {sft_job_id}")
    Theme.info("Jobs", f"sft jobs")
