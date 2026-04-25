"""Persistent state management for mounts and background jobs."""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import shlex
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from sft.config import HostInfo
from sft.context import ExecutionContext
from sft.pbs import parse_qstat_json, pbs_state_to_status


MOUNT_STATE_DIR = os.path.expanduser("~/.local/state/sft")
MOUNT_STATE_FILE = os.path.join(MOUNT_STATE_DIR, "mounts.json")
JOB_STATE_FILE = os.path.join(MOUNT_STATE_DIR, "jobs.json")
MARIMO_SESSIONS_DIR = os.path.join(MOUNT_STATE_DIR, "marimo-sessions")
JOB_MAX_RECORDS = 200
JOB_COMPLETED_TTL_HOURS = 24


def _ensure_state_dir() -> None:
    os.makedirs(MOUNT_STATE_DIR, exist_ok=True)


# --- Mount state ---


def load_mounts_state() -> List[Dict[str, Any]]:
    _ensure_state_dir()
    if not os.path.exists(MOUNT_STATE_FILE):
        return []
    try:
        with open(MOUNT_STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def save_mounts_state(mounts: List[Dict[str, Any]]) -> None:
    _ensure_state_dir()
    tmp_path = MOUNT_STATE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(mounts, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp_path, MOUNT_STATE_FILE)


def add_mount_record(record: Dict[str, Any]) -> None:
    mounts = load_mounts_state()
    mounts.append(record)
    save_mounts_state(mounts)


def remove_mount_record(local_mount: str) -> None:
    mounts = load_mounts_state()
    mounts = [m for m in mounts if m["local_mount"] != os.path.abspath(local_mount)]
    save_mounts_state(mounts)


def find_mount_record(local_mount: str) -> Optional[Dict[str, Any]]:
    mounts = load_mounts_state()
    target = os.path.abspath(local_mount)
    for m in mounts:
        if m["local_mount"] == target:
            return m
    return None


def is_mount_alive(local_mount: str) -> bool:
    abs_mount = os.path.abspath(local_mount)

    # Step 1: check if kernel still thinks it's mounted
    try:
        result = subprocess.run(
            ["findmnt", "--noheadings", "--output", "TARGET", abs_mount],
            capture_output=True,
            text=True,
            timeout=5,
        )
        mounted = result.returncode == 0 and result.stdout.strip() != ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        try:
            result = subprocess.run(
                ["mount"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            mounted = abs_mount in result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    if not mounted:
        return False

    # Step 2: verify the mount is actually usable (catches broken FUSE
    # mounts where sshfs died but the kernel entry remains — "Transport
    # endpoint is not connected").
    try:
        os.stat(abs_mount)
    except OSError:
        return False

    return True


def clean_stale_records() -> List[str]:
    mounts = load_mounts_state()
    stale = []
    fresh = []
    for m in mounts:
        if not is_mount_alive(m["local_mount"]):
            stale.append(m["local_mount"])
        else:
            fresh.append(m)
    if stale:
        save_mounts_state(fresh)
    return stale


def derive_mountpoint(host_name: str, remote_path: str) -> str:
    basename = os.path.basename(remote_path.rstrip("/"))
    return os.path.expanduser(f"~/mnt/{host_name}/{basename}")


# --- Job state ---


def load_jobs_state() -> List[Dict[str, Any]]:
    _ensure_state_dir()
    if not os.path.exists(JOB_STATE_FILE):
        return []
    try:
        with open(JOB_STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except (json.JSONDecodeError, IOError):
        return []


def _cleanup_old_jobs(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    now = datetime.datetime.now()
    fresh = []
    for job in jobs:
        status = job.get("_status", "running")
        if status == "running":
            fresh.append(job)
            continue
        created = job.get("completed_at") or job.get("created_at")
        if created:
            try:
                created_dt = datetime.datetime.fromisoformat(created)
                if (now - created_dt).total_seconds() < JOB_COMPLETED_TTL_HOURS * 3600:
                    fresh.append(job)
            except (ValueError, TypeError):
                fresh.append(job)
        else:
            fresh.append(job)
    if len(fresh) > JOB_MAX_RECORDS:
        fresh = fresh[-JOB_MAX_RECORDS:]
    return fresh


def save_jobs_state(jobs: List[Dict[str, Any]]) -> None:
    _ensure_state_dir()
    jobs = _cleanup_old_jobs(jobs)
    tmp_path = JOB_STATE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(jobs, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp_path, JOB_STATE_FILE)


def add_job_record(record: Dict[str, Any]) -> None:
    jobs = load_jobs_state()
    jobs.append(record)
    save_jobs_state(jobs)


def update_job_record(job_id: str, updates: Dict[str, Any]) -> None:
    jobs = load_jobs_state()
    for job in jobs:
        if job["id"] == job_id:
            job.update(updates)
            break
    save_jobs_state(jobs)


def find_job_record(job_id: str) -> Optional[Dict[str, Any]]:
    for job in load_jobs_state():
        if job["id"] == job_id:
            return job
    return None


def check_singleton(
    ctx: ExecutionContext,
    host: HostInfo,
    remote_cwd: str,
    singleton_key: Optional[str],
) -> bool:
    if not singleton_key:
        return False
    for job in load_jobs_state():
        if job.get("singleton_key") == singleton_key and job["host"] == host.name:
            if is_job_alive(ctx, host, job["pid"], job.get("pid_started_at")):
                return True
    return False


def is_job_alive(
    ctx: ExecutionContext,
    host: HostInfo,
    pid: int,
    pid_started_at: Optional[str] = None,
) -> bool:
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return False
    try:
        if pid_started_at:
            check_cmd = f"ps -o lstart= -p {pid} 2>/dev/null"
            output = ctx.run_ssh(
                host,
                check_cmd,
                capture=True,
                allow_dry_run_execute=True,
                silent=True,
            )
            if not output:
                return False
            remote_start = " ".join(output.strip().split())
            expected_start = " ".join(pid_started_at.split())
            return remote_start == expected_start
        else:
            ctx.run_ssh(
                host,
                f"kill -0 {pid}",
                capture=True,
                allow_dry_run_execute=True,
                silent=True,
            )
            return True
    except RuntimeError:
        return False


def check_remote_pid(
    ctx: ExecutionContext,
    host: HostInfo,
    pid: int,
    pid_started_at: Optional[str] = None,
) -> str:
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return "unknown"
    try:
        if pid_started_at:
            check_cmd = f"ps -o lstart= -p {pid} 2>/dev/null"
            output = ctx.run_ssh(
                host,
                check_cmd,
                capture=True,
                allow_dry_run_execute=True,
                silent=True,
            )
            if not output:
                return "done"
            remote_start = " ".join(output.strip().split())
            expected_start = " ".join(pid_started_at.split())
            if remote_start == expected_start:
                return "running"
            return "done"
        else:
            ctx.run_ssh(
                host,
                f"kill -0 {pid} 2>/dev/null",
                capture=True,
                allow_dry_run_execute=True,
                silent=True,
            )
            return "running"
    except RuntimeError as e:
        err_msg = str(e).lower()
        if any(
            w in err_msg
            for w in (
                "connection refused",
                "connection closed",
                "network is unreachable",
                "name or service not known",
                "operation timed out",
            )
        ):
            return "unknown"
        return "done"


def get_job_exit_code(
    ctx: ExecutionContext,
    host: HostInfo,
    log_path: str,
) -> Optional[int]:
    exit_file = f"{log_path}.exit".replace("~", "$HOME")
    try:
        output = ctx.run_ssh(
            host,
            f"bash -c 'cat {exit_file} 2>/dev/null'",
            capture=True,
            allow_dry_run_execute=True,
            silent=True,
        )
        if output and output.strip().isdigit():
            return int(output.strip())
    except RuntimeError:
        pass
    return None


def batch_check_remote_pids(
    ctx: ExecutionContext,
    host_info: HostInfo,
    checks: List[Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """Check all job PIDs and exit codes on one host in a single SSH call.

    Each check dict must have:
      - pid: int
      - pid_started_at: str | None
      - exit_file: str (remote path to .exit file)

    Returns {pid: {"status": str, "exit_code": int|None, "pid_started_at": str|None}}
    where status is "running", "done", or "unknown".
    """
    if not checks:
        return {}

    lines = ["set -e"]
    for i, c in enumerate(checks):
        pid = c["pid"]
        exit_file = c["exit_file"].replace("~", "$HOME")
        pid_started_at = c.get("pid_started_at")

        if pid_started_at:
            lines.append(f"s{i}=$(ps -o lstart= -p {pid} 2>/dev/null) || true")
            lines.append(f'echo "STATUS:{pid}:${{s{i}:-GONE}}"')
        else:
            lines.append(
                f"if kill -0 {pid} 2>/dev/null; then "
                f'echo "STATUS:{pid}:ALIVE"; '
                f'else echo "STATUS:{pid}:GONE"; fi'
            )
        lines.append(f"e{i}=$(cat {exit_file} 2>/dev/null) || true")
        lines.append(f'echo "EXIT:{pid}:${{e{i}:-NONE}}"')

    batch_cmd = "\n".join(lines)

    try:
        output = ctx.run_ssh(
            host_info,
            batch_cmd,
            capture=True,
            allow_dry_run_execute=True,
            silent=True,
        )
    except RuntimeError as e:
        err_msg = str(e).lower()
        if any(
            w in err_msg
            for w in (
                "connection refused",
                "connection closed",
                "network is unreachable",
                "name or service not known",
                "operation timed out",
            )
        ):
            return {
                c["pid"]: {
                    "status": "unknown",
                    "exit_code": None,
                    "pid_started_at": c.get("pid_started_at"),
                }
                for c in checks
            }
        return {
            c["pid"]: {
                "status": "done",
                "exit_code": None,
                "pid_started_at": c.get("pid_started_at"),
            }
            for c in checks
        }

    results: Dict[int, Dict[str, Any]] = {}
    status_map: Dict[int, str] = {}

    if not output:
        for c in checks:
            results[c["pid"]] = {
                "status": "done",
                "exit_code": None,
                "pid_started_at": c.get("pid_started_at"),
            }
        return results

    for line in output.strip().split("\n"):
        if line.startswith("STATUS:"):
            parts = line.split(":", 2)
            if len(parts) == 3:
                pid = int(parts[1])
                raw_start = parts[2]
                if raw_start == "GONE":
                    status_map[pid] = "done"
                elif raw_start == "ALIVE":
                    status_map[pid] = "running"
                else:
                    expected = None
                    for c in checks:
                        if c["pid"] == pid:
                            expected = c.get("pid_started_at")
                            break
                    if expected and " ".join(raw_start.split()) == " ".join(
                        expected.split()
                    ):
                        status_map[pid] = "running"
                    else:
                        status_map[pid] = "done"
        elif line.startswith("EXIT:"):
            parts = line.split(":", 2)
            if len(parts) == 3:
                pid = int(parts[1])
                raw_exit = parts[2]
                exit_code = (
                    int(raw_exit)
                    if raw_exit != "NONE" and raw_exit.strip().isdigit()
                    else None
                )
                if pid not in results:
                    results[pid] = {
                        "status": "unknown",
                        "exit_code": None,
                        "pid_started_at": None,
                    }
                results[pid]["exit_code"] = exit_code

    for c in checks:
        pid = c["pid"]
        if pid not in results:
            results[pid] = {
                "status": "done",
                "exit_code": None,
                "pid_started_at": c.get("pid_started_at"),
            }
        if pid in status_map:
            results[pid]["status"] = status_map[pid]
        if "pid_started_at" not in results[pid]:
            results[pid]["pid_started_at"] = c.get("pid_started_at")

    return results


# --- PBS job backend ---


def batch_check_pbs_jobs(
    ctx: ExecutionContext,
    host_info: HostInfo,
    job_records: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Check PBS job status via ``qstat -xf -F json`` in a single SSH call.

    Each job record must have ``pbs_job_id``.

    Returns ``{job_id (sft): {"status": str, "exit_code": int|None,
    "scheduler_state": str, "pbs_job_id": str}}``.
    """
    if not job_records:
        return {}

    pbs_ids = [j["pbs_job_id"] for j in job_records if j.get("pbs_job_id")]
    if not pbs_ids:
        return {}

    qstat_cmd = "qstat -xf -F json " + " ".join(
        shlex.quote(pbs_id) for pbs_id in pbs_ids
    )

    # Map sft job ID -> pbs job ID for result routing
    sft_to_pbs: Dict[str, str] = {
        j["id"]: j["pbs_job_id"] for j in job_records if j.get("pbs_job_id")
    }

    try:
        raw = ctx.run_ssh(
            host_info,
            qstat_cmd,
            capture=True,
            allow_dry_run_execute=True,
            silent=True,
        )
    except RuntimeError as e:
        err_msg = str(e).lower()
        is_network = any(
            w in err_msg
            for w in (
                "connection refused",
                "connection closed",
                "network is unreachable",
                "name or service not known",
                "operation timed out",
            )
        )
        base_status = "unknown" if is_network else "done"
        return {
            j["id"]: {
                "status": base_status,
                "exit_code": None,
                "scheduler_state": None,
                "pbs_job_id": j["pbs_job_id"],
            }
            for j in job_records
        }

    parsed = parse_qstat_json(raw or "")
    results: Dict[str, Dict[str, Any]] = {}

    for j in job_records:
        jid = j["id"]
        pbs_id = j.get("pbs_job_id", "")
        info = parsed.get(pbs_id)
        if info:
            status = pbs_state_to_status(info.state)
            exit_code = info.exit_status if info.state == "F" else None
            results[jid] = {
                "status": status,
                "exit_code": exit_code,
                "scheduler_state": info.state,
                "pbs_job_id": pbs_id,
            }
        else:
            # Job not found in qstat — likely purged from history
            results[jid] = {
                "status": "done",
                "exit_code": None,
                "scheduler_state": None,
                "pbs_job_id": pbs_id,
            }

    return results


def batch_check_jobs(
    ctx: ExecutionContext,
    host_info: HostInfo,
    jobs: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Unified dispatcher: check both process and PBS jobs for one host.

    Returns ``{job_id: {"status": str, "exit_code": int|None, ...}}``.

    For process jobs the dict also contains ``pid_started_at``.
    For PBS jobs it contains ``scheduler_state`` and ``pbs_job_id``.
    """
    process_jobs: List[Dict[str, Any]] = []
    pbs_jobs: List[Dict[str, Any]] = []

    for j in jobs:
        jtype = j.get("job_type", "process")
        if jtype == "pbs":
            pbs_jobs.append(j)
        else:
            process_jobs.append(j)

    results: Dict[str, Dict[str, Any]] = {}

    if process_jobs:
        checks = []
        skipped: List[Dict[str, Any]] = []
        for j in process_jobs:
            try:
                pid = int(j["pid"])
            except (ValueError, TypeError, KeyError):
                skipped.append(j)
                continue
            log_path = j.get("log_path", "")
            exit_file = f"{log_path}.exit" if log_path else ""
            checks.append(
                {
                    "pid": pid,
                    "pid_started_at": j.get("pid_started_at"),
                    "exit_file": exit_file,
                }
            )
        # Malformed records → unknown status
        for j in skipped:
            results[j["id"]] = {
                "status": "unknown",
                "exit_code": None,
                "pid_started_at": None,
            }
        if checks:
            pid_results = batch_check_remote_pids(ctx, host_info, checks)
            for j in process_jobs:
                if j["id"] in results:
                    continue  # already marked unknown
                pid = int(j["pid"])
                r = pid_results.get(
                    pid,
                    {
                        "status": "unknown",
                        "exit_code": None,
                    },
                )
                results[j["id"]] = {
                    "status": r["status"],
                    "exit_code": r.get("exit_code"),
                    "pid_started_at": r.get("pid_started_at"),
                }

    if pbs_jobs:
        pbs_results = batch_check_pbs_jobs(ctx, host_info, pbs_jobs)
        results.update(pbs_results)

    return results


# --- Marimo session state ---


def _ensure_marimo_dir() -> None:
    os.makedirs(MARIMO_SESSIONS_DIR, exist_ok=True)


def _session_file(session_id: str) -> str:
    return os.path.join(MARIMO_SESSIONS_DIR, f"{session_id}.json")


def save_session(session: Dict[str, Any]) -> str:
    """Save a marimo session. Uses session['id'] or auto-generates one."""
    _ensure_marimo_dir()
    if "id" not in session:
        tag = session.get("host", "unknown")
        date = datetime.datetime.now().strftime("%y%m%d")
        short = session.get("remote_path", "").rstrip("/").split("/")[-1] or "root"
        session["id"] = f"{tag}-{short}-{date}"
    path = _session_file(session["id"])
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(session, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp, path)
    return session["id"]


def load_session(session_id: str) -> Optional[Dict[str, Any]]:
    path = _session_file(session_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def list_sessions() -> List[Dict[str, Any]]:
    _ensure_marimo_dir()
    sessions = []
    for name in sorted(os.listdir(MARIMO_SESSIONS_DIR)):
        if name.endswith(".json"):
            sid = name[:-5]
            s = load_session(sid)
            if s:
                sessions.append(s)
    return sessions


def remove_session(session_id: str) -> None:
    path = _session_file(session_id)
    if os.path.exists(path):
        os.unlink(path)
