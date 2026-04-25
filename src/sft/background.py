"""Background job launch logic shared by cmd_run and cmd_sync_run."""

from __future__ import annotations

import base64
import datetime
import shlex
import time
import uuid
from typing import Any, Dict, List, Optional

from sft.config import HostInfo
from sft.context import ExecutionContext
from sft.shell import rq
from sft.state import add_job_record
from sft.ui import Theme


def launch_background_job(
    ctx: ExecutionContext,
    host_info: HostInfo,
    remote_cwd: str,
    full_cmd: str,
    command: str,
    log_path: str,
    singleton_key: Optional[str],
    fetch_patterns: Optional[List[str]],
    fetch_dest: str,
    name: Optional[str],
    name_prefix: str,
) -> str:
    """Launch a nohup background job on remote host and record it locally.

    Returns the job ID.
    """
    log_dir = log_path.rsplit("/", 1)[0] if "/" in log_path else ""
    log_path_r = log_path.replace("~", "$HOME")
    log_dir_r = log_dir.replace("~", "$HOME") if log_dir else ""

    pid_wrapper = f"echo $$ > {rq(log_path_r + '.pid')}; "
    encoded_cmd = base64.b64encode(
        (pid_wrapper + full_cmd).encode()
    ).decode()
    exit_file = rq(log_path_r + ".exit")
    log_path_q = rq(log_path_r)
    mkdir_cmd = (
        f"mkdir -p {rq(log_dir_r)}" if log_dir_r else ""
    )
    nohup_cmd = (
        f"{mkdir_cmd} && "
        f"( echo {shlex.quote(encoded_cmd)} | base64 -d | "
        f"nohup bash > {log_path_q} 2>&1 < /dev/null; "
        f"echo $? > {exit_file} ) &"
    )

    ctx.log(f"Starting background job on {host_info.name}:{remote_cwd}")
    if ctx.verbose:
        Theme.command(full_cmd)

    ctx.run_ssh(host_info, nohup_cmd, capture=True)
    pid_file = rq(log_path_r + ".pid")
    pid_output = None
    for _ in range(10):
        time.sleep(0.3)
        pid_output = ctx.run_ssh(
            host_info, f"cat {pid_file} 2>/dev/null", capture=True,
            silent=True,
        )
        if pid_output and pid_output.strip().isdigit():
            break
        pid_output = None
    if not pid_output or not pid_output.strip().isdigit():
        raise RuntimeError("Failed to get PID from remote host after 3s")

    pid = int(pid_output.strip())
    try:
        pid_start = ctx.run_ssh(
            host_info, f"ps -o lstart= -p {pid}", capture=True,
            silent=True,
        )
    except RuntimeError:
        ctx.log(f"Warning: could not get start time for PID {pid}")
        pid_start = None

    job_id = uuid.uuid4().hex[:8]
    record: Dict[str, Any] = {
        "id": job_id,
        "name": name or f"{name_prefix}-{job_id}",
        "host": host_info.name,
        "remote_cwd": remote_cwd,
        "command": command,
        "log_path": log_path,
        "pid": pid,
        "pid_started_at": pid_start.strip() if pid_start else None,
        "created_at": datetime.datetime.now().isoformat(),
        "singleton_key": singleton_key,
        "fetch_patterns": fetch_patterns,
        "fetch_dest": fetch_dest,
    }
    add_job_record(record)
    Theme.success(
        f"Background job {job_id} started (PID {pid} on {host_info.name})"
    )
    Theme.info("Log", f"{host_info.name}:{log_path}")
    Theme.info("View", f"sft logs {job_id}")
    if fetch_patterns:
        Theme.info("Fetch", f"sft fetch {job_id}")

    return job_id
