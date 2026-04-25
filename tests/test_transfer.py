"""Tests for transfer.py: rsync, archives, git bundles, and transfer orchestration."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sft.config import HostInfo, ParsedTarget, SftConfig
from sft.context import ExecutionContext

WSL_RS = HostInfo(
    name="testhost",
    hostname="10.0.0.1",
    port=2222,
    user="testuser",
    aliases=["rs"],
    extra_options={},
)

BETELGEUSE = HostInfo(
    name="testhost2",
    hostname="10.0.0.2",
    port=22,
    user="testuser",
    aliases=["bg"],
    extra_options={},
)


def _make_ctx(**overrides):
    ctx = ExecutionContext(dry_run=False, verbose=False, config=SftConfig())
    ctx._ensure_ssh_control_master = MagicMock()
    ctx._ssh_options = lambda h: ["-o", "ConnectTimeout=5"]
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


class TestCountFilesLocal(unittest.TestCase):
    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            from sft.transfer import count_files_local

            self.assertEqual(count_files_local(tmp), 0)

    def test_counts_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.py").write_text("x")
            (Path(tmp) / "b.py").write_text("y")
            (Path(tmp) / "sub").mkdir()
            (Path(tmp) / "sub" / "c.py").write_text("z")

            from sft.transfer import count_files_local

            self.assertEqual(count_files_local(tmp), 3)

    def test_excludes_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            (Path(tmp) / ".git" / "config").write_text("x")
            (Path(tmp) / "node_modules").mkdir()
            (Path(tmp) / "node_modules" / "pkg.js").write_text("y")
            (Path(tmp) / "main.py").write_text("z")

            from sft.transfer import count_files_local

            self.assertEqual(count_files_local(tmp), 1)

    def test_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "single.txt"
            f.write_text("hello")

            from sft.transfer import count_files_local

            self.assertEqual(count_files_local(str(f)), 1)

    def test_nonexistent_path(self):
        from sft.transfer import count_files_local

        self.assertEqual(count_files_local("/nonexistent/path"), 0)


class TestDetectGitRepo(unittest.TestCase):
    @patch("subprocess.run")
    def test_local_git_repo(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="/home/user/proj\n")

        ctx = _make_ctx()
        target = ParsedTarget(is_remote=False, path="/home/user/proj", host=None, user_override=None)

        from sft.transfer import detect_git_repo

        result = detect_git_repo(target, ctx)
        self.assertEqual(result, "/home/user/proj")

    @patch("subprocess.run")
    def test_local_no_git(self, mock_run):
        mock_run.side_effect = RuntimeError("not a git repo")

        ctx = _make_ctx()
        target = ParsedTarget(is_remote=False, path="/tmp/nogit", host=None, user_override=None)

        from sft.transfer import detect_git_repo

        result = detect_git_repo(target, ctx)
        self.assertIsNone(result)

    def test_remote_git_repo(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value="/home/user/proj")

        target = ParsedTarget(
            is_remote=True, path="~/proj", host=WSL_RS, user_override=None
        )

        from sft.transfer import detect_git_repo

        result = detect_git_repo(target, ctx)
        self.assertEqual(result, "/home/user/proj")
        ctx.run_ssh.assert_called_once()

    def test_remote_no_git(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(side_effect=RuntimeError("not a git repo"))

        target = ParsedTarget(
            is_remote=True, path="~/proj", host=WSL_RS, user_override=None
        )

        from sft.transfer import detect_git_repo

        result = detect_git_repo(target, ctx)
        self.assertIsNone(result)


class TestCreateLocalArchive(unittest.TestCase):
    @patch("subprocess.Popen")
    def test_creates_archive(self, mock_popen):
        mock_tar = MagicMock()
        mock_tar.stdout = MagicMock()
        mock_tar.wait.return_value = 0
        mock_zstd = MagicMock()
        mock_zstd.wait.return_value = 0
        mock_popen.side_effect = [mock_tar, mock_zstd]

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "project"
            src.mkdir()
            (src / "file.txt").write_text("hello")

            ctx = _make_ctx()

            from sft.transfer import create_local_archive

            archive_path, temp_dir = create_local_archive(str(src), ctx)
            self.assertTrue(archive_path.endswith("payload.tar.zst"))
            self.assertEqual(mock_popen.call_count, 2)

    def test_dry_run(self):
        ctx = _make_ctx(dry_run=True)
        ctx.log = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "project"
            src.mkdir()

            from sft.transfer import create_local_archive

            archive_path, temp_dir = create_local_archive(str(src), ctx)
            self.assertTrue(archive_path.endswith("payload.tar.zst"))
            ctx.log.assert_called_once()


class TestDecompressLocalArchive(unittest.TestCase):
    def test_dry_run(self):
        ctx = _make_ctx(dry_run=True)
        ctx.log = MagicMock()

        from sft.transfer import decompress_local_archive

        decompress_local_archive("/tmp/test.tar.zst", "/tmp/dest", ctx)
        ctx.log.assert_called_once()

    @patch("sft.transfer.subprocess.run")
    def test_decompresses(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "dest"
            dest.mkdir()
            archive = Path(tmp) / "test.tar.zst"
            archive.touch()

            ctx = _make_ctx()

            from sft.transfer import decompress_local_archive

            decompress_local_archive(str(archive), str(dest), ctx)
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertIn("tar", cmd)
            self.assertIn("--use-compress-program=zstd -d", cmd)


class TestCopySingleFile(unittest.TestCase):
    def test_local_to_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_file = Path(tmp) / "src.txt"
            src_file.write_text("hello")
            dst_file = Path(tmp) / "sub" / "dst.txt"

            src_target = ParsedTarget(is_remote=False, path=str(src_file), host=None, user_override=None)
            dst_target = ParsedTarget(is_remote=False, path=str(dst_file), host=None, user_override=None)
            ctx = _make_ctx()

            from sft.transfer import copy_single_file

            copy_single_file(src_target, str(src_file), dst_target, str(dst_file), ctx)
            self.assertTrue(dst_file.exists())
            self.assertEqual(dst_file.read_text(), "hello")

    def test_local_to_local_dry_run(self):
        ctx = _make_ctx(dry_run=True)
        ctx.log = MagicMock()

        src_target = ParsedTarget(is_remote=False, path="/tmp/src.txt", host=None, user_override=None)
        dst_target = ParsedTarget(is_remote=False, path="/tmp/dst.txt", host=None, user_override=None)

        from sft.transfer import copy_single_file

        copy_single_file(src_target, "/tmp/src.txt", dst_target, "/tmp/dst.txt", ctx)
        ctx.log.assert_called_once()

    def test_remote_to_local(self):
        ctx = _make_ctx()
        ctx.scp_from_remote = MagicMock()

        src_target = ParsedTarget(is_remote=True, path="~/file.txt", host=WSL_RS, user_override=None)
        dst_target = ParsedTarget(is_remote=False, path="/tmp/dst.txt", host=None, user_override=None)

        from sft.transfer import copy_single_file

        copy_single_file(src_target, "~/file.txt", dst_target, "/tmp/dst.txt", ctx)
        ctx.scp_from_remote.assert_called_once()

    def test_local_to_remote(self):
        ctx = _make_ctx()
        ctx.scp_to_remote = MagicMock()
        ctx.run_ssh = MagicMock()

        src_target = ParsedTarget(is_remote=False, path="/tmp/src.txt", host=None, user_override=None)
        dst_target = ParsedTarget(is_remote=True, path="~/dst.txt", host=WSL_RS, user_override=None)

        from sft.transfer import copy_single_file

        copy_single_file(src_target, "/tmp/src.txt", dst_target, "~/dst.txt", ctx)
        ctx.scp_to_remote.assert_called_once()

    def test_remote_to_remote(self):
        ctx = _make_ctx()
        ctx.scp_from_remote = MagicMock()
        ctx.scp_to_remote = MagicMock()
        ctx.run_ssh = MagicMock()

        src_target = ParsedTarget(is_remote=True, path="~/src.txt", host=WSL_RS, user_override=None)
        dst_target = ParsedTarget(is_remote=True, path="~/dst.txt", host=BETELGEUSE, user_override=None)

        from sft.transfer import copy_single_file

        copy_single_file(src_target, "~/src.txt", dst_target, "~/dst.txt", ctx)
        ctx.scp_from_remote.assert_called_once()
        ctx.scp_to_remote.assert_called_once()


class TestRunRegularRsync(unittest.TestCase):
    @patch("sft.transfer.subprocess.run")
    def test_local_to_local(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()

        from sft.transfer import run_regular_rsync

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            dst = Path(tmp) / "dst"
            dst.mkdir()

            run_regular_rsync(str(src), str(dst), None, None, ctx)
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "rsync")
            self.assertIn("-a", args)

    @patch("sft.transfer.subprocess.run")
    def test_local_to_remote(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        ctx.run_ssh = MagicMock()

        from sft.transfer import run_regular_rsync

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()

            run_regular_rsync(str(src), "~/dst", None, WSL_RS, ctx)
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "rsync")
            self.assertIn("-e", args)

    @patch("sft.transfer.subprocess.run")
    def test_remote_to_local(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        ctx.run_ssh = MagicMock()

        from sft.transfer import run_regular_rsync

        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "dst"
            dst.mkdir()

            run_regular_rsync("~/src", str(dst), WSL_RS, None, ctx)
            mock_run.assert_called_once()

    @patch("sft.transfer.subprocess.run")
    def test_remote_to_remote(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        ctx.run_ssh = MagicMock()

        from sft.transfer import run_regular_rsync

        run_regular_rsync("~/src", "~/dst", WSL_RS, BETELGEUSE, ctx)
        mock_run.assert_called_once()
        env = mock_run.call_args[1]["env"]
        self.assertIn("RSYNC_RSH", env)

    @patch("sft.transfer.subprocess.run")
    def test_compression_auto_wan(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        ctx = _make_ctx()
        ctx.run_ssh = MagicMock()

        from sft.transfer import run_regular_rsync

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()

            run_regular_rsync(str(src), "~/dst", None, WSL_RS, ctx)
            args = mock_run.call_args[0][0]
            self.assertIn("--compress", args)

    @patch("sft.transfer.subprocess.run")
    def test_compression_disabled(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        config = SftConfig()
        config.rsync_compression = "never"
        ctx = _make_ctx(config=config)
        ctx.run_ssh = MagicMock()

        from sft.transfer import run_regular_rsync

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()

            run_regular_rsync(str(src), "~/dst", None, WSL_RS, ctx)
            args = mock_run.call_args[0][0]
            self.assertNotIn("--compress", args)

    def test_dry_run(self):
        ctx = _make_ctx(dry_run=True)
        ctx.log = MagicMock()
        ctx.run_ssh = MagicMock()

        from sft.transfer import run_regular_rsync

        run_regular_rsync("~/src", "~/dst", WSL_RS, BETELGEUSE, ctx)
        ctx.log.assert_called()

    @patch("sft.transfer.subprocess.run")
    def test_compression_always(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        config = SftConfig()
        config.rsync_compression = "always"
        ctx = _make_ctx(config=config)

        from sft.transfer import run_regular_rsync

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            dst = Path(tmp) / "dst"
            dst.mkdir()

            run_regular_rsync(str(src), str(dst), None, None, ctx)
            args = mock_run.call_args[0][0]
            self.assertIn("--compress", args)


class TestProbeSourceRemote(unittest.TestCase):
    def test_remote_dir_with_git(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            return_value=json.dumps({
                "exists": True,
                "is_dir": True,
                "git_root": "/home/user/proj",
                "envrc_dir": "/home/user/proj",
                "file_count": 0,
            })
        )

        target = ParsedTarget(is_remote=True, path="~/proj", host=WSL_RS, user_override=None)

        from sft.transfer import probe_source_remote

        result = probe_source_remote(target, ctx)
        self.assertTrue(result.exists)
        self.assertTrue(result.is_dir)
        self.assertEqual(result.git_root, "/home/user/proj")

    def test_remote_file(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            return_value=json.dumps({
                "exists": True,
                "is_dir": False,
                "git_root": None,
                "envrc_dir": None,
                "file_count": 1,
            })
        )

        target = ParsedTarget(is_remote=True, path="~/file.txt", host=WSL_RS, user_override=None)

        from sft.transfer import probe_source_remote

        result = probe_source_remote(target, ctx)
        self.assertTrue(result.exists)
        self.assertFalse(result.is_dir)
        self.assertEqual(result.file_count, 1)

    def test_nonexistent(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(
            return_value=json.dumps({
                "exists": False,
                "is_dir": False,
                "git_root": None,
                "envrc_dir": None,
                "file_count": 0,
            })
        )

        target = ParsedTarget(is_remote=True, path="~/missing", host=WSL_RS, user_override=None)

        from sft.transfer import probe_source_remote

        result = probe_source_remote(target, ctx)
        self.assertFalse(result.exists)

    def test_ssh_failure_fallback(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(side_effect=RuntimeError("connection refused"))

        target = ParsedTarget(is_remote=True, path="~/proj", host=WSL_RS, user_override=None)

        from sft.transfer import probe_source_remote

        result = probe_source_remote(target, ctx)
        self.assertFalse(result.exists)
        self.assertEqual(result.file_count, 0)


class TestHasGitRefs(unittest.TestCase):
    def test_remote_has_refs(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value="abc123 HEAD\n")

        target = ParsedTarget(is_remote=True, path="~/proj", host=WSL_RS, user_override=None)

        from sft.transfer import has_git_refs

        self.assertTrue(has_git_refs(target, ctx))

    def test_remote_no_refs(self):
        ctx = _make_ctx()
        ctx.run_ssh = MagicMock(return_value="")

        target = ParsedTarget(is_remote=True, path="~/proj", host=WSL_RS, user_override=None)

        from sft.transfer import has_git_refs

        self.assertFalse(has_git_refs(target, ctx))

    def test_local_has_refs(self):
        ctx = _make_ctx()
        ctx.run = MagicMock(return_value="abc123 HEAD\n")

        target = ParsedTarget(is_remote=False, path="/tmp/proj", host=None, user_override=None)

        from sft.transfer import has_git_refs

        self.assertTrue(has_git_refs(target, ctx))

    def test_local_no_refs(self):
        ctx = _make_ctx()
        ctx.run = MagicMock(return_value="")

        target = ParsedTarget(is_remote=False, path="/tmp/proj", host=None, user_override=None)

        from sft.transfer import has_git_refs

        self.assertFalse(has_git_refs(target, ctx))


class TestPerformTransfer(unittest.TestCase):
    @patch("sft.transfer.run_regular_rsync")
    def test_local_to_local_small_project(self, mock_rsync):
        ctx = _make_ctx()

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "file.txt").write_text("hello")
            dst = Path(tmp) / "dst"
            dst.mkdir()

            src_target = ParsedTarget(is_remote=False, path=str(src), host=None, user_override=None)
            dst_target = ParsedTarget(is_remote=False, path=str(dst), host=None, user_override=None)

            args = MagicMock()
            args.full_flake = False
            args.force_compress = False
            args.no_compress = False

            from sft.transfer import perform_transfer

            perform_transfer(src_target, dst_target, args, ctx)
            mock_rsync.assert_called_once()

    @patch("sft.transfer.transfer_via_archive")
    def test_local_to_local_large_project(self, mock_archive):
        ctx = _make_ctx()

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            for i in range(60):
                (src / f"file{i}.txt").write_text(f"content {i}")
            dst = Path(tmp) / "dst"
            dst.mkdir()

            src_target = ParsedTarget(is_remote=False, path=str(src), host=None, user_override=None)
            dst_target = ParsedTarget(is_remote=False, path=str(dst), host=None, user_override=None)

            args = MagicMock()
            args.full_flake = False
            args.force_compress = False
            args.no_compress = False

            from sft.transfer import perform_transfer

            perform_transfer(src_target, dst_target, args, ctx)
            mock_archive.assert_called_once()

    @patch("sft.transfer.transfer_git_bundle")
    def test_git_bundle_full_flake(self, mock_bundle):
        ctx = _make_ctx()
        ctx.run = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / ".git").mkdir()
            (src / "flake.nix").write_text("{}")
            dst = Path(tmp) / "dst"
            dst.mkdir()

            src_target = ParsedTarget(is_remote=False, path=str(src), host=None, user_override=None)
            dst_target = ParsedTarget(is_remote=False, path=str(dst), host=None, user_override=None)

            args = MagicMock()
            args.full_flake = True
            args.force_compress = False
            args.no_compress = False

            from sft.transfer import perform_transfer

            perform_transfer(src_target, dst_target, args, ctx)
            mock_bundle.assert_called_once()

    def test_dry_run_no_transfer(self):
        ctx = _make_ctx(dry_run=True)
        ctx.log = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "file.txt").write_text("hello")
            dst = Path(tmp) / "dst"
            dst.mkdir()

            src_target = ParsedTarget(is_remote=False, path=str(src), host=None, user_override=None)
            dst_target = ParsedTarget(is_remote=False, path=str(dst), host=None, user_override=None)

            args = MagicMock()
            args.full_flake = False
            args.force_compress = False
            args.no_compress = False

            from sft.transfer import perform_transfer

            perform_transfer(src_target, dst_target, args, ctx)

    @patch("sft.transfer.transfer_via_archive")
    def test_force_compress_overrides_threshold(self, mock_archive):
        ctx = _make_ctx()

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "file.txt").write_text("hello")
            dst = Path(tmp) / "dst"
            dst.mkdir()

            src_target = ParsedTarget(is_remote=False, path=str(src), host=None, user_override=None)
            dst_target = ParsedTarget(is_remote=False, path=str(dst), host=None, user_override=None)

            args = MagicMock()
            args.full_flake = False
            args.force_compress = True
            args.no_compress = False

            from sft.transfer import perform_transfer

            perform_transfer(src_target, dst_target, args, ctx)
            mock_archive.assert_called_once()

    @patch("sft.transfer.run_regular_rsync")
    def test_no_compress_overrides_threshold(self, mock_rsync):
        ctx = _make_ctx()

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            for i in range(60):
                (src / f"file{i}.txt").write_text(f"content {i}")
            dst = Path(tmp) / "dst"
            dst.mkdir()

            src_target = ParsedTarget(is_remote=False, path=str(src), host=None, user_override=None)
            dst_target = ParsedTarget(is_remote=False, path=str(dst), host=None, user_override=None)

            args = MagicMock()
            args.full_flake = False
            args.force_compress = False
            args.no_compress = True

            from sft.transfer import perform_transfer

            perform_transfer(src_target, dst_target, args, ctx)
            mock_rsync.assert_called_once()


class TestTransferViaArchive(unittest.TestCase):
    @patch("sft.transfer.create_local_archive")
    @patch("sft.transfer.decompress_local_archive")
    def test_local_to_local(self, mock_decompress, mock_create):
        with tempfile.TemporaryDirectory() as tmp:
            archive_dir = Path(tmp) / "archive"
            archive_dir.mkdir()
            archive_file = archive_dir / "payload.tar.zst"
            archive_file.touch()

            mock_create.return_value = (str(archive_file), archive_dir)

            src = Path(tmp) / "src"
            src.mkdir()
            dst = Path(tmp) / "dst"
            dst.mkdir()

            src_target = ParsedTarget(is_remote=False, path=str(src), host=None, user_override=None)
            dst_target = ParsedTarget(is_remote=False, path=str(dst), host=None, user_override=None)

            ctx = _make_ctx()

            from sft.transfer import transfer_via_archive

            transfer_via_archive(src_target, dst_target, ctx)
            mock_create.assert_called_once()
            mock_decompress.assert_called_once()

    def test_dry_run(self):
        ctx = _make_ctx(dry_run=True)
        ctx.log = MagicMock()

        src_target = ParsedTarget(is_remote=False, path="/tmp/src", host=None, user_override=None)
        dst_target = ParsedTarget(is_remote=False, path="/tmp/dst", host=None, user_override=None)

        from sft.transfer import transfer_via_archive

        with patch("sft.transfer.create_local_archive") as mock_create:
            with tempfile.TemporaryDirectory() as tmp:
                archive_dir = Path(tmp)
                mock_create.return_value = (f"{tmp}/payload.tar.zst", archive_dir)
                transfer_via_archive(src_target, dst_target, ctx)

        self.assertTrue(ctx.log.called)


class TestEnsureLocalParent(unittest.TestCase):
    def test_creates_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "a" / "b" / "c" / "file.txt"
            path = str(nested)

            from sft.transfer import ensure_local_parent

            ensure_local_parent(path)
            self.assertTrue((Path(tmp) / "a" / "b" / "c").exists())


if __name__ == "__main__":
    unittest.main()
