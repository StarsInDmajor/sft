"""File transfer: rsync, archives, git bundles, and multi-mode orchestration."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sft.config import (
    HostInfo,
    ParsedTarget,
    determine_mode,
)
from sft.context import ExecutionContext
from sft.ui import Theme


EXCLUDED_DIRS = frozenset(
    {".git", ".venv", "__pycache__", ".mypy_cache", "node_modules"}
)


def count_files_local(path: str) -> int:
    if not Path(path).exists():
        return 0
    total = 0
    for root, dirs, filenames in os.walk(path):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        total += len(filenames)
    return total or (1 if Path(path).is_file() else 0)


def count_files_remote(
    target: ParsedTarget,
    ctx: ExecutionContext,
    *,
    allow_dry_run_execute: bool = False,
) -> int:
    assert target.host
    path = target.path
    script = textwrap.dedent(
        """
        import os, sys
        path = os.path.abspath(%s)
        if not os.path.exists(path):
            print(0)
            sys.exit(0)
        if os.path.isfile(path):
            print(1)
            sys.exit(0)
        excluded = {".git", ".direnv", ".venv", "__pycache__", ".mypy_cache", "node_modules", "result"}
        total = 0
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in excluded]
            total += len(files)
        print(total)
        """
    ) % json.dumps(path)
    output = ctx.run_ssh(
        target.host,
        f"python3 - <<'PY'\n{script}\nPY",
        capture=True,
        allow_dry_run_execute=allow_dry_run_execute,
    )
    try:
        return int(output or "0")
    except ValueError:
        return 0


def detect_git_repo(
    target: ParsedTarget,
    ctx: ExecutionContext,
    *,
    allow_dry_run_execute: bool = False,
) -> Optional[str]:
    if target.is_remote:
        assert target.host
        cmd = f"cd {shlex.quote(target.path)} && git rev-parse --show-toplevel"
        try:
            return ctx.run_ssh(
                target.host,
                cmd,
                capture=True,
                allow_dry_run_execute=allow_dry_run_execute,
            )
        except RuntimeError:
            return None
    try:
        resolved = Path(target.path).resolve(strict=False)
        output = ctx.run(
            ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
            capture=True,
            allow_dry_run_execute=allow_dry_run_execute,
        )
        return output
    except RuntimeError:
        return None


def ensure_local_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def ensure_remote_parent(
    host: HostInfo, remote_path: str, ctx: ExecutionContext
) -> None:
    """Ensure the parent directory of *remote_path* exists on the remote host."""
    parent = os.path.dirname(remote_path.rstrip("/"))
    if parent:
        ctx.run_ssh(host, f"mkdir -p {shlex.quote(parent)}", allow_dry_run_execute=True)


def create_local_archive(path: str, ctx: ExecutionContext) -> Tuple[str, Path]:
    src = Path(path).resolve()
    temp_dir = Path(tempfile.mkdtemp(prefix="sft-"))
    zst_path = temp_dir / "payload.tar.zst"

    if ctx.dry_run:
        ctx.log(f"Dry-run: create archive at {zst_path}")
        return str(zst_path), temp_dir

    tar_cmd = ["tar", "-C", str(src.parent), "-cf", "-", src.name]
    zstd_level = ctx.config.zstd_level
    zstd_cmd = [
        "zstd",
        f"-{zstd_level}",
        "-T0",
        "-f",
        "-o",
        str(zst_path),
    ]

    tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE)
    zstd_proc = subprocess.Popen(zstd_cmd, stdin=tar_proc.stdout)
    if tar_proc.stdout:
        tar_proc.stdout.close()

    tar_returncode = tar_proc.wait()
    zstd_returncode = zstd_proc.wait()

    if tar_returncode != 0:
        raise RuntimeError(f"tar failed with code {tar_returncode}")
    if zstd_returncode != 0:
        raise RuntimeError(f"zstd compression failed with code {zstd_returncode}")

    ctx.log(f"Created archive {zst_path}")
    return str(zst_path), temp_dir


def create_remote_archive(
    target: ParsedTarget, ctx: ExecutionContext
) -> Optional[Tuple[str, str]]:
    assert target.host
    if ctx.dry_run:
        placeholder = f"/tmp/sft-dry-{uuid.uuid4().hex}.tar.zst"
        ctx.log(f"Dry-run: create remote archive at {placeholder}")
        return (placeholder, "/tmp")
    path = target.path
    if "/" in path:
        dir_part = path.rsplit("/", 1)[0]
        name_part = path.rsplit("/", 1)[1]
    else:
        dir_part = "."
        name_part = path
    tmp_id = uuid.uuid4().hex
    zstd_level = ctx.config.zstd_level
    command = f"""set -euo pipefail
tmp="/tmp/sft-archive-{tmp_id}"
mkdir -p "$tmp"
cd {shlex.quote(dir_part)} || exit 1
tar -cf "$tmp/payload.tar" {shlex.quote(name_part)}
zstd -{zstd_level} -T0 -f -o "$tmp/payload.tar.zst" "$tmp/payload.tar"
rm -f "$tmp/payload.tar"
printf '%s\\n%s' "$tmp/payload.tar.zst" "$tmp"
"""
    result = ctx.run_ssh(
        target.host,
        command,
        capture=True,
        description=f"Creating remote archive of {name_part} on {target.host.name}",
    )
    if not result:
        raise RuntimeError("Failed to create remote archive")
    lines = result.strip().split("\n")
    archive_path = lines[0]
    temp_dir = lines[1] if len(lines) > 1 else os.path.dirname(archive_path)
    ctx.log(f"Created remote archive {archive_path}")
    return (archive_path, temp_dir)


def decompress_local_archive(archive: str, dest: str, ctx: ExecutionContext) -> None:
    if ctx.dry_run:
        ctx.log(f"Dry-run: decompress {archive} -> {dest}")
        return
    dest_path = Path(dest)
    if dest_path.exists() and dest_path.is_dir():
        extract_dir = dest_path
    else:
        if dest_path.exists():
            if dest_path.is_dir():
                shutil.rmtree(dest_path)
            else:
                dest_path.unlink()
        extract_dir = dest_path.parent
    extract_dir.mkdir(parents=True, exist_ok=True)
    ctx.run(
        [
            "tar",
            "--use-compress-program=zstd -d",
            "-xf",
            archive,
            "-C",
            str(extract_dir),
        ]
    )


def decompress_remote_archive(
    host: HostInfo,
    remote_archive: str,
    dest: str,
    ctx: ExecutionContext,
) -> None:
    if ctx.dry_run:
        ctx.log(f"Dry-run: decompress remote {remote_archive} -> {dest}")
        return
    check_cmd = f"test -d {shlex.quote(dest)}"
    try:
        ctx.run_ssh(host, check_cmd)
        is_dir = True
    except RuntimeError:
        is_dir = False
    if is_dir:
        cmd = f"""set -euo pipefail
cd {shlex.quote(dest)} || exit 1
tar --use-compress-program='zstd -d' -xf {shlex.quote(remote_archive)}
rm -f {shlex.quote(remote_archive)}
"""
    else:
        dest_parent = os.path.dirname(dest.rstrip("/")) or "/"
        cmd = f"""set -euo pipefail
rm -rf {shlex.quote(dest)}
mkdir -p {shlex.quote(dest_parent)}
cd {shlex.quote(dest_parent)} || exit 1
tar --use-compress-program='zstd -d' -xf {shlex.quote(remote_archive)}
rm -f {shlex.quote(remote_archive)}
"""
    ctx.run_ssh(host, cmd)


def copy_single_file(
    src_target: ParsedTarget,
    src_file: str,
    dst_target: ParsedTarget,
    dst_file: str,
    ctx: ExecutionContext,
) -> None:
    if not src_target.is_remote and not dst_target.is_remote:
        if ctx.dry_run:
            ctx.log(f"Dry-run: copy local {src_file} -> {dst_file}")
            return
        ensure_local_parent(dst_file)
        shutil.copy2(src_file, dst_file)
        ctx.log(f"Copied {src_file} to {dst_file}")
        return
    if src_target.is_remote and not dst_target.is_remote:
        ensure_local_parent(dst_file)
        assert src_target.host
        ctx.scp_from_remote(src_target.host, src_file, dst_file)
        return
    if not src_target.is_remote and dst_target.is_remote:
        assert dst_target.host
        ensure_remote_parent(dst_target.host, dst_file, ctx)
        ctx.scp_to_remote(src_file, dst_target.host, dst_file)
        return
    assert src_target.host and dst_target.host
    tmp_dir = Path(tempfile.mkdtemp(prefix="sft-pass-"))
    intermediate = tmp_dir / Path(src_file).name
    ctx.scp_from_remote(src_target.host, src_file, str(intermediate))
    ensure_remote_parent(dst_target.host, dst_file, ctx)
    ctx.scp_to_remote(str(intermediate), dst_target.host, dst_file)
    shutil.rmtree(tmp_dir)


def run_regular_rsync(
    src: str,
    dst: str,
    src_remote: Optional[HostInfo],
    dst_remote: Optional[HostInfo],
    ctx: ExecutionContext,
) -> None:
    args = [
        "rsync",
        "-a",
        "--delete",
        "--partial",
        "--info=progress2",
    ]

    compress_mode = ctx.config.rsync_compression
    is_wan_transfer = src_remote is not None or dst_remote is not None

    should_compress = False
    if compress_mode == "always":
        should_compress = True
    elif compress_mode == "auto" and is_wan_transfer:
        should_compress = True

    if should_compress:
        args += ["--compress"]
        ctx.log("rsync compression enabled (WAN optimization)")

    if src_remote and dst_remote:
        dst_ssh_opts = [
            "-p",
            str(dst_remote.port),
        ] + ctx._ssh_options(dst_remote)
        dst_ssh_cmd = " ".join(shlex.quote(part) for part in ["ssh"] + dst_ssh_opts)
        args += ["-e", dst_ssh_cmd]

        ensure_remote_parent(dst_remote, dst, ctx)

        src_ssh_opts = [
            "-p",
            str(src_remote.port),
        ] + ctx._ssh_options(src_remote)
        src_ssh_cmd = " ".join(shlex.quote(part) for part in ["ssh"] + src_ssh_opts)
        src_spec = f"{src_remote.ssh_target()}:{src}"
        dst_spec = f"{dst_remote.ssh_target()}:{dst}"

        env = os.environ.copy()
        env["RSYNC_RSH"] = src_ssh_cmd

        if ctx.dry_run:
            ctx.log(f"Dry-run: rsync remote->remote {src_spec} -> {dst_spec}")
            return

        args += [src_spec, dst_spec]
        subprocess.run(args, env=env, check=True)
        return

    if dst_remote:
        ensure_remote_parent(dst_remote, dst, ctx)
    ssh_host = src_remote or dst_remote
    if ssh_host:
        ssh_cmd = " ".join(
            shlex.quote(part)
            for part in [
                "ssh",
                "-p",
                str(ssh_host.port),
            ]
            + ctx._ssh_options(ssh_host)
        )
        args += ["-e", ssh_cmd]
    if not dst_remote:
        ensure_local_parent(dst)
    src_spec = f"{src_remote.ssh_target()}:{src}" if src_remote else src
    dst_spec = f"{dst_remote.ssh_target()}:{dst}" if dst_remote else dst
    args += [src_spec, dst_spec]
    ctx.run(args)


def build_intermediate_local_path() -> Path:
    return Path(tempfile.mkdtemp(prefix="sft-temp-"))


@dataclass
class SourceProbeResult:
    git_root: Optional[str]
    envrc_dir: Optional[str]
    file_count: int
    is_dir: bool
    exists: bool


def probe_source_remote(
    target: ParsedTarget, ctx: ExecutionContext
) -> SourceProbeResult:
    assert target.host
    path = target.path
    if not path.startswith("~") and not path.startswith("/"):
        path = f"~/{path}"

    script = textwrap.dedent(
        """
        import os, sys, json, subprocess

        path = os.path.expanduser(%s)
        path = os.path.abspath(path)

        result = {
            "exists": False,
            "is_dir": False,
            "git_root": None,
            "envrc_dir": None,
            "file_count": 0
        }

        if not os.path.exists(path):
            print(json.dumps(result))
            sys.exit(0)

        result["exists"] = True
        result["is_dir"] = os.path.isdir(path)

        try:
            git_output = subprocess.run(
                ["git", "-C", path, "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True
            )
            result["git_root"] = git_output.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        check_path = result["git_root"] or path
        if result["is_dir"]:
            search_path = check_path
        else:
            search_path = os.path.dirname(check_path)

        current = search_path
        while True:
            if os.path.isfile(os.path.join(current, ".envrc")):
                result["envrc_dir"] = current
                break
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

        excluded = {".git", ".direnv", ".venv", "__pycache__", ".mypy_cache", "node_modules", "result"}
        if not result["git_root"] and result["is_dir"]:
            count = 0
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in excluded]
                count += len(files)
            result["file_count"] = count
        elif os.path.isfile(path):
            result["file_count"] = 1

        print(json.dumps(result))
        """
    ) % json.dumps(path)

    try:
        output = ctx.run_ssh(
            target.host,
            f"python3 - <<'PY'\n{script}\nPY",
            capture=True,
            description=f"Probing source {target.path} on {target.host.name}",
            allow_dry_run_execute=True,
        )
        if output:
            data = json.loads(output)
            return SourceProbeResult(
                git_root=data.get("git_root"),
                envrc_dir=data.get("envrc_dir"),
                file_count=data.get("file_count", 0),
                is_dir=data.get("is_dir", False),
                exists=data.get("exists", False),
            )
    except (RuntimeError, json.JSONDecodeError) as e:
        Theme.warning(f"Batch probe failed: {e}, falling back to individual probes")

    return SourceProbeResult(
        git_root=None, envrc_dir=None, file_count=0, is_dir=False, exists=False
    )


def has_git_refs(
    target: ParsedTarget,
    ctx: ExecutionContext,
    *,
    allow_dry_run_execute: bool = False,
) -> bool:
    try:
        if target.is_remote:
            assert target.host
            cmd = f"cd {shlex.quote(target.path)} && git show-ref --head"
            output = ctx.run_ssh(
                target.host,
                cmd,
                capture=True,
                allow_dry_run_execute=allow_dry_run_execute,
            )
            return bool(output and output.strip())
        else:
            output = ctx.run(
                ["git", "-C", target.path, "show-ref", "--head"],
                capture=True,
                allow_dry_run_execute=allow_dry_run_execute,
            )
            return bool(output and output.strip())
    except RuntimeError:
        return False


def create_remote_git_bundle(
    target: ParsedTarget, bundle_name: str, ctx: ExecutionContext
) -> Optional[str]:
    assert target.host
    if ctx.dry_run:
        placeholder = f"/tmp/sft-dry-{uuid.uuid4().hex}.bundle"
        ctx.log(f"Dry-run: create remote git bundle at {placeholder}")
        return placeholder
    path = target.path
    if not path.startswith("~") and not path.startswith("/"):
        path = f"~/{path}"
    script = textwrap.dedent(
        """
        import os, subprocess, sys, tempfile
        git_path = os.path.expanduser(%s)
        git_path = os.path.realpath(git_path)
        if not os.path.isdir(os.path.join(git_path, '.git')):
            sys.exit(1)
        tmpdir = tempfile.mkdtemp(prefix='sft-')
        bundle_path = os.path.join(tmpdir, %s)
        try:
            subprocess.run(['git', '-C', git_path, 'bundle', 'create', bundle_path, '--all'], check=True)
            print(bundle_path)
        except subprocess.CalledProcessError:
            os.rmdir(tmpdir)
            sys.exit(1)
        """
    ) % (json.dumps(path), json.dumps(bundle_name))
    try:
        return ctx.run_ssh(
            target.host,
            f"python3 - <<'PY'\n{script}\nPY",
            capture=True,
            description=f"Creating remote git bundle on {target.host.name}",
        )
    except RuntimeError:
        Theme.warning("Failed to create git bundle, falling back to archive transfer.")
        return None


def run_clone_from_bundle(
    host: HostInfo,
    bundle_path: str,
    dest: str,
    src_path: str,
    ctx: ExecutionContext,
) -> None:
    check_cmd = f"test -d {shlex.quote(dest)}"
    try:
        ctx.run_ssh(host, check_cmd)
        is_dir = True
    except RuntimeError:
        is_dir = False

    if is_dir:
        src_dir_name = os.path.basename(src_path.rstrip("/"))
        clone_dest = os.path.join(dest, src_dir_name)
    else:
        clone_dest = dest

    cmd_parts = ["set -euo pipefail"]
    if not is_dir:
        cmd_parts.append(f"rm -rf {shlex.quote(dest)}")
    else:
        cmd_parts.append(f"rm -rf {shlex.quote(clone_dest)}")
    cmd_parts.append(
        f"mkdir -p {shlex.quote(os.path.dirname(clone_dest.rstrip('/')) or '/')}"
    )
    cmd_parts.append(f"git clone {shlex.quote(bundle_path)} {shlex.quote(clone_dest)}")
    cmd_parts.append(f"rm -f {shlex.quote(bundle_path)}")
    cmd = "\n".join(cmd_parts)

    ctx.run_ssh(
        host,
        cmd,
        description=f"Cloning bundle to {clone_dest} on {host.name}",
    )


def transfer_git_bundle(
    src: ParsedTarget,
    dst: ParsedTarget,
    ctx: ExecutionContext,
) -> None:
    if not has_git_refs(src, ctx, allow_dry_run_execute=True):
        ctx.log(
            "Git repository has no refs (empty repo). Falling back to archive transfer."
        )
        transfer_via_archive(src, dst, ctx)
        return

    bundle_name = f"sft-{uuid.uuid4().hex}.bundle"

    local_bundle_dir = tempfile.mkdtemp(prefix="sft-bundle-")
    try:
        local_bundle_path = Path(local_bundle_dir) / bundle_name

        if dst.is_remote:
            assert dst.host
            check_cmd = f"test -d {shlex.quote(dst.path)}"
            try:
                ctx.run_ssh(dst.host, check_cmd)
                is_dest_dir = True
            except RuntimeError:
                is_dest_dir = False

            if is_dest_dir:
                src_dir_name = os.path.basename(src.path.rstrip("/"))
                final_dest_dir = os.path.join(dst.path, src_dir_name)
            else:
                final_dest_dir = dst.path
        else:
            dest_path = Path(dst.path)
            if dest_path.exists() and dest_path.is_dir():
                src_dir_name = os.path.basename(src.path.rstrip("/"))
                final_dest_dir = os.path.join(dst.path, src_dir_name)
            else:
                final_dest_dir = dst.path

        if src.is_remote:
            assert src.host
            remote_bundle = create_remote_git_bundle(src, bundle_name, ctx)
            if not remote_bundle:
                Theme.warning(
                    "Falling back to archive transfer due to git bundle failure."
                )
                transfer_via_archive(src, dst, ctx)
                return
            ctx.scp_from_remote(src.host, remote_bundle, str(local_bundle_path))
            ctx.run_ssh(src.host, f"rm -f {shlex.quote(remote_bundle)}")
            remote_tmp_dir = os.path.dirname(remote_bundle)
            if remote_tmp_dir not in ("", "/", "/tmp"):
                ctx.run_ssh(src.host, f"rm -rf {shlex.quote(remote_tmp_dir)}")
        else:
            try:
                ctx.run(
                    [
                        "git",
                        "-C",
                        src.path,
                        "bundle",
                        "create",
                        str(local_bundle_path),
                        "--all",
                    ],
                    description=f"Creating local git bundle: {bundle_name}",
                )
            except RuntimeError:
                Theme.warning(
                    "Falling back to archive transfer due to git bundle failure."
                )
                transfer_via_archive(src, dst, ctx)
                return

        if dst.is_remote:
            assert dst.host
            remote_store = f"/tmp/{bundle_name}"
            ctx.scp_to_remote(str(local_bundle_path), dst.host, remote_store)
            run_clone_from_bundle(dst.host, remote_store, dst.path, src.path, ctx)
        else:
            dest_path = Path(dst.path)
            if dest_path.exists() and dest_path.is_dir():
                src_dir_name = os.path.basename(src.path.rstrip("/"))
                clone_dest = os.path.join(dst.path, src_dir_name)
                from sft.config import cleanup_path

                cleanup_path(clone_dest, ctx.dry_run)
            else:
                clone_dest = dst.path
                from sft.config import cleanup_path

                cleanup_path(dst.path, ctx.dry_run)
            ctx.run(
                ["git", "clone", str(local_bundle_path), clone_dest],
                description=f"Cloning bundle to {clone_dest}",
            )
    finally:
        if os.path.exists(local_bundle_dir):
            shutil.rmtree(local_bundle_dir)


def transfer_via_archive(
    src: ParsedTarget, dst: ParsedTarget, ctx: ExecutionContext
) -> None:
    cleanup_dir: Optional[Path] = None
    remote_temp_dir: Optional[str] = None
    if src.is_remote:
        assert src.host
        archive_result = create_remote_archive(src, ctx)
        if not archive_result:
            remote_archive_path = f"/tmp/sft-dry-{uuid.uuid4().hex}.tar.zst"
            remote_temp_dir = "/tmp"
        else:
            remote_archive_path, remote_temp_dir = archive_result
        temp_dir = Path(tempfile.mkdtemp(prefix="sft-archive-"))
        local_archive = temp_dir / Path(remote_archive_path).name
        cleanup_dir = temp_dir
        ctx.scp_from_remote(src.host, remote_archive_path, str(local_archive))
        ctx.run_ssh(src.host, f"rm -f {shlex.quote(remote_archive_path)}")
        if remote_temp_dir and remote_temp_dir != "/tmp":
            ctx.run_ssh(src.host, f"rm -rf {shlex.quote(remote_temp_dir)}")
    else:
        archive_path, archive_dir = create_local_archive(src.path, ctx)
        local_archive = Path(archive_path)
        cleanup_dir = archive_dir

    if dst.is_remote:
        assert dst.host
        remote_dest = f"/tmp/{local_archive.name}"
        ctx.scp_to_remote(str(local_archive), dst.host, remote_dest)
        decompress_remote_archive(dst.host, remote_dest, dst.path, ctx)
    else:
        decompress_local_archive(str(local_archive), dst.path, ctx)
    if cleanup_dir and cleanup_dir.exists() and not ctx.dry_run:
        shutil.rmtree(cleanup_dir)


def perform_transfer(
    src: ParsedTarget,
    dst: ParsedTarget,
    args: Any,
    ctx: ExecutionContext,
) -> None:
    mode = determine_mode(src, dst)
    Theme.info("Transfer Mode", mode)

    if src.is_remote:
        probe = probe_source_remote(src, ctx)
        git_root = probe.git_root
        file_count = probe.file_count
    else:
        git_root = detect_git_repo(src, ctx)
        file_count = count_files_local(src.path)

    Theme.info("File Count", file_count)
    if git_root:
        Theme.info("Git Root", git_root)

    threshold = ctx.config.compression_threshold
    if args.force_compress:
        Theme.info("Compression", "Forced")
    elif args.no_compress:
        Theme.info("Compression", "Disabled")
    elif file_count > threshold:
        Theme.info("Compression", f"Auto (>{threshold} files)")

    use_git_bundle = bool(
        git_root
        and (
            getattr(args, "full_flake", False) or os.path.realpath(src.path) == os.path.realpath(git_root)
        )
    )

    if use_git_bundle:
        Theme.step(Theme.GIT, "Transferring git bundle", src.path)
        transfer_git_bundle(src, dst, ctx)
    else:
        should_compress = args.force_compress or (
            not args.no_compress and file_count > threshold
        )
        if should_compress:
            Theme.step(Theme.ZIP, "Compressing and transferring", src.path)
            transfer_via_archive(src, dst, ctx)
        else:
            if mode == "local->local":
                Theme.step(Theme.XFER, "Syncing local files", src.path)
                run_regular_rsync(src.path, dst.path, None, None, ctx)
            elif mode == "local->remote":
                assert dst.host
                Theme.step(
                    Theme.XFER,
                    "Syncing to remote",
                    f"{dst.host.name}:{dst.path}",
                )
                run_regular_rsync(src.path, dst.path, None, dst.host, ctx)
            elif mode == "remote->local":
                assert src.host
                Theme.step(
                    Theme.XFER,
                    "Syncing from remote",
                    f"{src.host.name}:{src.path}",
                )
                run_regular_rsync(src.path, dst.path, src.host, None, ctx)
            else:
                assert src.host and dst.host
                Theme.info("Optimization", "Direct remote-to-remote transfer")
                Theme.step(
                    Theme.XFER,
                    "Syncing remote to remote",
                    f"{src.host.name} -> {dst.host.name}",
                )
                run_regular_rsync(src.path, dst.path, src.host, dst.host, ctx)

    # Run plugin post-transfer hooks (e.g. flake file sync via sft-nix)
    from sft.plugins import get_post_transfer_hooks

    for hook in get_post_transfer_hooks():
        try:
            hook(src, dst, args, ctx)
        except Exception as exc:
            Theme.warning(f"Post-transfer hook failed: {exc}")
