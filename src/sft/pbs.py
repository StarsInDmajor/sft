"""PBS (Portable Batch System) helpers: qstat parsing, qsub building.

Pure functions where possible for testability. SSH-dependent functions
are in state.py (batch_check_pbs_jobs).
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# PBS job states (PBS Pro 23.06.06)
STATE_QUEUED = "Q"
STATE_RUNNING = "R"
STATE_FINISHED = "F"
STATE_HELD = "H"
STATE_WAITING = "W"
STATE_SUSPENDED = "S"
STATE_MOVED = "M"
STATE_EXPIRED = "E"

# Map PBS single-letter states to human-readable status
PBS_STATE_MAP: Dict[str, str] = {
    STATE_QUEUED: "queued",
    STATE_RUNNING: "running",
    STATE_FINISHED: "done",
    STATE_HELD: "held",
    STATE_WAITING: "queued",  # waiting to be staged
    STATE_SUSPENDED: "held",
    STATE_MOVED: "done",
    STATE_EXPIRED: "done",
}


@dataclass
class PbsJobInfo:
    """Parsed info from a single PBS job via qstat JSON output."""

    job_id: str  # e.g. "320659.sirius"
    name: str
    state: str  # single-letter PBS state (Q/R/F/H/...)
    exit_status: Optional[int] = None  # NOTE: JSON key is "Exit_status" (capital E)
    output_path: Optional[str] = None  # raw, may include "hostname:" prefix
    error_path: Optional[str] = None
    join_output: bool = False  # whether stdout+stderr joined
    ctime: Optional[str] = None  # creation time
    stime: Optional[str] = None  # start time
    mtime: Optional[str] = None  # modification time
    queue: Optional[str] = None
    session_id: Optional[int] = None


def parse_qstat_json(raw: str) -> Dict[str, PbsJobInfo]:
    """Parse ``qstat -xf -F json <job_ids>`` output.

    Returns a dict keyed by PBS job ID (e.g. ``"320659.sirius"``).

    Empty response (unknown jobs) returns an empty dict — callers
    should treat missing keys as unknown jobs.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    jobs_data = data.get("Jobs", {})
    if not jobs_data:
        return {}

    results: Dict[str, PbsJobInfo] = {}
    for job_key, attrs in jobs_data.items():
        # job_key is like "0:320659.sirius" or just "320659.sirius"
        # Strip the leading index prefix if present
        job_id = job_key.split(":", 1)[-1] if ":" in job_key else job_key

        join_val = attrs.get("Join_Path", "n")
        results[job_id] = PbsJobInfo(
            job_id=job_id,
            name=attrs.get("Job_Name", ""),
            state=attrs.get("job_state", ""),
            # NOTE: capital E in JSON output — "Exit_status", not "exit_status"
            exit_status=attrs.get("Exit_status"),
            output_path=attrs.get("Output_Path"),
            error_path=attrs.get("Error_Path"),
            join_output=(str(join_val).lower() in ("oe", "eo")),
            ctime=attrs.get("ctime"),
            stime=attrs.get("stime"),
            mtime=attrs.get("mtime"),
            queue=attrs.get("queue"),
            session_id=attrs.get("session_id"),
        )

    return results


def parse_output_path(raw: Optional[str]) -> Optional[str]:
    """Strip the ``hostname:`` prefix from a PBS Output_Path value.

    Example: ``"sirius:/home/user/job.out"`` → ``"/home/user/job.out"``

    Returns *None* if *raw* is *None*.
    """
    if not raw:
        return None
    if ":" in raw:
        # Format is "hostname:/path" — strip the hostname prefix
        return raw.split(":", 1)[1]
    return raw


def pbs_state_to_status(state: str) -> str:
    """Map a PBS single-letter state to a human-readable status string.

    Returns ``"unknown"`` for unrecognised states.
    """
    return PBS_STATE_MAP.get(state, "unknown")


def build_qsub_command(
    script_path: str,
    name: Optional[str] = None,
    output_path: Optional[str] = None,
    error_path: Optional[str] = None,
    join_output: bool = True,
    env_vars: Optional[List[str]] = None,
    queue: Optional[str] = None,
    resources: Optional[str] = None,
) -> str:
    """Build a ``qsub`` command string for remote execution via SSH.

    Parameters
    ----------
    script_path
        Remote path to the PBS script file (already uploaded).
    name
        Job name (``-N``).
    output_path
        Stdout log path (``-o``).
    error_path
        Stderr log path (``-e``).
    join_output
        Merge stderr into stdout (``-j oe``). Default *True*.
    env_vars
        List of ``KEY=VALUE`` strings → ``-v VAR1=val1,VAR2=val2``.
    queue
        Target queue (``-q``).
    resources
        Resource list string (``-l``), e.g. ``"nodes=1:ppn=128,walltime=24:00:00"``.
    working_dir
        Working directory. Prepends a ``cd`` line to the script content
        (PBS Pro has no standard ``qsub`` flag for this).

    Returns
    -------
    str
        The full ``qsub`` command ready for ``ctx.run_ssh()``.
    """
    parts = ["qsub"]

    if name:
        parts.extend(["-N", shlex.quote(name)])

    if join_output and not error_path:
        parts.extend(["-j", "oe"])

    if output_path:
        parts.extend(["-o", shlex.quote(output_path)])

    if error_path:
        parts.extend(["-e", shlex.quote(error_path)])

    if env_vars:
        # qsub -v VAR1=val1,VAR2=val2
        var_str = ",".join(env_vars)
        parts.extend(["-v", shlex.quote(var_str)])

    if queue:
        parts.extend(["-q", shlex.quote(queue)])

    if resources:
        parts.extend(["-l", shlex.quote(resources)])

    # NOTE: PBS Pro has no -w or -d flag for working directory.
    # The caller (submit.py) prepends "cd <dir>" to the script content instead.

    parts.append(shlex.quote(script_path))
    return " ".join(parts)


def parse_qsub_output(raw: str) -> Optional[str]:
    """Parse the job ID from ``qsub`` stdout.

    Example output: ``"320659.sirius"``

    Returns *None* if parsing fails.
    """
    line = raw.strip()
    if "." in line:
        return line
    # Some PBS versions output just a number
    if line.isdigit():
        return line
    return None
