"""CLI argument parsing, command dispatch, and main entry point."""

from __future__ import annotations

import argparse
import sys
import textwrap

from sft.commands import (
    cmd_fetch,
    cmd_jobs,
    cmd_logs,
    cmd_mount,
    cmd_mounts,
    cmd_run,
    cmd_status,
    cmd_stop,
    cmd_submit,
    cmd_sync_run,
    cmd_umount,
)
from sft.config import load_hosts, parse_target
from sft.context import ExecutionContext
from sft.transfer import perform_transfer


def _print_hosts() -> None:
    ctx = ExecutionContext(dry_run=False, verbose=False)
    hosts, _ = load_hosts(ctx.config)
    for name, host in hosts.items():
        aliases = ", ".join(host.aliases) if host.aliases else "(no aliases)"
        print(f"{name}\t{aliases}\t{host.hostname}")


# --- Argument parsing ---


def parse_args() -> argparse.Namespace:
    subcommands = {
        "mount",
        "umount",
        "mounts",
        "run",
        "sync-run",
        "jobs",
        "logs",
        "stop",
        "fetch",
        "status",
        "submit",
    }

    non_option_args = []
    global_flags = []
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ("--dry-run", "-v", "--verbose", "--list-hosts"):
            global_flags.append(arg)
        else:
            non_option_args.append(arg)
        i += 1

    if non_option_args and non_option_args[0] in subcommands:
        sub_cmd = non_option_args[0]
        subcommand_argv = list(non_option_args)
        executable_flags = [
            f for f in global_flags if f in ("--dry-run", "-v", "--verbose")
        ]
        for flag in reversed(executable_flags):
            subcommand_argv.insert(1, flag)

        parser = argparse.ArgumentParser(
            description="Smart file transfer + remote mount helper (sft)"
        )
        parser.add_argument(
            "--list-hosts",
            action="store_true",
            help="List configured hosts and exit",
        )
        subparsers = parser.add_subparsers(dest="command")

        # --- Parent parsers ---
        global_parent = argparse.ArgumentParser(add_help=False)
        global_parent.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview actions without executing",
        )
        global_parent.add_argument(
            "-v", "--verbose", action="store_true", help=argparse.SUPPRESS
        )

        run_parent = argparse.ArgumentParser(add_help=False)
        run_parent.add_argument(
            "--cwd",
            default=None,
            help="Working directory on remote (default: target path)",
        )
        run_parent.add_argument(
            "--background",
            action="store_true",
            help="Run in background (nohup)",
        )
        run_parent.add_argument(
            "--log",
            default=None,
            help="Remote log file path (background mode)",
        )
        run_parent.add_argument(
            "--name", default=None, help="Job name for identification"
        )
        run_parent.add_argument(
            "--singleton",
            action="store_true",
            help="Fail if a job is already running in the same directory",
        )
        run_parent.add_argument(
            "--env",
            action="append",
            default=None,
            help="Environment variable (KEY=VALUE), repeatable",
        )
        run_parent.add_argument(
            "--retry",
            type=int,
            default=0,
            help="Auto-retry on failure (max attempts, 0=disabled)",
        )
        run_parent.add_argument(
            "--env-sync-mode",
            choices=["full-flake", "stub", "none"],
            default=None,
            help="How to sync env (plugin-specific, default: full-flake)",
        )
        run_parent.add_argument(
            "--no-auto-env",
            action="store_true",
            help="Do not auto-activate environment",
        )
        run_parent.add_argument(
            "--fetch",
            action="append",
            default=None,
            help="Remote path/glob to fetch after execution (repeatable)",
        )
        run_parent.add_argument(
            "--fetch-to",
            default=None,
            help="Local directory to fetch results into (default: project root or cwd)",
        )
        run_parent.add_argument(
            "--fetch-auto",
            action="store_true",
            help="Auto-detect and fetch new/modified files after execution",
        )
        run_parent.add_argument(
            "--build-timeout",
            type=int,
            default=0,
            help="Max seconds for environment build (0=unlimited)",
        )

        job_parent = argparse.ArgumentParser(add_help=False)
        job_parent.add_argument("job_id", help="Job ID")

        # --- Subcommands ---
        mount_parser = subparsers.add_parser(
            "mount",
            help="Mount a remote directory locally via sshfs",
            parents=[global_parent],
        )
        mount_parser.add_argument("remote", help="Remote path (host:/path)")
        mount_parser.add_argument(
            "local_mount",
            nargs="?",
            default=None,
            help="Local mount point (default: ~/mnt/<host>/<dir>)",
        )
        mount_parser.add_argument(
            "--mkdir",
            action="store_true",
            help="Create mountpoint if it doesn't exist",
        )
        mount_parser.add_argument(
            "--readonly", action="store_true", help="Mount as read-only"
        )
        mount_parser.add_argument(
            "--foreground",
            action="store_true",
            help="Run in foreground (debug)",
        )
        mount_parser.add_argument(
            "--allow-other",
            action="store_true",
            help="Allow other users to access",
        )
        mount_parser.add_argument(
            "--no-cache",
            action="store_true",
            help="Disable kernel cache",
        )

        umount_parser = subparsers.add_parser(
            "umount",
            help="Unmount a remote directory",
            parents=[global_parent],
        )
        umount_parser.add_argument("local_mount", help="Local mount point to unmount")

        subparsers.add_parser(
            "mounts", help="List active mounts", parents=[global_parent]
        )

        run_parser = subparsers.add_parser(
            "run",
            help="Execute a command on a remote host",
            parents=[global_parent, run_parent],
        )
        run_parser.add_argument(
            "target",
            help="Remote host or target (host or host:/path)",
        )
        run_parser.add_argument(
            "--sync-env-from",
            default=None,
            help="Local directory to sync environment from",
        )
        run_parser.add_argument(
            "--no-sync-env",
            action="store_true",
            help="Do not sync environment to remote",
        )
        run_parser.add_argument(
            "run_command",
            nargs="+",
            help="Command to execute (use -- before command if needed)",
        )

        sync_run_parser = subparsers.add_parser(
            "sync-run",
            help="Sync files to remote host, then execute a command",
            parents=[global_parent, run_parent],
        )
        sync_run_parser.add_argument("src_dir", help="Local source directory")
        sync_run_parser.add_argument("target", help="Remote target (host:/path)")
        sync_mode = sync_run_parser.add_mutually_exclusive_group()
        sync_mode.add_argument(
            "--include",
            action="append",
            default=None,
            help="File to include (relative to src_dir), repeatable",
        )
        sync_mode.add_argument(
            "--project-sync",
            action="store_true",
            help="Sync entire project directory (excluding .git, .venv, etc.)",
        )
        sync_run_parser.add_argument(
            "--exclude",
            action="append",
            default=None,
            help="Additional exclude pattern (with --project-sync), repeatable",
        )
        sync_run_parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete files at destination not present at source (with --project-sync)",
        )
        sync_run_parser.add_argument(
            "--diff",
            action="store_true",
            help="Show changes since last sync without syncing",
        )
        sync_run_parser.add_argument(
            "--post-sync",
            action="append",
            default=None,
            help="Command to run after sync, before env setup (repeatable)",
        )
        sync_run_parser.add_argument(
            "--reinstall-pkg",
            action="append",
            default=None,
            help="Reinstall a Python package via uv sync --reinstall-package (repeatable)",
        )
        sync_run_parser.add_argument(
            "run_command",
            nargs="*",
            default=[],
            help="Command to execute after sync",
        )

        jobs_parser = subparsers.add_parser(
            "jobs", help="List background jobs", parents=[global_parent]
        )
        jobs_parser.add_argument(
            "--host", default=None, help="Filter by host name or alias"
        )
        jobs_parser.add_argument(
            "--status",
            choices=["running", "completed"],
            default=None,
            help="Filter by job status",
        )
        jobs_parser.add_argument(
            "--name",
            default=None,
            help="Filter by job name (case-insensitive substring)",
        )
        jobs_parser.add_argument(
            "--after",
            default=None,
            help="Show jobs created after ISO datetime",
        )
        jobs_parser.add_argument(
            "--before",
            default=None,
            help="Show jobs created before ISO datetime",
        )

        logs_parser = subparsers.add_parser(
            "logs",
            help="Tail logs of a background job",
            parents=[global_parent, job_parent],
        )
        logs_parser.add_argument(
            "-f",
            "--follow",
            action="store_true",
            help="Follow log output (tail -f)",
        )
        logs_parser.add_argument(
            "-n",
            "--lines",
            type=int,
            default=None,
            help="Number of lines to show",
        )

        stop_parser = subparsers.add_parser(
            "stop",
            help="Stop a background job",
            parents=[global_parent, job_parent],
        )
        stop_parser.add_argument(
            "--force",
            action="store_true",
            help="Send SIGKILL instead of SIGTERM",
        )

        fetch_parser = subparsers.add_parser(
            "fetch",
            help="Fetch results from a completed background job",
            parents=[global_parent, job_parent],
        )
        fetch_parser.add_argument(
            "--wait",
            action="store_true",
            help="Wait for job to complete before fetching",
        )
        fetch_parser.add_argument(
            "--fetch-to",
            default=None,
            help="Override local destination directory",
        )

        subparsers.add_parser(
            "status",
            help="Show detailed status of a background job",
            parents=[global_parent, job_parent],
        )

        submit_parser = subparsers.add_parser(
            "submit",
            help="Submit a PBS script to a cluster scheduler",
            parents=[global_parent],
        )
        submit_parser.add_argument(
            "script",
            help="PBS script file to submit",
        )
        submit_parser.add_argument(
            "target",
            help="Remote target (host or host:/path)",
        )
        submit_parser.add_argument(
            "--name",
            default=None,
            help="Job name (maps to qsub -N)",
        )
        submit_parser.add_argument(
            "--queue",
            "-q",
            default=None,
            help="Target queue (maps to qsub -q)",
        )
        submit_parser.add_argument(
            "--resources",
            "-l",
            default=None,
            help="Resource list, e.g. 'nodes=1:ppn=128,walltime=24:00:00'",
        )
        submit_parser.add_argument(
            "--env",
            action="append",
            default=None,
            help="Environment variable (KEY=VALUE), repeatable (maps to qsub -v)",
        )
        submit_parser.add_argument(
            "--log",
            default=None,
            help="Override output log path (maps to qsub -o)",
        )
        submit_parser.add_argument(
            "--cwd",
            default=None,
            help="Working directory on remote host",
        )

        # Let plugins register their own subcommand parsers
        from sft.plugins import get_subcommand_parsers

        for name, parser_fn in get_subcommand_parsers().items():
            parser_fn(subparsers, global_parent)

        args = parser.parse_args(subcommand_argv)

        if args.list_hosts:
            _print_hosts()
            sys.exit(0)

        if args.command is None:
            parser.print_help()
            sys.exit(0)

        return args

    parser = argparse.ArgumentParser(
        description="Smart file transfer helper (sft)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Additional commands:
              sft mount <host:/path> [local_mount]  Mount a remote directory locally
              sft umount <local_mount>              Unmount a remote directory
              sft mounts                            List active mounts
              sft run <host> <command>              Run command in current project on remote host
              sft run <host:/path> <command>        Run command at specific remote path
              sft sync-run <src_dir> <host:/path>   Sync files then execute remotely
              sft submit <script> <host>            Submit a PBS script to a cluster
              sft jobs                              List background jobs
              sft logs <job_id>                     Tail logs of a background job
              sft stop <job_id>                     Stop a background job
              sft fetch <job_id>                    Fetch results from a completed job
              sft status <job_id>                   Show detailed status of a background job

            Use 'sft <command> --help' for more information on a subcommand.
            """
        ),
    )
    parser.add_argument(
        "src",
        nargs="?",
        help="Source path (local or host:path)",
    )
    parser.add_argument(
        "dst",
        nargs="?",
        help="Destination path (local or host:path)",
    )
    compression = parser.add_mutually_exclusive_group()
    compression.add_argument(
        "--force-compress",
        action="store_true",
        help="Force tar+zstd compression",
    )
    compression.add_argument(
        "--no-compress",
        action="store_true",
        help="Disable automatic compression",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the transfer",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    parser.add_argument(
        "--list-hosts",
        action="store_true",
        help="List configured hosts and exit",
    )
    args = parser.parse_args()
    if args.list_hosts:
        _print_hosts()
        sys.exit(0)
    if not args.src or not args.dst:
        parser.error("src and dst are required when not using --list-hosts")
    return args


# --- Main ---


COMMANDS = {
    "mount": cmd_mount,
    "umount": cmd_umount,
    "mounts": cmd_mounts,
    "run": cmd_run,
    "sync-run": cmd_sync_run,
    "jobs": cmd_jobs,
    "logs": cmd_logs,
    "stop": cmd_stop,
    "fetch": cmd_fetch,
    "status": cmd_status,
    "submit": cmd_submit,
}


def main() -> None:
    # Discover plugins before parsing args
    from sft.plugins import discover_plugins, get_subcommands

    discover_plugins()

    args = parse_args()
    ctx = ExecutionContext(
        dry_run=getattr(args, "dry_run", False),
        verbose=getattr(args, "verbose", False),
    )

    if hasattr(args, "command") and args.command:
        # Check plugin subcommands first
        plugin_commands = get_subcommands()
        if args.command in plugin_commands:
            plugin_commands[args.command](args, ctx)
            return

        handler = COMMANDS.get(args.command)
        if handler:
            handler(args, ctx)
        return

    hosts, alias_map = load_hosts(ctx.config)
    src = parse_target(args.src, hosts, alias_map)
    dst = parse_target(args.dst, hosts, alias_map)
    perform_transfer(src, dst, args, ctx)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
