"""Job management commands: jobs, logs, stop, status, fetch."""

from __future__ import annotations

import datetime
import os
import shlex
import subprocess
import sys
import time
from collections import defaultdict

from sft.config import load_hosts
from sft.context import ExecutionContext
from sft.shell import rq
from sft.state import (
    batch_check_jobs,
    batch_check_pbs_jobs,
    find_job_record,
    get_job_exit_code,
    is_job_alive,
    load_jobs_state,
    save_jobs_state,
    update_job_record,
)
from sft.ui import Theme

from sft.commands.fetch import fetch_results


def _is_pid_alive(host_info, pid, ctx):
    return is_job_alive(ctx, host_info, pid)


def _is_job_alive(job: dict, host_info, ctx: ExecutionContext) -> bool:
    """Check if a job is alive, dispatching by job_type."""
    jtype = job.get("job_type", "process")
    if jtype == "pbs":
        pbs_id = job.get("pbs_job_id")
        if not pbs_id:
            return False
        results = batch_check_pbs_jobs(ctx, host_info, [job])
        r = results.get(job["id"], {})
        return r.get("status") not in ("done", "unknown")
    else:
        pid = job.get("pid")
        if not pid:
            return False
        return bool(_is_pid_alive(host_info, int(pid), ctx))


def cmd_jobs(args, ctx: ExecutionContext) -> None:
    jobs = load_jobs_state()

    if not jobs:
        Theme.info("No active jobs", "")
        return

    hosts_map, _ = load_hosts(ctx.config)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for job in jobs:
        grouped[job["host"]].append(job)

    for host_name, host_jobs in grouped.items():
        host_info = hosts_map.get(host_name)
        if not host_info:
            for job in host_jobs:
                job["_status"] = job.get("_status", "unknown")
            continue

        results = batch_check_jobs(ctx, host_info, host_jobs)

        for job in host_jobs:
            jid = job["id"]
            result = results.get(jid, {
                "status": "unknown", "exit_code": None,
            })
            status = result["status"]

            if status == "done":
                job["_status"] = "completed"
                if not job.get("completed_at"):
                    job["completed_at"] = datetime.datetime.now().isoformat()
                if job.get("exit_code") is None:
                    exit_code = result.get("exit_code")
                    jtype = job.get("job_type", "process")
                    if exit_code is None and jtype == "process":
                        exit_code = get_job_exit_code(
                            ctx, host_info, job.get("log_path", "")
                        )
                    job["exit_code"] = exit_code
                # Update PBS scheduler_state if present
                if result.get("scheduler_state"):
                    job["scheduler_state"] = result["scheduler_state"]
            elif status == "queued":
                job["_status"] = "queued"
            elif status == "held":
                job["_status"] = "held"
            elif status == "unknown":
                job["_status"] = "unknown"
            else:
                job["_status"] = "running"
                # Update PBS scheduler_state if present
                if result.get("scheduler_state"):
                    job["scheduler_state"] = result["scheduler_state"]

    all_jobs = list(jobs)
    save_jobs_state(all_jobs)

    if args.host:
        hosts_map, alias_map = load_hosts(ctx.config)
        target_host = alias_map.get(args.host) or args.host
        all_jobs = [j for j in all_jobs if j["host"] == target_host]

    if args.status:
        if args.status == "running":
            all_jobs = [
                j for j in all_jobs if j.get("_status") in ("running", "queued", "held")
            ]
        elif args.status == "completed":
            all_jobs = [
                j for j in all_jobs if j.get("_status") == "completed"
            ]

    if args.name:
        all_jobs = [
            j
            for j in all_jobs
            if args.name.lower() in j.get("name", "").lower()
        ]

    if args.after:
        all_jobs = [
            j for j in all_jobs if j.get("created_at", "") >= args.after
        ]

    if args.before:
        all_jobs = [
            j for j in all_jobs if j.get("created_at", "") <= args.before
        ]

    if not all_jobs:
        filter_desc = []
        if args.status:
            filter_desc.append(f"status={args.status}")
        if args.host:
            filter_desc.append(f"host={args.host}")
        if args.name:
            filter_desc.append(f"name={args.name}")
        if filter_desc:
            Theme.info("No jobs matching", ", ".join(filter_desc))
        else:
            Theme.info("No active jobs", "")
        return

    header = f"{Theme.BOLD}{'ID':<10} {'NAME':<16} {'HOST':<14} {'TYPE':<6} {'JOB-ID':<16} {'COMMAND':<28} {'CREATED'}{Theme.CLR}"
    print(header)

    for j in all_jobs:
        cmd_display = (
            (j["command"][:25] + "...")
            if len(j["command"]) > 28
            else j["command"]
        )
        created = j.get("created_at", "unknown")[:19]
        status_tag = (
            f" [{j.get('_status', '?')}]" if j.get("_status") else ""
        )
        exit_code = j.get("exit_code")
        if exit_code is not None:
            if exit_code == 0:
                exit_tag = f" {Theme.GREEN}✓{Theme.CLR}"
            else:
                exit_tag = f" {Theme.RED}✗ {exit_code}{Theme.CLR}"
        else:
            exit_tag = ""

        jtype = j.get("job_type", "process")
        type_label = "pbs" if jtype == "pbs" else "proc"

        if jtype == "pbs":
            job_id_display = j.get("pbs_job_id", "-")[:16]
        else:
            job_id_display = str(j.get("pid", "-"))[:16]

        print(
            f"  {j['id']:<10} {j.get('name', '-'):<16} {j['host']:<14} {type_label:<6} {job_id_display:<16} {cmd_display:<28} {created}{status_tag}{exit_tag}"
        )


def cmd_logs(args, ctx: ExecutionContext) -> None:
    job = find_job_record(args.job_id)
    if not job:
        Theme.error(f"Job not found: {args.job_id}")
        sys.exit(1)

    hosts_map, _ = load_hosts(ctx.config)
    host_info = hosts_map.get(job["host"])
    if not host_info:
        Theme.error(f"Host not found: {job['host']}")
        sys.exit(1)

    log_path = job.get("log_path")
    if not log_path:
        Theme.error("This job has no log file (foreground execution)")
        sys.exit(1)

    jtype = job.get("job_type", "process")

    # For PBS jobs, check status via qstat for exit code
    if jtype == "pbs":
        pbs_results = batch_check_pbs_jobs(
            ctx, host_info, [job] if job.get("pbs_job_id") else [],
        )
        r = pbs_results.get(job["id"], {})
        exit_code = r.get("exit_code")
        if exit_code is not None:
            if exit_code == 0:
                Theme.success("Exit code: 0")
            else:
                Theme.error(f"Exit code: {exit_code}")
            if not job.get("exit_code"):
                update_job_record(job["id"], {"exit_code": exit_code})
    elif job.get("_status") == "completed" or job.get("exit_code") is not None:
        if job.get("exit_code") is None:
            exit_code = get_job_exit_code(ctx, host_info, log_path)
            if exit_code is not None:
                update_job_record(job["id"], {"exit_code": exit_code})
                job["exit_code"] = exit_code
        exit_code = job.get("exit_code")
        if exit_code is not None:
            if exit_code == 0:
                Theme.success("Exit code: 0")
            else:
                Theme.error(f"Exit code: {exit_code}")

    tail_cmd = "tail"
    if args.follow:
        tail_cmd += " -f"
    if args.lines:
        tail_cmd += f" -n {args.lines}"
    tail_cmd += f" -- {rq(log_path)}"

    if ctx.dry_run:
        ctx.log(
            f"Dry-run: would run `tail` on {host_info.name}:{log_path}"
        )
        return

    try:
        ssh_cmd = (
            ["ssh", "-p", str(host_info.port)]
            + ctx._ssh_options(host_info)
            + [host_info.ssh_target(), tail_cmd]
        )
        subprocess.run(ssh_cmd)
    except KeyboardInterrupt:
        pass


def cmd_stop(args, ctx: ExecutionContext) -> None:
    job = find_job_record(args.job_id)
    if not job:
        Theme.error(f"Job not found: {args.job_id}")
        sys.exit(1)

    hosts_map, _ = load_hosts(ctx.config)
    host_info = hosts_map.get(job["host"])
    if not host_info:
        Theme.error(f"Host not found: {job['host']}")
        sys.exit(1)

    jtype = job.get("job_type", "process")

    if jtype == "pbs":
        pbs_job_id = job.get("pbs_job_id")
        if not pbs_job_id:
            Theme.error("No PBS job ID recorded for this job")
            sys.exit(1)
        qdel_cmd = f"qdel {shlex.quote(pbs_job_id)}"
        if ctx.dry_run:
            ctx.log(
                f"Dry-run: would run qdel {pbs_job_id} on {host_info.name}"
            )
            return
        ctx.log(f"Deleting PBS job {pbs_job_id} on {host_info.name}")
        try:
            ctx.run_ssh(host_info, qdel_cmd)
        except RuntimeError as e:
            if "255" in str(e):
                ctx.log("SSH connection lost, but qdel may have been sent")
            else:
                raise
        update_job_record(
            job["id"],
            {
                "completed_at": datetime.datetime.now().isoformat(),
                "scheduler_state": "F",
            },
        )
        Theme.success(f"PBS job {pbs_job_id} deleted (sft job {args.job_id})")
    else:
        pid = job.get("pid")
        if not pid:
            Theme.error("No PID recorded for this job")
            sys.exit(1)

        sig = "SIGKILL" if args.force else "SIGTERM"
        kill_cmd = f"kill -s {sig} {pid}"

        if ctx.dry_run:
            ctx.log(f"Dry-run: would send {sig} to PID {pid} on {host_info.name}")
            return

        ctx.log(f"Sending {sig} to PID {pid} on {host_info.name}")
        try:
            ctx.run_ssh(host_info, kill_cmd)
        except RuntimeError as e:
            if "255" in str(e):
                ctx.log(f"SSH connection lost, but signal may have been sent")
            else:
                raise

        if args.force:
            time.sleep(0.5)

        exit_code = get_job_exit_code(ctx, host_info, job.get("log_path", ""))
        if exit_code is not None:
            update_job_record(
                job["id"],
                {
                    "exit_code": exit_code,
                    "completed_at": datetime.datetime.now().isoformat(),
                },
            )
            Theme.success(f"Job {args.job_id} stopped (exit code: {exit_code})")
        else:
            Theme.success(f"Signal sent to job {args.job_id}")


def cmd_status(args, ctx: ExecutionContext) -> None:
    job = find_job_record(args.job_id)
    if not job:
        Theme.error(f"Job not found: {args.job_id}")
        sys.exit(1)

    hosts_map, _ = load_hosts(ctx.config)
    host_info = hosts_map.get(job["host"])

    jtype = job.get("job_type", "process")

    if jtype == "pbs" and host_info and job.get("pbs_job_id"):
        pbs_results = batch_check_pbs_jobs(ctx, host_info, [job])
        r = pbs_results.get(job["id"], {})
        status = r.get("status", "unknown")
        exit_code = r.get("exit_code")
        scheduler_state = r.get("scheduler_state")
        updates = {}
        if exit_code is not None:
            updates["exit_code"] = exit_code
        if scheduler_state:
            updates["scheduler_state"] = scheduler_state
        if updates:
            update_job_record(job["id"], updates)
    else:
        alive = False
        if host_info:
            pid = job.get("pid")
            if pid:
                alive = bool(_is_pid_alive(host_info, int(pid), ctx))

        exit_code = None
        if host_info:
            exit_code = get_job_exit_code(
                ctx, host_info, job.get("log_path", "")
            )

        if exit_code is not None:
            update_job_record(job["id"], {"exit_code": exit_code})

        status = "running" if alive else "completed"

    if exit_code is not None:
        status += f" (exit: {exit_code})"

    print(f"  Job ID:     {job['id']}")
    print(f"  Name:       {job.get('name', '-')}")
    print(f"  Host:       {job['host']}")
    print(f"  Type:       {jtype}")
    if jtype == "pbs":
        print(f"  PBS ID:     {job.get('pbs_job_id', '-')}")
        print(f"  Scheduler:  {job.get('scheduler_state', '-')}")
    else:
        print(f"  PID:        {job.get('pid', '-')}")
    print(f"  CWD:        {job.get('remote_cwd', '-')}")
    print(f"  Command:    {job.get('command', '-')}")
    print(f"  Log:        {job.get('log_path', '-')}")
    print(f"  Created:    {job.get('created_at', '-')}")
    print(f"  Status:     {status}")
    if job.get("fetch_patterns"):
        print(f"  Fetch:     {', '.join(job['fetch_patterns'])}")


def cmd_fetch(args, ctx: ExecutionContext) -> None:
    job = find_job_record(args.job_id)
    if not job:
        Theme.error(f"Job not found: {args.job_id}")
        sys.exit(1)

    fetch_patterns = job.get("fetch_patterns")
    if not fetch_patterns:
        Theme.error(
            f"Job {args.job_id} was not created with --fetch patterns"
        )
        Theme.info(
            "Hint",
            "Use 'sft run ... --fetch <pattern>' to enable result fetching",
        )
        sys.exit(1)

    hosts_map, _ = load_hosts(ctx.config)
    host_info = hosts_map.get(job["host"])
    if not host_info:
        Theme.error(f"Host not found: {job['host']}")
        sys.exit(1)

    remote_cwd = job.get("remote_cwd", "~")
    local_dest = getattr(args, "fetch_to", None) or job.get(
        "fetch_dest", os.getcwd()
    )

    job_alive = _is_job_alive(job, host_info, ctx)

    if job_alive and args.wait:
        log_path = job.get("log_path")
        if log_path:
            jtype = job.get("job_type", "process")
            if jtype == "pbs":
                pbs_id = job.get("pbs_job_id", "?")
                ctx.log(
                    f"Waiting for PBS job {pbs_id} on {job['host']} to complete..."
                )
            else:
                pid = job.get("pid", "?")
                ctx.log(
                    f"Waiting for job {args.job_id} (PID {pid} on {job['host']}) to complete..."
                )
            ctx.log(f"Tailing log: {host_info.name}:{log_path}")
            try:
                tail_cmd = f"tail -f -- {rq(log_path)}"
                ssh_cmd = (
                    ["ssh", "-p", str(host_info.port)]
                    + ctx._ssh_options(host_info)
                    + [host_info.ssh_target(), tail_cmd]
                )
                subprocess.run(ssh_cmd)
            except KeyboardInterrupt:
                pass
            ctx.log("Log stream ended")
        else:
            ctx.log(
                f"Waiting for job on {job['host']} to exit..."
            )
            while _is_job_alive(job, host_info, ctx):
                time.sleep(5)
            ctx.log("Job completed")
    elif job_alive:
        Theme.error(
            f"Job {args.job_id} is still running on {job['host']}"
        )
        Theme.info(
            "Hint",
            "Use 'sft fetch <job_id> --wait' to wait for completion, or 'sft logs <job_id> -f' to monitor",
        )
        sys.exit(1)

    if ctx.dry_run:
        for pattern in fetch_patterns:
            ctx.log(
                f"Dry-run: would fetch {host_info.name}:{remote_cwd}/{pattern} -> {local_dest}/",
                always=True,
            )
        return

    jtype = job.get("job_type", "process")
    if jtype == "pbs":
        pbs_results = batch_check_pbs_jobs(
            ctx, host_info, [job] if job.get("pbs_job_id") else [],
        )
        r = pbs_results.get(job["id"], {})
        exit_code = r.get("exit_code")
    else:
        exit_code = get_job_exit_code(
            ctx, host_info, job.get("log_path", "")
        )
    if exit_code is not None and exit_code != 0:
        Theme.warning(
            f"Job exited with code {exit_code}. Fetching partial results..."
        )

    fetch_results(host_info, remote_cwd, fetch_patterns, local_dest, ctx)
