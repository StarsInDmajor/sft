"""Sync manifest: tracks what was synced, when, and detects changes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from sft.config import HostInfo
from sft.context import ExecutionContext
from sft.shell import rq as _rq
from sft.ui import Theme

MANIFEST_DIR = ".sft"
MANIFEST_FILE = "sync-manifest.json"


def _file_hash(path: str) -> str:
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    except (OSError, IOError):
        return ""
    return h.hexdigest()


def _scan_local(src_dir: str, excludes: List[str]) -> Dict[str, Any]:
    entries: Dict[str, Any] = {}
    src_dir = os.path.expanduser(src_dir)
    excluded_dirs = set()
    excluded_exts = set()
    for e in excludes:
        if "*" in e:
            excluded_exts.add(e.lstrip("*"))
        else:
            excluded_dirs.add(e)
    for root, dirs, files in os.walk(src_dir):
        for d in list(dirs):
            if d in excluded_dirs or d.startswith("."):
                dirs.remove(d)
        rel_root = os.path.relpath(root, src_dir)
        if rel_root == ".":
            rel_root = ""
        for fname in files:
            if fname.startswith("."):
                continue
            if any(fname.endswith(ext) for ext in excluded_exts):
                continue
            rel = os.path.join(rel_root, fname) if rel_root else fname
            full = os.path.join(root, fname)
            try:
                st = os.stat(full)
                entries[rel] = {
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "hash": _file_hash(full),
                }
            except OSError:
                pass
    return entries


def build_manifest(
    src_dir: str,
    remote_path: str,
    excludes: List[str],
    sync_mode: str,
) -> Dict[str, Any]:
    return {
        "version": 1,
        "synced_at": time.time(),
        "src_dir": os.path.expanduser(src_dir),
        "remote_path": remote_path,
        "sync_mode": sync_mode,
        "excludes": excludes,
        "files": _scan_local(src_dir, excludes),
    }


def load_remote_manifest(
    host_info: HostInfo,
    remote_path: str,
    ctx: ExecutionContext,
) -> Optional[Dict[str, Any]]:
    manifest_path = os.path.join(remote_path, MANIFEST_DIR, MANIFEST_FILE)
    try:
        output = ctx.run_ssh(
            host_info,
            f"cat {_rq(manifest_path)} 2>/dev/null",
            capture=True,
            allow_dry_run_execute=True,
            silent=True,
        )
        if output:
            return json.loads(output)
    except (RuntimeError, json.JSONDecodeError):
        pass
    return None


def save_remote_manifest(
    host_info: HostInfo,
    remote_path: str,
    manifest: Dict[str, Any],
    ctx: ExecutionContext,
) -> None:
    manifest_path = os.path.join(remote_path, MANIFEST_DIR, MANIFEST_FILE)
    manifest_json = json.dumps(manifest, indent=2)
    encoded = base64.b64encode(manifest_json.encode()).decode()
    manifest_dir = os.path.join(remote_path, MANIFEST_DIR)
    ctx.run_ssh(
        host_info,
        f"mkdir -p {_rq(manifest_dir)} && "
        f"echo {encoded} | base64 -d > {_rq(manifest_path)}",
        allow_dry_run_execute=True,
    )


def compare_manifests(
    old: Dict[str, Any],
    new: Dict[str, Any],
) -> Tuple[List[str], List[str], List[str]]:
    old_files = old.get("files", {})
    new_files = new.get("files", {})

    added = []
    modified = []
    deleted = []

    for path, info in new_files.items():
        if path not in old_files:
            added.append(path)
        elif old_files[path].get("hash") != info.get("hash"):
            modified.append(path)

    for path in old_files:
        if path not in new_files:
            deleted.append(path)

    return added, modified, deleted


def format_delta(
    added: List[str],
    modified: List[str],
    deleted: List[str],
) -> str:
    lines = []
    if added:
        lines.append(f"{Theme.GREEN}+ {len(added)} added{Theme.CLR}")
        for p in added[:5]:
            lines.append(f"  + {p}")
        if len(added) > 5:
            lines.append(f"  ... and {len(added) - 5} more")
    if modified:
        lines.append(f"{Theme.YELLOW}~ {len(modified)} modified{Theme.CLR}")
        for p in modified[:5]:
            lines.append(f"  ~ {p}")
        if len(modified) > 5:
            lines.append(f"  ... and {len(modified) - 5} more")
    if deleted:
        lines.append(f"{Theme.RED}- {len(deleted)} deleted{Theme.CLR}")
        for p in deleted[:5]:
            lines.append(f"  - {p}")
        if len(deleted) > 5:
            lines.append(f"  ... and {len(deleted) - 5} more")
    if not lines:
        lines.append(f"{Theme.GREEN}No changes since last sync{Theme.CLR}")
    return "\n".join(lines)
