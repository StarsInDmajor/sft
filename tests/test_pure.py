"""Unit tests for pure functions: config parsing, target resolution, env detection."""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from sft.config import (
    HostInfo,
    ParsedTarget,
    SftConfig,
    parse_target,
    determine_mode,
    load_config,
)
from sft.env import (
    find_project_root,
    compute_remote_project_dir,
    compute_remote_flake_dir,
)
from sft.manifest import (
    build_manifest,
    compare_manifests,
    format_delta,
    _scan_local,
)


class TestHostInfo(unittest.TestCase):
    def test_ssh_target(self):
        h = HostInfo(
            name="test",
            hostname="10.0.0.1",
            port=22,
            user="user",
            aliases=["t", "testhost"],
            extra_options={},
        )
        self.assertEqual(h.ssh_target(), "user@10.0.0.1")

    def test_from_dict(self):
        h = HostInfo(
            name="myhost",
            hostname="example.com",
            port=2222,
            user="admin",
            aliases=[],
            extra_options={"IdentityFile": "~/.ssh/id_rsa"},
        )
        self.assertEqual(h.ssh_target(), "admin@example.com")
        self.assertEqual(h.port, 2222)


class TestParsedTarget(unittest.TestCase):
    def test_local_target(self):
        t = ParsedTarget(
            is_remote=False,
            path="/home/user/project",
            host=None,
            user_override=None,
        )
        self.assertFalse(t.is_remote)
        self.assertEqual(t.path, "/home/user/project")
        self.assertIsNone(t.host)

    def test_remote_target(self):
        h = HostInfo(
            name="test",
            hostname="10.0.0.1",
            port=22,
            user="user",
            aliases=[],
            extra_options={},
        )
        t = ParsedTarget(
            is_remote=True,
            path="~/project",
            host=h,
            user_override=None,
        )
        self.assertTrue(t.is_remote)
        self.assertEqual(t.host.name, "test")


class TestParseTarget(unittest.TestCase):
    def setUp(self):
        self.hosts = {
            "myhost": HostInfo(
                name="myhost",
                hostname="10.0.0.1",
                port=22,
                user="test",
                aliases=["mh", "alias"],
                extra_options={},
            ),
        }
        self.alias_map = {
            "myhost": "myhost",
            "mh": "myhost",
            "alias": "myhost",
        }

    def test_local_path(self):
        t = parse_target("/some/local/path", self.hosts, self.alias_map)
        self.assertFalse(t.is_remote)

    def test_remote_path(self):
        t = parse_target("myhost:~/project", self.hosts, self.alias_map)
        self.assertTrue(t.is_remote)
        self.assertEqual(t.host.name, "myhost")
        self.assertEqual(t.path, "~/project")

    def test_alias(self):
        t = parse_target("mh:~/project", self.hosts, self.alias_map)
        self.assertTrue(t.is_remote)
        self.assertEqual(t.host.name, "myhost")

    def test_unknown_host(self):
        with self.assertRaises(ValueError):
            parse_target("unknown:path", self.hosts, self.alias_map)

    def test_empty_remote_path(self):
        with self.assertRaises(ValueError):
            parse_target("myhost:", self.hosts, self.alias_map)


class TestDetermineMode(unittest.TestCase):
    def test_all_modes(self):
        local = ParsedTarget(False, "/a", None, None)
        remote = ParsedTarget(
            True, "/b", HostInfo("h", "h", 22, "u", [], {}), None
        )
        self.assertEqual(determine_mode(local, local), "local->local")
        self.assertEqual(determine_mode(local, remote), "local->remote")
        self.assertEqual(determine_mode(remote, local), "remote->local")
        self.assertEqual(determine_mode(remote, remote), "remote->remote")


class TestFindProjectRoot(unittest.TestCase):
    def test_finds_git_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            result = find_project_root(str(tmp))
            self.assertEqual(result, str(tmp))

    def test_finds_envrc_root(self):
        # .envrc detection moved to sft-nix plugin; .git still works
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            (Path(tmp) / ".envrc").write_text("use flake")
            result = find_project_root(str(tmp))
            self.assertEqual(result, str(tmp))

    def test_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = find_project_root(str(tmp))
            self.assertIsNone(result)


class TestComputeRemoteProjectDir(unittest.TestCase):
    def test_default_path(self):
        result = compute_remote_project_dir("/home/user/myproject", None)
        self.assertEqual(result, "~/Workspace/myproject")

    def test_explicit_remote_path(self):
        result = compute_remote_project_dir(
            "/home/user/myproject", "~/custom/location"
        )
        self.assertEqual(result, "~/custom/location")


class TestComputeRemoteFlakeDir(unittest.TestCase):
    def test_relative_envrc(self):
        result = compute_remote_flake_dir(
            local_envrc_dir="/home/user/project",
            local_project_root="/home/user/project",
            remote_project_dir="~/Workspace/project",
        )
        self.assertEqual(result, "~/Workspace/project/.sft/flake-env/project")


class TestLoadConfig(unittest.TestCase):
    def test_default_empty_hosts(self):
        config = load_config(config_path="/nonexistent/config.yaml")
        self.assertEqual(config.hosts, {})
        self.assertEqual(config.compression_threshold, 50)
        self.assertTrue(config.ssh_control_master)


class TestManifest(unittest.TestCase):
    def test_scan_local_finds_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "train.py").write_text("print('hi')\n")
            (Path(tmp) / "data.txt").write_text("hello\n")
            (Path(tmp) / ".hidden").write_text("secret\n")
            cache = Path(tmp) / "__pycache__"
            cache.mkdir()
            (cache / "mod.pyc").write_text("bytecode")

            files = _scan_local(tmp, ["__pycache__", "*.pyc"])
            self.assertIn("train.py", files)
            self.assertIn("data.txt", files)
            self.assertNotIn(".hidden", files)
            self.assertNotIn("__pycache__/mod.pyc", files)

    def test_build_manifest_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "main.py").write_text("x = 1\n")
            manifest = build_manifest(tmp, "~/project", [], "project-sync")
            self.assertEqual(manifest["version"], 1)
            self.assertIn("synced_at", manifest)
            self.assertEqual(manifest["sync_mode"], "project-sync")
            self.assertIn("main.py", manifest["files"])

    def test_compare_manifests_detects_changes(self):
        old = {
            "files": {
                "a.py": {"hash": "aaa", "size": 10, "mtime": 1.0},
                "b.py": {"hash": "bbb", "size": 20, "mtime": 2.0},
                "c.py": {"hash": "ccc", "size": 30, "mtime": 3.0},
            }
        }
        new = {
            "files": {
                "a.py": {"hash": "aaa", "size": 10, "mtime": 1.0},
                "b.py": {"hash": "BBB", "size": 25, "mtime": 2.5},
                "d.py": {"hash": "ddd", "size": 40, "mtime": 4.0},
            }
        }
        added, modified, deleted = compare_manifests(old, new)
        self.assertEqual(added, ["d.py"])
        self.assertEqual(modified, ["b.py"])
        self.assertEqual(deleted, ["c.py"])

    def test_compare_manifests_no_changes(self):
        files = {"x.py": {"hash": "abc", "size": 5, "mtime": 1.0}}
        added, modified, deleted = compare_manifests(
            {"files": files}, {"files": files}
        )
        self.assertEqual(added, [])
        self.assertEqual(modified, [])
        self.assertEqual(deleted, [])

    def test_format_delta_no_changes(self):
        result = format_delta([], [], [])
        self.assertIn("No changes", result)

    def test_format_delta_with_changes(self):
        result = format_delta(["a.py"], ["b.py"], ["c.py"])
        self.assertIn("1 added", result)
        self.assertIn("1 modified", result)
        self.assertIn("1 deleted", result)

    def test_format_delta_truncates_long_lists(self):
        result = format_delta(
            [f"f{i}.py" for i in range(10)],
            [f"m{i}.py" for i in range(10)],
            [],
        )
        self.assertIn("10 added", result)
        self.assertIn("... and 5 more", result)


from sft.shell import rq, build_env_exports


class TestRq(unittest.TestCase):
    def test_home_prefix_double_quoted(self):
        self.assertEqual(rq("$HOME/project"), '"$HOME/project"')

    def test_tilde_expanded_to_home(self):
        self.assertEqual(rq("~/project"), '"$HOME/project"')

    def test_absolute_path_shlex_quoted(self):
        self.assertEqual(rq("/tmp/project"), shlex.quote("/tmp/project"))

    def test_path_with_spaces(self):
        self.assertEqual(rq("/tmp/my project"), shlex.quote("/tmp/my project"))

    def test_home_with_spaces(self):
        self.assertEqual(rq("$HOME/my project"), '"$HOME/my project"')


class TestBuildEnvExports(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(build_env_exports(None), "")

    def test_empty_list_returns_empty(self):
        self.assertEqual(build_env_exports([]), "")

    def test_key_value(self):
        result = build_env_exports(["FOO=bar"])
        self.assertIn("export FOO=bar", result)

    def test_key_only_reads_environ(self):
        os.environ["SFT_TEST_VAR"] = "hello"
        try:
            result = build_env_exports(["SFT_TEST_VAR"])
            self.assertIn("export SFT_TEST_VAR=hello", result)
        finally:
            del os.environ["SFT_TEST_VAR"]


from sft.config_project import load_project_config, merge_project_config


class TestLoadProjectConfig(unittest.TestCase):
    def test_no_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = load_project_config(tmp)
            self.assertEqual(result, {})

    def test_valid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / ".sftrc.toml"
            cfg_path.write_text(
                'post_sync = ["uv sync --reinstall-package mypkg"]\n'
                'reinstall_pkg = ["reion3"]\n'
                'exclude = ["*.fits", "*.h5"]\n'
            )
            result = load_project_config(tmp)
            self.assertEqual(
                result["post_sync"],
                ["uv sync --reinstall-package mypkg"],
            )
            self.assertEqual(result["reinstall_pkg"], ["reion3"])
            self.assertEqual(result["exclude"], ["*.fits", "*.h5"])

    def test_empty_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / ".sftrc.toml"
            cfg_path.write_text("\n")
            result = load_project_config(tmp)
            self.assertEqual(result, {})

    def test_invalid_type_warns(self):
        """A string where a list is expected should be silently skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / ".sftrc.toml"
            cfg_path.write_text('post_sync = "not a list"\n')
            result = load_project_config(tmp)
            self.assertNotIn("post_sync", result)

    def test_unknown_keys_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / ".sftrc.toml"
            cfg_path.write_text('unknown_key = "value"\n')
            result = load_project_config(tmp)
            self.assertEqual(result, {})


class TestMergeProjectConfig(unittest.TestCase):
    class FakeArgs:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    def test_cli_post_sync_overrides_config(self):
        cfg = {"post_sync": ["from-config"]}
        args = self.FakeArgs(
            post_sync=["from-cli"], reinstall_pkg=None, exclude=None,
        )
        post_sync, reinstall, excludes = merge_project_config(cfg, args)
        self.assertEqual(post_sync, ["from-cli"])

    def test_config_post_sync_used_when_no_cli(self):
        cfg = {"post_sync": ["from-config"]}
        args = self.FakeArgs(post_sync=None, reinstall_pkg=None, exclude=None)
        post_sync, reinstall, excludes = merge_project_config(cfg, args)
        self.assertEqual(post_sync, ["from-config"])

    def test_cli_reinstall_overrides_config(self):
        cfg = {"reinstall_pkg": ["pkg-config"]}
        args = self.FakeArgs(
            post_sync=None, reinstall_pkg=["pkg-cli"], exclude=None,
        )
        post_sync, reinstall, excludes = merge_project_config(cfg, args)
        self.assertEqual(reinstall, ["pkg-cli"])

    def test_config_reinstall_used_when_no_cli(self):
        cfg = {"reinstall_pkg": ["pkg-config"]}
        args = self.FakeArgs(post_sync=None, reinstall_pkg=None, exclude=None)
        post_sync, reinstall, excludes = merge_project_config(cfg, args)
        self.assertEqual(reinstall, ["pkg-config"])

    def test_empty_config_and_no_cli(self):
        cfg = {}
        args = self.FakeArgs(post_sync=None, reinstall_pkg=None, exclude=None)
        post_sync, reinstall, excludes = merge_project_config(cfg, args)
        self.assertEqual(post_sync, [])
        self.assertEqual(reinstall, [])
        self.assertIsNone(excludes)

    def test_cli_exclude_overrides_config(self):
        cfg = {"exclude": ["*.fits"]}
        args = self.FakeArgs(
            post_sync=None, reinstall_pkg=None, exclude=["*.h5"],
        )
        post_sync, reinstall, excludes = merge_project_config(cfg, args)
        self.assertEqual(excludes, ["*.h5"])

    def test_config_exclude_used_when_no_cli(self):
        cfg = {"exclude": ["*.fits", "*.h5"]}
        args = self.FakeArgs(post_sync=None, reinstall_pkg=None, exclude=None)
        post_sync, reinstall, excludes = merge_project_config(cfg, args)
        self.assertEqual(excludes, ["*.fits", "*.h5"])


class TestCliPostSyncAndReinstallFlags(unittest.TestCase):
    """Test that --post-sync and --reinstall-pkg parse correctly via argparse."""

    def _parse(self, argv):
        from sft.cli import parse_args
        old = sys.argv
        try:
            sys.argv = argv
            return parse_args()
        finally:
            sys.argv = old

    def test_post_sync_single(self):
        args = self._parse(
            ["sft", "sync-run", "--project-sync", "--post-sync", "echo hi",
             "/tmp/src", "testhost:~/proj"]
        )
        self.assertEqual(args.post_sync, ["echo hi"])

    def test_post_sync_repeatable(self):
        args = self._parse(
            ["sft", "sync-run", "--project-sync",
             "--post-sync", "echo hi",
             "--post-sync", "echo bye",
             "/tmp/src", "testhost:~/proj"]
        )
        self.assertEqual(args.post_sync, ["echo hi", "echo bye"])

    def test_reinstall_pkg_single(self):
        args = self._parse(
            ["sft", "sync-run", "--project-sync",
             "--reinstall-pkg", "reion3",
             "/tmp/src", "testhost:~/proj"]
        )
        self.assertEqual(args.reinstall_pkg, ["reion3"])

    def test_reinstall_pkg_repeatable(self):
        args = self._parse(
            ["sft", "sync-run", "--project-sync",
             "--reinstall-pkg", "reion3",
             "--reinstall-pkg", "py21cmfast",
             "/tmp/src", "testhost:~/proj"]
        )
        self.assertEqual(args.reinstall_pkg, ["reion3", "py21cmfast"])

    def test_both_flags_together(self):
        args = self._parse(
            ["sft", "sync-run", "--project-sync",
             "--post-sync", "echo hello",
             "--reinstall-pkg", "reion3",
             "/tmp/src", "testhost:~/proj"]
        )
        self.assertEqual(args.post_sync, ["echo hello"])
        self.assertEqual(args.reinstall_pkg, ["reion3"])

    def test_no_flags_gives_none(self):
        args = self._parse(
            ["sft", "sync-run", "--project-sync",
             "/tmp/src", "testhost:~/proj"]
        )
        self.assertIsNone(args.post_sync)
        self.assertIsNone(args.reinstall_pkg)


# --- PBS helpers ---


import json

from sft.pbs import (
    PbsJobInfo,
    build_qsub_command,
    parse_output_path,
    parse_qstat_json,
    parse_qsub_output,
    pbs_state_to_status,
)


class TestPbsStateMapping(unittest.TestCase):
    def test_known_states(self):
        self.assertEqual(pbs_state_to_status("Q"), "queued")
        self.assertEqual(pbs_state_to_status("R"), "running")
        self.assertEqual(pbs_state_to_status("F"), "done")
        self.assertEqual(pbs_state_to_status("H"), "held")
        self.assertEqual(pbs_state_to_status("W"), "queued")
        self.assertEqual(pbs_state_to_status("S"), "held")

    def test_unknown_state(self):
        self.assertEqual(pbs_state_to_status("Z"), "unknown")
        self.assertEqual(pbs_state_to_status(""), "unknown")


class TestParseOutputPath(unittest.TestCase):
    def test_with_hostname_prefix(self):
        self.assertEqual(
            parse_output_path("myserver:/home/user/job.out"),
            "/home/user/job.out",
        )

    def test_absolute_path_no_prefix(self):
        self.assertEqual(
            parse_output_path("/home/user/job.out"),
            "/home/user/job.out",
        )

    def test_none_input(self):
        self.assertIsNone(parse_output_path(None))

    def test_empty_string(self):
        self.assertIsNone(parse_output_path(""))


class TestParseQstatJson(unittest.TestCase):
    def test_finished_job(self):
        """Verify parsing of a completed PBS job with capital-E Exit_status."""
        raw = json.dumps({
            "timestamp": 1234567890,
            "pbs_version": "23.06.06",
            "Jobs": {
                "0:320659.myserver": {
                    "Job_Name": "myjob",
                    "job_state": "F",
                    "Exit_status": 0,
                    "Output_Path": "myserver:/home/user/output/job.out",
                    "Error_Path": "myserver:/home/user/output/job.err",
                    "Join_Path": "n",
                    "substate": 92,
                    "ctime": "1712566800",
                    "stime": "1712566801",
                    "mtime": "1712571679",
                    "queue": "batch",
                    "session_id": 12345,
                }
            }
        })
        result = parse_qstat_json(raw)
        self.assertEqual(len(result), 1)
        info = result["320659.myserver"]
        self.assertIsInstance(info, PbsJobInfo)
        self.assertEqual(info.job_id, "320659.myserver")
        self.assertEqual(info.name, "myjob")
        self.assertEqual(info.state, "F")
        self.assertEqual(info.exit_status, 0)
        self.assertFalse(info.join_output)

    def test_running_job(self):
        """Running jobs have null Exit_status."""
        raw = json.dumps({
            "timestamp": 1234567890,
            "pbs_version": "23.06.06",
            "Jobs": {
                "1:310364.myserver": {
                    "Job_Name": "fit_model",
                    "job_state": "R",
                    "Exit_status": None,
                    "substate": 42,
                }
            }
        })
        result = parse_qstat_json(raw)
        self.assertEqual(len(result), 1)
        info = result["310364.myserver"]
        self.assertEqual(info.state, "R")
        self.assertIsNone(info.exit_status)

    def test_multiple_jobs(self):
        raw = json.dumps({
            "Jobs": {
                "0:100.myserver": {"Job_Name": "a", "job_state": "R"},
                "1:200.myserver": {"Job_Name": "b", "job_state": "Q"},
            }
        })
        result = parse_qstat_json(raw)
        self.assertEqual(len(result), 2)
        self.assertEqual(result["100.myserver"].state, "R")
        self.assertEqual(result["200.myserver"].state, "Q")

    def test_empty_response(self):
        """Unknown job IDs return empty Jobs dict."""
        raw = json.dumps({
            "timestamp": 1234567890,
            "pbs_version": "23.06.06",
            "pbs_server": "myserver",
        })
        result = parse_qstat_json(raw)
        self.assertEqual(result, {})

    def test_invalid_json(self):
        result = parse_qstat_json("not json at all")
        self.assertEqual(result, {})

    def test_capital_exit_status_gotcha(self):
        """Verify we read 'Exit_status' not 'exit_status' (capital E)."""
        raw = json.dumps({
            "Jobs": {
                "0:999.myserver": {
                    "Job_Name": "test",
                    "job_state": "F",
                    "Exit_status": 42,
                }
            }
        })
        result = parse_qstat_json(raw)
        self.assertEqual(result["999.myserver"].exit_status, 42)

    def test_join_output_oe(self):
        raw = json.dumps({
            "Jobs": {
                "0:100.myserver": {
                    "Job_Name": "a",
                    "job_state": "R",
                    "Join_Path": "oe",
                }
            }
        })
        result = parse_qstat_json(raw)
        self.assertTrue(result["100.myserver"].join_output)


class TestBuildQsubCommand(unittest.TestCase):
    def test_minimal(self):
        cmd = build_qsub_command("/path/to/script.sh")
        self.assertIn("qsub", cmd)
        # shlex.quote leaves safe strings unquoted
        self.assertIn("/path/to/script.sh", cmd)

    def test_with_name(self):
        cmd = build_qsub_command(
            "/path/to/script.sh", name="my-job",
        )
        self.assertIn("-N", cmd)
        # shlex.quote leaves safe strings unquoted; check the value is present
        self.assertIn("my-job", cmd)

    def test_with_output_and_join(self):
        cmd = build_qsub_command(
            "/path/to/script.sh",
            output_path="/home/user/job.log",
            join_output=True,
        )
        self.assertIn("-j", cmd)
        self.assertIn("oe", cmd)
        self.assertIn("-o", cmd)
        self.assertIn("/home/user/job.log", cmd)

    def test_with_env_vars(self):
        cmd = build_qsub_command(
            "/path/to/script.sh",
            env_vars=["FOO=bar", "BAZ=qux"],
        )
        self.assertIn("-v", cmd)
        self.assertIn("FOO=bar,BAZ=qux", cmd)

    def test_with_queue_and_resources(self):
        cmd = build_qsub_command(
            "/path/to/script.sh",
            queue="gpu",
            resources="nodes=1:ppn=128,walltime=24:00:00",
        )
        self.assertIn("-q", cmd)
        self.assertIn("gpu", cmd)
        self.assertIn("-l", cmd)
        self.assertIn("nodes=1:ppn=128,walltime=24:00:00", cmd)

    def test_no_join_when_error_path_set(self):
        cmd = build_qsub_command(
            "/path/to/script.sh",
            error_path="/home/user/job.err",
            join_output=True,
        )
        self.assertNotIn("-j", cmd)
        self.assertIn("-e", cmd)

    def test_shell_quoting_for_special_chars(self):
        """shlex.quote adds quotes when paths contain spaces/special chars."""
        cmd = build_qsub_command(
            "/path/with spaces/script.sh",
            name="my job",
            output_path="/home/user dir/output.log",
        )
        self.assertIn("'/path/with spaces/script.sh'", cmd)
        self.assertIn("'my job'", cmd)
        self.assertIn("'/home/user dir/output.log'", cmd)


class TestParseQsubOutput(unittest.TestCase):
    def test_standard_format(self):
        self.assertEqual(parse_qsub_output("320659.myserver"), "320659.myserver")

    def test_number_only(self):
        self.assertEqual(parse_qsub_output("320659"), "320659")

    def test_empty(self):
        self.assertIsNone(parse_qsub_output(""))

    def test_whitespace(self):
        self.assertEqual(parse_qsub_output("  320659.myserver\n"), "320659.myserver")

    def test_garbage(self):
        self.assertIsNone(parse_qsub_output("some error message"))


class TestHostInfoScheduler(unittest.TestCase):
    def test_scheduler_field_default_none(self):
        h = HostInfo(
            name="test", hostname="1.2.3.4", port=22,
            user="u", aliases=[], extra_options={},
        )
        self.assertIsNone(h.scheduler)

    def test_scheduler_field_pbs(self):
        h = HostInfo(
            name="test", hostname="1.2.3.4", port=22,
            user="u", aliases=[], extra_options={},
            scheduler="pbs",
        )
        self.assertEqual(h.scheduler, "pbs")


# --- Mount point and path translation tests ---


from sft.state import derive_mountpoint, remote_to_local, local_to_remote


class TestDeriveMountpoint(unittest.TestCase):
    """Test the improved derive_mountpoint that mirrors full remote paths."""

    def test_absolute_path_mirrored(self):
        result = derive_mountpoint("myhost", "/home/user/Workspace/project")
        expected = os.path.expanduser("~/mnt/myhost/home/user/Workspace/project")
        self.assertEqual(result, expected)

    def test_trailing_slash_stripped(self):
        result = derive_mountpoint("host", "/path/to/dir/")
        expected = os.path.expanduser("~/mnt/host/path/to/dir")
        self.assertEqual(result, expected)

    def test_short_path(self):
        result = derive_mountpoint("myhost", "/project")
        expected = os.path.expanduser("~/mnt/myhost/project")
        self.assertEqual(result, expected)

    def test_deep_nested_path(self):
        result = derive_mountpoint("myhost", "/home/user/Workspace/myproject")
        expected = os.path.expanduser(
            "~/mnt/myhost/home/user/Workspace/myproject"
        )
        self.assertEqual(result, expected)

    def test_different_hosts_dont_collide(self):
        """Two hosts with same-named directories get different mount points."""
        r1 = derive_mountpoint("host-a", "/home/user/project")
        r2 = derive_mountpoint("host-b", "/home/user/project")
        self.assertNotEqual(r1, r2)

    def test_different_paths_dont_collide(self):
        """Different remote paths on same host get different mount points."""
        r1 = derive_mountpoint("myhost", "/home/user/Workspace/project1")
        r2 = derive_mountpoint("myhost", "/home/user/code/project1")
        self.assertNotEqual(r1, r2)


class TestRemoteToLocal(unittest.TestCase):
    """Test remote_to_local path translation."""

    def test_translates_absolute_path(self):
        result = remote_to_local("/home/user/project/file.py", "myhost")
        expected = os.path.expanduser("~/mnt/myhost/home/user/project/file.py")
        self.assertEqual(result, expected)

    def test_translates_root(self):
        result = remote_to_local("/", "host")
        expected = os.path.expanduser("~/mnt/host")
        self.assertEqual(result, expected)

    def test_passes_through_relative_path(self):
        result = remote_to_local("relative/path.py", "myhost")
        self.assertEqual(result, "relative/path.py")

    def test_passes_through_empty_string(self):
        result = remote_to_local("", "myhost")
        self.assertEqual(result, "")

    def test_deep_path(self):
        result = remote_to_local(
            "/home/user/Workspace/myproject/main.py", "myhost"
        )
        expected = os.path.expanduser(
            "~/mnt/myhost/home/user/Workspace/myproject/main.py"
        )
        self.assertEqual(result, expected)


class TestLocalToRemote(unittest.TestCase):
    """Test local_to_remote reverse path translation."""

    def test_translates_local_to_remote(self):
        local = os.path.expanduser("~/mnt/myhost/home/user/project/file.py")
        result = local_to_remote(local, "myhost")
        self.assertEqual(result, "/home/user/project/file.py")

    def test_translates_mount_root(self):
        local = os.path.expanduser("~/mnt/myhost")
        result = local_to_remote(local, "myhost")
        self.assertEqual(result, "/")

    def test_passes_through_non_mount_path(self):
        result = local_to_remote("/tmp/something", "myhost")
        self.assertEqual(result, "/tmp/something")

    def test_roundtrip_with_remote_to_local(self):
        remote = "/home/user/project/subdir/file.py"
        local = remote_to_local(remote, "myhost")
        roundtrip = local_to_remote(local, "myhost")
        self.assertEqual(roundtrip, remote)
