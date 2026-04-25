"""Integration tests: cmd_run and cmd_sync_run with mocked SSH/subprocess."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from sft.config import HostInfo, ParsedTarget, SftConfig
from sft.context import ExecutionContext
from sft.env import EnvSource


WSL_RS = HostInfo(
    name="testhost",
    hostname="10.0.0.1",
    port=2222,
    user="testuser",
    aliases=["rs"],
    extra_options={},
)

HOSTS_MAP = {"testhost": WSL_RS}
ALIAS_MAP = {"testhost": "testhost", "rs": "testhost"}


def _make_ctx(**overrides):
    ctx = ExecutionContext(dry_run=False, verbose=False, config=SftConfig())
    ctx._ensure_ssh_control_master = MagicMock()
    ctx._ssh_options = lambda h: ["-o", "ConnectTimeout=5"]
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


def _run_args(**overrides):
    defaults = dict(
        target="testhost",
        run_command=["echo", "hello"],
        cwd=None,
        background=False,
        singleton=False,
        env=None,
        retry=0,
        fetch=None,
        fetch_to=None,
        fetch_auto=False,
        no_sync_env=True,
        no_auto_env=True,
        sync_env_from=None,
        env_sync_mode=None,
        log=None,
        name=None,
        dry_run=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _sync_run_args(**overrides):
    defaults = dict(
        src_dir="/tmp/project",
        target="testhost:/home/testuser/project",
        run_command=["python", "train.py"],
        project_sync=True,
        include=None,
        exclude=None,
        delete=False,
        cwd=None,
        background=False,
        singleton=False,
        env=None,
        retry=0,
        fetch=None,
        fetch_to=None,
        env_sync_mode="none",
        no_auto_env=True,
        no_sync_env=True,
        sync_env_from=None,
        log=None,
        name=None,
        dry_run=False,
        diff=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestCmdRunForeground(unittest.TestCase):
    """Test cmd_run in foreground mode with mocked SSH."""

    @patch("sft.commands.run.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_basic_foreground_run(self, mock_load_hosts, mock_subprocess_run):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value=None)
        args = _run_args()

        from sft.cli import cmd_run

        cmd_run(args, ctx)

        mock_subprocess_run.assert_called_once()
        ssh_cmd = mock_subprocess_run.call_args[0][0]
        self.assertEqual(ssh_cmd[0], "ssh")
        self.assertIn(str(WSL_RS.port), ssh_cmd)
        self.assertIn("echo hello", ssh_cmd[-1])

    @patch("sft.commands.run.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_foreground_passes_cwd(self, mock_load_hosts, mock_subprocess_run):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        args = _run_args(
            target="testhost:/data/experiment",
            no_sync_env=True,
            no_auto_env=True,
            cwd="/data/experiment",
        )

        from sft.cli import cmd_run

        cmd_run(args, ctx)

        ssh_cmd = mock_subprocess_run.call_args[0][0]
        remote_cmd = ssh_cmd[-1]
        self.assertIn("/data/experiment", remote_cmd)

    @patch("sft.commands.run.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_foreground_nonzero_exit(self, mock_load_hosts, mock_subprocess_run):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=42)

        ctx = _make_ctx()
        args = _run_args()

        from sft.cli import cmd_run

        with self.assertRaises(SystemExit) as cm:
            cmd_run(args, ctx)
        self.assertEqual(cm.exception.code, 42)

    @patch("sft.commands.run.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_foreground_with_env_exports(self, mock_load_hosts, mock_subprocess_run):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        args = _run_args(env=["FOO=bar", "BAZ=qux"])

        from sft.cli import cmd_run

        cmd_run(args, ctx)

        ssh_cmd = mock_subprocess_run.call_args[0][0]
        remote_cmd = ssh_cmd[-1]
        self.assertIn("FOO", remote_cmd)
        self.assertIn("bar", remote_cmd)
        self.assertIn("BAZ", remote_cmd)
        self.assertIn("qux", remote_cmd)

    @patch("sft.commands.run.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_foreground_with_retry(self, mock_load_hosts, mock_subprocess_run):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        args = _run_args(retry=3)

        from sft.cli import cmd_run

        cmd_run(args, ctx)

        ssh_cmd = mock_subprocess_run.call_args[0][0]
        remote_cmd = ssh_cmd[-1]
        self.assertIn("seq 1 3", remote_cmd)
        self.assertIn("Retry", remote_cmd)


class TestCmdRunBackground(unittest.TestCase):
    """Test cmd_run in background mode."""

    @patch("sft.background.add_job_record")
    @patch("sft.background.uuid.uuid4")
    @patch("sft.config.load_hosts")
    def test_background_launch(self, mock_load_hosts, mock_uuid, mock_add_job):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_uuid.return_value = MagicMock(hex="abcd12345678")

        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(side_effect=[None, "12345\n", "Mon Apr  6 12:00:00 2026\n"])
        args = _run_args(background=True, name="my-job")

        from sft.cli import cmd_run

        cmd_run(args, ctx)

        self.assertEqual(ctx.run_ssh.call_count, 3)
        first_call = ctx.run_ssh.call_args_list[0]
        self.assertIn("base64 -d", first_call[0][1])
        self.assertIn("nohup bash", first_call[0][1])

        mock_add_job.assert_called_once()
        record = mock_add_job.call_args[0][0]
        self.assertEqual(record["pid"], 12345)
        self.assertEqual(record["name"], "my-job")
        self.assertEqual(record["host"], "testhost")

    @patch("sft.config.load_hosts")
    def test_background_no_pid_fails(self, mock_load_hosts):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)

        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value="")
        args = _run_args(background=True)

        from sft.cli import cmd_run

        with self.assertRaises((SystemExit, RuntimeError)):
            cmd_run(args, ctx)


class TestCmdRunDryRun(unittest.TestCase):
    """Test cmd_run in dry-run mode."""

    @patch("sft.config.load_hosts")
    def test_dry_run_no_subprocess(self, mock_load_hosts):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)

        ctx = _make_ctx(dry_run=True)
        args = _run_args(dry_run=True)

        from sft.cli import cmd_run

        with patch("sft.commands.mount.subprocess.run") as mock_sub:
            cmd_run(args, ctx)
            mock_sub.assert_not_called()


class TestCmdRunWithFetch(unittest.TestCase):
    """Test cmd_run with --fetch after foreground execution."""

    @patch("sft.commands.run.fetch_results")
    @patch("sft.commands.run.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_fetch_after_success(
        self, mock_load_hosts, mock_subprocess_run, mock_fetch
    ):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value=None)
        args = _run_args(
            target="testhost:/data",
            fetch=["*.csv", "output/"],
            fetch_to="/tmp/results",
        )

        from sft.cli import cmd_run

        cmd_run(args, ctx)

        mock_fetch.assert_called_once()
        call_args = mock_fetch.call_args
        self.assertEqual(call_args[0][0], WSL_RS)
        self.assertEqual(call_args[0][2], ["*.csv", "output/"])

    @patch("sft.commands.run.fetch_results")
    @patch("sft.commands.run.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_no_fetch_on_failure(
        self, mock_load_hosts, mock_subprocess_run, mock_fetch
    ):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=1)

        ctx = _make_ctx()
        args = _run_args(
            target="testhost:/data",
            fetch=["*.csv"],
        )

        from sft.cli import cmd_run

        with self.assertRaises(SystemExit):
            cmd_run(args, ctx)

        mock_fetch.assert_not_called()


class TestCmdSyncRun(unittest.TestCase):
    """Test cmd_sync_run with mocked SSH and rsync."""

    @patch("sft.commands.sync_run.save_remote_manifest")
    @patch("sft.commands.sync_run.load_remote_manifest", return_value=None)
    @patch("sft.config.load_hosts")
    def test_sync_run_project_sync(self, mock_load_hosts, *args):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)

        ctx = _make_ctx()
        ctx.run = MagicMock(return_value=None)
        ctx.run_ssh = MagicMock(return_value=None)
        ctx.scp_to_remote = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "myproject")
            os.makedirs(src)
            with open(os.path.join(src, "train.py"), "w") as f:
                f.write("print('hello')\n")

            args = _sync_run_args(src_dir=src)

            from sft.cli import cmd_sync_run

            cmd_sync_run(args, ctx)

            rsync_calls = [
                c for c in ctx.run.call_args_list if c[0][0][0] == "rsync"
            ]
            self.assertGreaterEqual(len(rsync_calls), 1)

            ssh_exec_calls = [
                c
                for c in ctx.run_ssh.call_args_list
                if "nohup" not in c[0][1]
            ]
            self.assertGreaterEqual(len(ssh_exec_calls), 1)
            exec_cmd = ssh_exec_calls[-1][0][1]
            self.assertIn("python", exec_cmd)
            self.assertIn("train.py", exec_cmd)

    @patch("sft.commands.sync_run.save_remote_manifest")
    @patch("sft.commands.sync_run.load_remote_manifest", return_value=None)
    @patch("sft.config.load_hosts")
    def test_sync_run_background(self, mock_load_hosts, *args):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)

        ctx = _make_ctx()
        ctx.run = MagicMock(return_value=None)
        ctx.run_ssh = MagicMock(
            side_effect=[None, "12345\n", "Mon Apr  6 12:00:00 2026\n"]
        )
        ctx.scp_to_remote = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "proj")
            os.makedirs(src)

            args = _sync_run_args(
                src_dir=src,
                background=True,
            )

            with patch("sft.background.add_job_record") as mock_add_job:
                with patch("sft.background.uuid.uuid4", return_value=MagicMock(hex="abcd12345678")):
                    from sft.cli import cmd_sync_run

                    cmd_sync_run(args, ctx)

                mock_add_job.assert_called_once()
                record = mock_add_job.call_args[0][0]
                self.assertEqual(record["pid"], 12345)
                self.assertEqual(record["host"], "testhost")


class TestCmdRunAlias(unittest.TestCase):
    """Test that host aliases resolve correctly."""

    @patch("sft.commands.run.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_alias_resolves(self, mock_load_hosts, mock_subprocess_run):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        args = _run_args(target="rs")

        from sft.cli import cmd_run

        cmd_run(args, ctx)

        ssh_cmd = mock_subprocess_run.call_args[0][0]
        self.assertIn(WSL_RS.ssh_target(), ssh_cmd)


class TestCmdRunWithSyncEnv(unittest.TestCase):
    """Test cmd_run when env sync is enabled."""

    @patch("sft.commands.run.sync_env_payload")
    @patch("sft.commands.run.resolve_env_source")
    @patch("sft.commands.run.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_env_sync_called(
        self, mock_load_hosts, mock_subprocess_run, mock_resolve, mock_sync
    ):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=0)
        mock_resolve.return_value = EnvSource(
            project_root="/home/testuser/project",
            envrc_dir="/home/testuser/project",
            flake_path="/home/testuser/project/flake.nix",
            flake_flags=[],
            flake_mode="full-flake",
            source_kind="envrc",
        )
        mock_sync.return_value = "/home/testuser/project"

        ctx = _make_ctx()
        args = _run_args(
            no_sync_env=False,
            no_auto_env=False,
        )

        from sft.cli import cmd_run

        cmd_run(args, ctx)

        mock_sync.assert_called_once()
        sync_args = mock_sync.call_args
        remote_project = sync_args[0][2]
        self.assertTrue(
            remote_project.endswith("project"),
            f"Expected remote project dir ending in 'project', got {remote_project}",
        )


class TestCmdMount(unittest.TestCase):
    """Test cmd_mount with mocked SSH and sshfs."""

    @patch("sft.commands.mount.add_mount_record")
    @patch("sft.commands.mount.shutil.which", return_value="/usr/bin/sshfs")
    @patch("sft.commands.mount.is_mount_alive", return_value=False)
    @patch("sft.commands.mount.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_mount_basic(
        self, mock_load_hosts, mock_subprocess_run, mock_alive, mock_which, mock_add_mount
    ):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()

        with tempfile.TemporaryDirectory() as tmp:
            local_mount = os.path.join(tmp, "mnt")
            os.makedirs(local_mount)
            args = argparse.Namespace(
                remote="testhost:/home/testuser/data",
                local_mount=local_mount,
                mkdir=False,
                readonly=False,
                allow_other=False,
                no_cache=True,
                foreground=False,
            )

            from sft.cli import cmd_mount

            cmd_mount(args, ctx)

            sshfs_calls = [
                c
                for c in mock_subprocess_run.call_args_list
                if c[0][0][0] == "sshfs"
            ]
            self.assertEqual(len(sshfs_calls), 1)
            sshfs_cmd = sshfs_calls[0][0][0]
            self.assertIn("sshfs", sshfs_cmd)
            self.assertIn(str(WSL_RS.port), sshfs_cmd)


class TestExitCodeCapture(unittest.TestCase):
    """Test background job exit code capture via sentinel file."""

    @patch("sft.config.load_hosts")
    def test_background_writes_exit_file(self, mock_load_hosts):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)

        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            side_effect=[None, "12345\n", "Mon Apr  6 12:00:00 2026\n"]
        )
        args = _run_args(background=True)

        with patch("sft.background.add_job_record"):
            with patch("sft.background.uuid.uuid4", return_value=MagicMock(hex="abcd12345678")):
                from sft.cli import cmd_run

                cmd_run(args, ctx)

        nohup_call = ctx.run_ssh.call_args_list[0][0][1]
        self.assertIn(".exit", nohup_call)
        self.assertIn("echo $?", nohup_call)

    def test_get_job_exit_code_reads_file(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value="42\n")

        from sft.state import get_job_exit_code

        result = get_job_exit_code(ctx, WSL_RS, "/tmp/job.log")
        self.assertEqual(result, 42)

    def test_get_job_exit_code_none_when_missing(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value="")

        from sft.state import get_job_exit_code

        result = get_job_exit_code(ctx, WSL_RS, "/tmp/job.log")
        self.assertIsNone(result)

    def test_get_job_exit_code_zero(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value="0\n")

        from sft.state import get_job_exit_code

        result = get_job_exit_code(ctx, WSL_RS, "/tmp/job.log")
        self.assertEqual(result, 0)

    @patch("sft.commands.sync_run.load_remote_manifest", return_value=None)
    @patch("sft.commands.sync_run.save_remote_manifest")
    @patch("sft.config.load_hosts")
    def test_background_cmd_sync_run_writes_exit_file(
        self, mock_load_hosts, *args
    ):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)

        ctx = _make_ctx()
        ctx.run = MagicMock(return_value=None)
        ctx.run_ssh = MagicMock(
            side_effect=[None, "12345\n", "Mon Apr  6 12:00:00 2026\n"]
        )
        ctx.scp_to_remote = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "proj")
            os.makedirs(src)

            args = _sync_run_args(src_dir=src, background=True)

            with patch("sft.background.add_job_record"):
                with patch("sft.background.uuid.uuid4", return_value=MagicMock(hex="abcd12345678")):
                    from sft.cli import cmd_sync_run

                    cmd_sync_run(args, ctx)

        nohup_call = ctx.run_ssh.call_args_list[0][0][1]
        self.assertIn(".exit", nohup_call)
        self.assertIn("echo $?", nohup_call)


class TestCmdJobs(unittest.TestCase):
    """Test cmd_jobs with batch PID checking."""

    @patch("sft.commands.jobs.batch_check_jobs")
    @patch("sft.commands.jobs.load_hosts")
    def test_jobs_shows_running_and_completed(
        self, mock_load_hosts, mock_batch
    ):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_batch.return_value = {
            "job1": {"status": "running", "exit_code": None,
                    "pid_started_at": "Mon Apr  6 12:00:00 2026"},
            "job2": {"status": "done", "exit_code": 0,
                    "pid_started_at": "Mon Apr  6 12:00:00 2026"},
        }

        ctx = _make_ctx()

        job1 = {
            "id": "job1",
            "name": "running-job",
            "host": "testhost",
            "pid": 12345,
            "command": "python train.py",
            "log_path": "~/logs/train.log",
            "created_at": "2026-04-06T12:00:00",
            "pid_started_at": "Mon Apr  6 12:00:00 2026",
        }
        job2 = {
            "id": "job2",
            "name": "done-job",
            "host": "testhost",
            "pid": 67890,
            "command": "python eval.py",
            "log_path": "~/logs/eval.log",
            "created_at": "2026-04-06T11:00:00",
            "pid_started_at": "Mon Apr  6 12:00:00 2026",
        }

        with patch("sft.commands.jobs.load_jobs_state",
                   return_value=[job1, job2]):
            with patch("sft.commands.jobs.save_jobs_state") as mock_save:
                from sft.cli import cmd_jobs

                args = argparse.Namespace(
                    host=None, status=None, name=None,
                    after=None, before=None,
                )
                cmd_jobs(args, ctx)

                mock_batch.assert_called_once()
                checks = mock_batch.call_args[0][2]
                self.assertEqual(len(checks), 2)
                mock_save.assert_called_once()

    @patch("sft.commands.jobs.load_hosts")
    def test_jobs_empty(self, mock_load_hosts):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)

        ctx = _make_ctx()

        with patch("sft.commands.jobs.load_jobs_state", return_value=[]):
            from sft.cli import cmd_jobs

            args = argparse.Namespace(
                host=None, status=None, name=None,
                after=None, before=None,
            )
            cmd_jobs(args, ctx)

    @patch("sft.commands.jobs.batch_check_jobs")
    @patch("sft.commands.jobs.load_hosts")
    def test_jobs_filters_by_host(self, mock_load_hosts, mock_batch):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_batch.return_value = {
            "job1": {"status": "running", "exit_code": None,
                    "pid_started_at": None},
        }

        ctx = _make_ctx()

        jobs = [
            {
                "id": "job1",
                "name": "wsl-job",
                "host": "testhost",
                "pid": 12345,
                "command": "echo hello",
                "created_at": "2026-04-06T12:00:00",
            },
            {
                "id": "job2",
                "name": "other-job",
                "host": "other-host",
                "pid": 99999,
                "command": "echo world",
                "created_at": "2026-04-06T11:00:00",
            },
        ]

        with patch("sft.commands.jobs.load_jobs_state", return_value=jobs):
            with patch("sft.commands.jobs.save_jobs_state"):
                from sft.cli import cmd_jobs

                args = argparse.Namespace(
                    host="testhost", status=None, name=None,
                    after=None, before=None,
                )
                import io
                captured = io.StringIO()
                import sys
                old_stdout = sys.stdout
                sys.stdout = captured
                try:
                    cmd_jobs(args, ctx)
                finally:
                    sys.stdout = old_stdout

                output = captured.getvalue()
                self.assertIn("wsl-job", output)
                self.assertNotIn("other-job", output)

    @patch("sft.commands.jobs.batch_check_jobs")
    @patch("sft.commands.jobs.load_hosts")
    def test_jobs_filters_by_status(self, mock_load_hosts, mock_batch):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_batch.return_value = {
            "job1": {"status": "running", "exit_code": None,
                    "pid_started_at": None},
            "job2": {"status": "done", "exit_code": 0,
                    "pid_started_at": None},
        }

        ctx = _make_ctx()

        jobs = [
            {
                "id": "job1",
                "name": "running-job",
                "host": "testhost",
                "pid": 12345,
                "command": "echo hello",
                "created_at": "2026-04-06T12:00:00",
            },
            {
                "id": "job2",
                "name": "done-job",
                "host": "testhost",
                "pid": 67890,
                "command": "echo world",
                "created_at": "2026-04-06T11:00:00",
            },
        ]

        with patch("sft.commands.jobs.load_jobs_state", return_value=jobs):
            with patch("sft.commands.jobs.save_jobs_state"):
                from sft.cli import cmd_jobs

                args = argparse.Namespace(
                    host=None, status="running", name=None,
                    after=None, before=None,
                )
                import io
                captured = io.StringIO()
                import sys
                old_stdout = sys.stdout
                sys.stdout = captured
                try:
                    cmd_jobs(args, ctx)
                finally:
                    sys.stdout = old_stdout

                output = captured.getvalue()
                self.assertIn("running-job", output)
                self.assertNotIn("done-job", output)

    @patch("sft.commands.jobs.batch_check_jobs")
    @patch("sft.commands.jobs.load_hosts")
    def test_jobs_unknown_host(self, mock_load_hosts, mock_batch):
        mock_load_hosts.return_value = ({"testhost": WSL_RS}, ALIAS_MAP)
        mock_batch.return_value = {}

        ctx = _make_ctx()

        jobs = [
            {
                "id": "job1",
                "name": "unknown-host-job",
                "host": "unknown-host",
                "pid": 12345,
                "command": "echo test",
                "created_at": "2026-04-06T12:00:00",
            },
        ]

        with patch("sft.commands.jobs.load_jobs_state", return_value=jobs):
            with patch("sft.commands.jobs.save_jobs_state"):
                from sft.cli import cmd_jobs

                args = argparse.Namespace(
                    host=None, status=None, name=None,
                    after=None, before=None,
                )
                import io
                captured = io.StringIO()
                import sys
                old_stdout = sys.stdout
                sys.stdout = captured
                try:
                    cmd_jobs(args, ctx)
                finally:
                    sys.stdout = old_stdout

                output = captured.getvalue()
                self.assertIn("unknown-host-job", output)

    @patch("sft.commands.jobs.batch_check_jobs")
    @patch("sft.commands.jobs.load_hosts")
    def test_jobs_batch_called_once_per_host(
        self, mock_load_hosts, mock_batch
    ):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_batch.return_value = {
            "j1": {"status": "running", "exit_code": None,
                  "pid_started_at": None},
            "j2": {"status": "done", "exit_code": 0,
                  "pid_started_at": None},
        }

        ctx = _make_ctx()

        jobs = [
            {
                "id": "j1", "name": "a", "host": "testhost", "pid": 111,
                "command": "echo 1", "created_at": "2026-04-06T12:00:00",
            },
            {
                "id": "j2", "name": "b", "host": "testhost", "pid": 222,
                "command": "echo 2", "created_at": "2026-04-06T12:01:00",
            },
        ]

        with patch("sft.commands.jobs.load_jobs_state", return_value=jobs):
            with patch("sft.commands.jobs.save_jobs_state"):
                from sft.cli import cmd_jobs

                args = argparse.Namespace(
                    host=None, status=None, name=None,
                    after=None, before=None,
                )
                cmd_jobs(args, ctx)

                self.assertEqual(mock_batch.call_count, 1)
                checks = mock_batch.call_args[0][2]
                self.assertEqual(len(checks), 2)


class TestCmdStatus(unittest.TestCase):
    """Test cmd_status subcommand."""

    @patch("sft.commands.jobs._is_pid_alive", return_value=True)
    @patch("sft.commands.jobs.load_hosts")
    def test_status_running(self, mock_load_hosts, mock_alive):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)

        ctx = _make_ctx()

        with patch("sft.commands.jobs.find_job_record") as mock_find:
            mock_find.return_value = {
                "id": "test1234",
                "name": "my-job",
                "host": "testhost",
                "pid": 12345,
                "command": "python train.py",
                "log_path": "/home/testuser/project/sft-job.log",
                "created_at": "2026-04-06T12:00:00",
            }

            from sft.cli import cmd_status

            args = argparse.Namespace(job_id="test1234")
            cmd_status(args, ctx)

    @patch("sft.commands.jobs.get_job_exit_code", return_value=0)
    @patch("sft.commands.jobs._is_pid_alive", return_value=False)
    @patch("sft.commands.jobs.load_hosts")
    def test_status_completed_with_exit_code(
        self, mock_load_hosts, mock_alive, mock_exit
    ):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)

        ctx = _make_ctx()

        with patch("sft.commands.jobs.find_job_record") as mock_find:
            mock_find.return_value = {
                "id": "test1234",
                "name": "my-job",
                "host": "testhost",
                "pid": 12345,
                "command": "python train.py",
                "log_path": "/home/testuser/project/sft-job.log",
                "created_at": "2026-04-06T12:00:00",
                "completed_at": "2026-04-06T12:05:00",
                "_status": "completed",
            }

            from sft.cli import cmd_status

            args = argparse.Namespace(job_id="test1234")
            cmd_status(args, ctx)

            mock_exit.assert_called_once()

    @patch("sft.commands.jobs._is_pid_alive", return_value=False)
    @patch("sft.commands.jobs.load_hosts")
    def test_status_completed_with_error(self, mock_load_hosts, mock_alive):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)

        ctx = _make_ctx()

        with patch("sft.commands.jobs.find_job_record") as mock_find:
            mock_find.return_value = {
                "id": "test1234",
                "name": "failed-job",
                "host": "testhost",
                "pid": 12345,
                "command": "python train.py",
                "log_path": "/home/testuser/project/sft-job.log",
                "created_at": "2026-04-06T12:00:00",
                "completed_at": "2026-04-06T12:05:00",
                "_status": "completed",
                "exit_code": 1,
            }

            from sft.cli import cmd_status

            args = argparse.Namespace(job_id="test1234")
            cmd_status(args, ctx)

    @patch("sft.commands.jobs.load_hosts")
    def test_status_job_not_found(self, mock_load_hosts):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)

        ctx = _make_ctx()

        with patch("sft.commands.jobs.find_job_record", return_value=None):
            from sft.cli import cmd_status

            args = argparse.Namespace(job_id="nonexistent")
            with self.assertRaises(SystemExit):
                cmd_status(args, ctx)


class TestSyncDiff(unittest.TestCase):
    """Test --diff flag for sync-run."""

    @patch("sft.commands.sync_run.load_remote_manifest")
    @patch("sft.config.load_hosts")
    def test_diff_no_previous_manifest(self, mock_load_hosts, mock_manifest):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_manifest.return_value = None

        ctx = _make_ctx()

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "proj")
            os.makedirs(src)

            args = _sync_run_args(src_dir=src, diff=True)

            from sft.cli import cmd_sync_run

            cmd_sync_run(args, ctx)

    @patch("sft.commands.sync_run.load_remote_manifest")
    @patch("sft.config.load_hosts")
    def test_diff_shows_changes(self, mock_load_hosts, mock_manifest):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_manifest.return_value = {
            "version": 1,
            "files": {
                "old.py": {"hash": "aaa", "size": 10, "mtime": 1.0},
                "unchanged.py": {"hash": "same", "size": 5, "mtime": 1.0},
            },
        }

        ctx = _make_ctx()

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "proj")
            os.makedirs(src)
            (Path(src) / "new.py").write_text("new content")
            (Path(src) / "unchanged.py").write_text("same")

            args = _sync_run_args(src_dir=src, diff=True)

            from sft.cli import cmd_sync_run

            cmd_sync_run(args, ctx)


class TestFetchAuto(unittest.TestCase):
    """Test --fetch-auto flag for auto-detecting and fetching new files."""

    def test_snapshot_remote_directory(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            return_value="output.csv\t1024\t1712345678.0\nresults.json\t512\t1712345679.0\n"
        )

        from sft.commands.fetch import snapshot_remote_directory

        result = snapshot_remote_directory(ctx, WSL_RS, "/home/testuser/project")
        self.assertIn("output.csv", result)
        self.assertEqual(result["output.csv"]["size"], 1024)
        self.assertIn("results.json", result)

    def test_snapshot_empty_directory(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value="")

        from sft.commands.fetch import snapshot_remote_directory

        result = snapshot_remote_directory(ctx, WSL_RS, "/home/testuser/project")
        self.assertEqual(result, {})

    def test_diff_snapshots(self):
        from sft.commands.fetch import diff_remote_snapshots

        before = {
            "old.txt": {"size": 100, "mtime": 1.0},
            "unchanged.txt": {"size": 50, "mtime": 1.0},
        }
        after = {
            "unchanged.txt": {"size": 50, "mtime": 1.0},
            "new.txt": {"size": 200, "mtime": 2.0},
            "modified.txt": {"size": 150, "mtime": 3.0},
        }
        added, modified = diff_remote_snapshots(before, after)
        self.assertIn("new.txt", added)
        self.assertIn("modified.txt", added)
        self.assertNotIn("unchanged.txt", added)
        self.assertEqual(modified, [])

    @patch("sft.commands.run.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_fetch_auto_in_foreground_run(
        self, mock_load_hosts, mock_subprocess_run
    ):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            side_effect=[
                "output.csv\t1024\t1712345678.0\n",
                "output.csv\t1024\t1712345678.0\nresults.json\t512\t1712345680.0\n",
            ]
        )
        ctx.run = MagicMock(return_value=None)
        args = _run_args(fetch_auto=True)

        from sft.cli import cmd_run

        cmd_run(args, ctx)

        rsync_calls = [
            c for c in ctx.run.call_args_list if c[0][0][0] == "rsync"
        ]
        self.assertGreaterEqual(len(rsync_calls), 1)

    @patch("sft.commands.run.subprocess.run")
    @patch("sft.config.load_hosts")
    def test_fetch_auto_no_changes(
        self, mock_load_hosts, mock_subprocess_run
    ):
        mock_load_hosts.return_value = (HOSTS_MAP, ALIAS_MAP)
        mock_subprocess_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        snapshot = "output.csv\t1024\t1712345678.0\n"
        ctx.run_ssh = MagicMock(
            side_effect=[snapshot, snapshot]
        )
        ctx.run = MagicMock(return_value=None)
        args = _run_args(fetch_auto=True)

        from sft.cli import cmd_run

        cmd_run(args, ctx)

        rsync_calls = [
            c for c in ctx.run.call_args_list if c[0][0][0] == "rsync"
        ]
        self.assertEqual(len(rsync_calls), 0)


class TestLaunchBackgroundJob(unittest.TestCase):
    """Test launch_background_job: PID retry loop, record fields, nohup command."""

    @patch("sft.background.add_job_record")
    @patch("sft.background.uuid.uuid4")
    def test_pid_succeeds_first_try(self, mock_uuid, mock_add_job):
        mock_uuid.return_value = MagicMock(hex="abcd12345678")
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            side_effect=[None, "12345\n", "Mon Apr  6 12:00:00 2026\n"]
        )

        from sft.background import launch_background_job

        job_id = launch_background_job(
            ctx=ctx,
            host_info=WSL_RS,
            remote_cwd="~/project",
            full_cmd="python train.py",
            command="python train.py",
            log_path="~/logs/train.log",
            singleton_key=None,
            fetch_patterns=None,
            fetch_dest="/tmp",
            name="test-job",
            name_prefix="run",
        )

        self.assertEqual(job_id, "abcd1234")
        self.assertEqual(ctx.run_ssh.call_count, 3)

        nohup_call = ctx.run_ssh.call_args_list[0]
        nohup_cmd = nohup_call[0][1]
        self.assertIn("nohup bash", nohup_cmd)
        self.assertIn("base64 -d", nohup_cmd)

        pid_call = ctx.run_ssh.call_args_list[1]
        self.assertIn(".pid", pid_call[0][1])

        record = mock_add_job.call_args[0][0]
        self.assertEqual(record["pid"], 12345)
        self.assertEqual(record["name"], "test-job")
        self.assertEqual(record["host"], "testhost")
        self.assertEqual(record["command"], "python train.py")
        self.assertEqual(record["singleton_key"], None)

    @patch("sft.background.add_job_record")
    @patch("sft.background.uuid.uuid4")
    def test_pid_retry_succeeds_on_third_attempt(
        self, mock_uuid, mock_add_job
    ):
        mock_uuid.return_value = MagicMock(hex="abcd12345678")
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            side_effect=[
                None,
                "",
                "  \n",
                "12345\n",
                "Mon Apr  6 12:00:00 2026\n",
            ]
        )

        from sft.background import launch_background_job

        job_id = launch_background_job(
            ctx=ctx,
            host_info=WSL_RS,
            remote_cwd="~/project",
            full_cmd="python train.py",
            command="python train.py",
            log_path="~/logs/train.log",
            singleton_key=None,
            fetch_patterns=["*.csv"],
            fetch_dest="/tmp",
            name=None,
            name_prefix="sync",
        )

        self.assertEqual(job_id, "abcd1234")
        record = mock_add_job.call_args[0][0]
        self.assertEqual(record["pid"], 12345)
        self.assertEqual(record["fetch_patterns"], ["*.csv"])
        self.assertTrue(record["name"].startswith("sync-"))

    @patch("sft.background.uuid.uuid4")
    def test_pid_retry_exhausted_raises(self, mock_uuid):
        mock_uuid.return_value = MagicMock(hex="abcd12345678")
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value="")

        from sft.background import launch_background_job

        with self.assertRaises(RuntimeError) as cm:
            launch_background_job(
                ctx=ctx,
                host_info=WSL_RS,
                remote_cwd="~/project",
                full_cmd="python train.py",
                command="python train.py",
                log_path="~/logs/train.log",
                singleton_key=None,
                fetch_patterns=None,
                fetch_dest="/tmp",
                name=None,
                name_prefix="run",
            )
        self.assertIn("Failed to get PID", str(cm.exception))

    @patch("sft.background.add_job_record")
    @patch("sft.background.uuid.uuid4")
    def test_pid_wrapper_writes_pid_file(self, mock_uuid, mock_add_job):
        mock_uuid.return_value = MagicMock(hex="abcd12345678")
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            side_effect=[None, "999\n", "Tue Apr  7 10:00:00 2026\n"]
        )

        from sft.background import launch_background_job

        launch_background_job(
            ctx=ctx,
            host_info=WSL_RS,
            remote_cwd="~/project",
            full_cmd="echo hello",
            command="echo hello",
            log_path="~/logs/test.log",
            singleton_key="unique-key",
            fetch_patterns=None,
            fetch_dest=os.getcwd(),
            name="singleton-test",
            name_prefix="run",
        )

        import base64

        nohup_call = ctx.run_ssh.call_args_list[0]
        nohup_cmd = nohup_call[0][1]
        self.assertIn("nohup bash", nohup_cmd)
        self.assertIn("base64 -d", nohup_cmd)
        self.assertIn(".exit", nohup_cmd)

        encoded_part = nohup_cmd.split("echo ")[1].split(" | base64")[0]
        inner = base64.b64decode(encoded_part).decode()
        self.assertIn("echo $$", inner)
        self.assertIn(".pid", inner)
        self.assertIn("echo hello", inner)

        record = mock_add_job.call_args[0][0]
        self.assertEqual(record["singleton_key"], "unique-key")
        self.assertEqual(record["pid"], 999)

    @patch("sft.background.add_job_record")
    @patch("sft.background.uuid.uuid4")
    def test_ps_lstart_failure_still_records(
        self, mock_uuid, mock_add_job
    ):
        mock_uuid.return_value = MagicMock(hex="abcd12345678")
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            side_effect=[
                None,
                "12345\n",
                RuntimeError("ps failed"),
            ]
        )

        from sft.background import launch_background_job

        launch_background_job(
            ctx=ctx,
            host_info=WSL_RS,
            remote_cwd="~/project",
            full_cmd="python train.py",
            command="python train.py",
            log_path="~/logs/train.log",
            singleton_key=None,
            fetch_patterns=None,
            fetch_dest="/tmp",
            name=None,
            name_prefix="run",
        )

        record = mock_add_job.call_args[0][0]
        self.assertEqual(record["pid"], 12345)
        self.assertIsNone(record["pid_started_at"])


class TestBatchCheckRemotePids(unittest.TestCase):
    """Test batch_check_remote_pids: single SSH call for multiple jobs on one host."""

    def test_all_running(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            return_value=(
                "STATUS:12345:Mon Apr  6 12:00:00 2026\n"
                "EXIT:12345:NONE\n"
                "STATUS:67890:Mon Apr  6 12:00:00 2026\n"
                "EXIT:67890:NONE\n"
            )
        )

        from sft.state import batch_check_remote_pids

        checks = [
            {"pid": 12345, "pid_started_at": "Mon Apr  6 12:00:00 2026",
             "exit_file": "~/logs/job1.log.exit"},
            {"pid": 67890, "pid_started_at": "Mon Apr  6 12:00:00 2026",
             "exit_file": "~/logs/job2.log.exit"},
        ]
        results = batch_check_remote_pids(ctx, WSL_RS, checks)

        self.assertEqual(results[12345]["status"], "running")
        self.assertEqual(results[67890]["status"], "running")
        self.assertIsNone(results[12345]["exit_code"])
        self.assertIsNone(results[67890]["exit_code"])
        self.assertEqual(ctx.run_ssh.call_count, 1)

    def test_all_done(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            return_value=(
                "STATUS:12345:GONE\n"
                "EXIT:12345:0\n"
                "STATUS:67890:GONE\n"
                "EXIT:67890:1\n"
            )
        )

        from sft.state import batch_check_remote_pids

        checks = [
            {"pid": 12345, "pid_started_at": "Mon Apr  6 12:00:00 2026",
             "exit_file": "~/logs/job1.log.exit"},
            {"pid": 67890, "pid_started_at": "Mon Apr  6 12:00:00 2026",
             "exit_file": "~/logs/job2.log.exit"},
        ]
        results = batch_check_remote_pids(ctx, WSL_RS, checks)

        self.assertEqual(results[12345]["status"], "done")
        self.assertEqual(results[12345]["exit_code"], 0)
        self.assertEqual(results[67890]["status"], "done")
        self.assertEqual(results[67890]["exit_code"], 1)

    def test_mixed_running_done(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            return_value=(
                "STATUS:111:GONE\n"
                "EXIT:111:0\n"
                "STATUS:222:Mon Apr  6 13:00:00 2026\n"
                "EXIT:222:NONE\n"
            )
        )

        from sft.state import batch_check_remote_pids

        checks = [
            {"pid": 111, "pid_started_at": "Mon Apr  6 12:00:00 2026",
             "exit_file": "~/logs/a.log.exit"},
            {"pid": 222, "pid_started_at": "Mon Apr  6 13:00:00 2026",
             "exit_file": "~/logs/b.log.exit"},
        ]
        results = batch_check_remote_pids(ctx, WSL_RS, checks)

        self.assertEqual(results[111]["status"], "done")
        self.assertEqual(results[222]["status"], "running")

    def test_pid_reuse_detected(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            return_value=(
                "STATUS:99999:Tue Apr  7 10:00:00 2026\n"
                "EXIT:99999:NONE\n"
            )
        )

        from sft.state import batch_check_remote_pids

        checks = [
            {"pid": 99999, "pid_started_at": "Mon Apr  6 12:00:00 2026",
             "exit_file": "~/logs/reuse.log.exit"},
        ]
        results = batch_check_remote_pids(ctx, WSL_RS, checks)

        self.assertEqual(results[99999]["status"], "done")

    def test_exit_file_missing(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            return_value=(
                "STATUS:12345:GONE\n"
                "EXIT:12345:NONE\n"
            )
        )

        from sft.state import batch_check_remote_pids

        checks = [
            {"pid": 12345, "pid_started_at": "Mon Apr  6 12:00:00 2026",
             "exit_file": "~/logs/missing.log.exit"},
        ]
        results = batch_check_remote_pids(ctx, WSL_RS, checks)

        self.assertEqual(results[12345]["status"], "done")
        self.assertIsNone(results[12345]["exit_code"])

    def test_ssh_failure_all_unknown(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            side_effect=RuntimeError(
                "ssh: connect to host 10.0.0.1 port 2222: Connection refused"
            )
        )

        from sft.state import batch_check_remote_pids

        checks = [
            {"pid": 12345, "pid_started_at": "Mon Apr  6 12:00:00 2026",
             "exit_file": "~/logs/job.log.exit"},
        ]
        results = batch_check_remote_pids(ctx, WSL_RS, checks)

        self.assertEqual(results[12345]["status"], "unknown")
        self.assertIsNone(results[12345]["exit_code"])

    def test_empty_checks(self):
        ctx = _make_ctx()
        from sft.state import batch_check_remote_pids

        results = batch_check_remote_pids(ctx, WSL_RS, [])
        self.assertEqual(results, {})

    def test_malformed_output(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value="garbage line\nmore garbage\n")

        from sft.state import batch_check_remote_pids

        checks = [
            {"pid": 12345, "pid_started_at": "Mon Apr  6 12:00:00 2026",
             "exit_file": "~/logs/job.log.exit"},
        ]
        results = batch_check_remote_pids(ctx, WSL_RS, checks)

        self.assertEqual(results[12345]["status"], "done")
        self.assertIsNone(results[12345]["exit_code"])

    def test_without_pid_started_at(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            return_value=(
                "STATUS:12345:ALIVE\n"
                "EXIT:12345:0\n"
            )
        )

        from sft.state import batch_check_remote_pids

        checks = [
            {"pid": 12345, "pid_started_at": None,
             "exit_file": "~/logs/job.log.exit"},
        ]
        results = batch_check_remote_pids(ctx, WSL_RS, checks)

        self.assertEqual(results[12345]["status"], "running")
        self.assertEqual(results[12345]["exit_code"], 0)

    def test_batch_command_structure(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value="")

        from sft.state import batch_check_remote_pids

        checks = [
            {"pid": 100, "pid_started_at": "Mon Apr  6 12:00:00 2026",
             "exit_file": "~/logs/a.log.exit"},
            {"pid": 200, "pid_started_at": "Mon Apr  6 12:00:00 2026",
             "exit_file": "~/logs/b.log.exit"},
        ]
        batch_check_remote_pids(ctx, WSL_RS, checks)

        cmd = ctx.run_ssh.call_args[0][1]
        self.assertIn("ps -o lstart= -p 100", cmd)
        self.assertIn("ps -o lstart= -p 200", cmd)
        self.assertIn("STATUS:100", cmd)
        self.assertIn("STATUS:200", cmd)
        self.assertIn("EXIT:100", cmd)
        self.assertIn("EXIT:200", cmd)
        self.assertIn("cat $HOME/logs/a.log.exit", cmd)
        self.assertIn("cat $HOME/logs/b.log.exit", cmd)
