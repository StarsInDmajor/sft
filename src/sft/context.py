"""Execution context: subprocess wrapper, SSH multiplexing, and connection management."""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from typing import List, Optional

from sft.config import HostInfo, SftConfig, get_config
from sft.ui import Spinner, Theme


class ExecutionContext:

    def __init__(
        self,
        *,
        dry_run: bool,
        verbose: bool,
        config: Optional[SftConfig] = None,
    ) -> None:
        self.dry_run = dry_run
        self.verbose = verbose
        self.config = config or get_config()
        self._ssh_control_initialized: set = set()
        self._ssh_control_lock = threading.Lock()

    def log(
        self, message: str, *, always: bool = False, icon: str = Theme.INFO
    ) -> None:
        if always or self.verbose:
            print(f"{icon} {message}")

    def run(
        self,
        cmd: List[str],
        *,
        capture: bool = False,
        description: Optional[str] = None,
        allow_dry_run_execute: bool = False,
        silent: bool = False,
    ) -> Optional[str]:
        cmd_str = " ".join(shlex.quote(p) for p in cmd)
        if self.verbose:
            Theme.command(cmd_str)

        if self.dry_run and not allow_dry_run_execute:
            if description:
                Theme.info("Dry-run", description)
            return None

        if description and not self.verbose:
            with Spinner(description):
                result = subprocess.run(
                    cmd, capture_output=capture, text=True
                )
        else:
            result = subprocess.run(cmd, capture_output=capture, text=True)

        if result.returncode != 0:
            if not silent:
                Theme.error(f"Command failed: {cmd_str}")
                if result.stderr:
                    print(f"{Theme.GREY}{result.stderr}{Theme.CLR}")
            raise RuntimeError(f"Command failed: {cmd}\n{result.stderr}")
        return result.stdout.strip() if capture else None

    def _ensure_ssh_control_master(self, host: HostInfo) -> None:
        if not self.config.ssh_control_master:
            return

        host_key = f"{host.user}@{host.hostname}:{host.port}"
        with self._ssh_control_lock:
            if host_key in self._ssh_control_initialized:
                return

        control_path = os.path.expanduser(self.config.ssh_control_path)
        control_path = control_path.replace("%h", host.hostname)
        control_path = control_path.replace("%p", str(host.port))
        control_path = control_path.replace("%r", host.user)

        control_dir = os.path.dirname(control_path)
        if control_dir:
            os.makedirs(control_dir, exist_ok=True)

        persist = (
            str(self.config.ssh_control_persist)
            if self.config.ssh_control_persist > 0
            else "yes"
        )
        cmd = [
            "ssh",
            "-p",
            str(host.port),
            "-o",
            "ControlMaster=yes",
            "-o",
            f"ControlPath={control_path}",
            "-o",
            f"ControlPersist={persist}",
            "-o",
            f"ConnectTimeout={self.config.ssh_connect_timeout}",
        ]
        for key, value in host.extra_options.items():
            cmd += ["-o", f"{key}={value}"]
        cmd += [host.ssh_target(), "true"]

        try:
            with Spinner(
                f"Initializing connection to {host.name}", icon=Theme.SSH
            ):
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.config.ssh_connect_timeout,
                )
            if result.returncode == 0:
                with self._ssh_control_lock:
                    self._ssh_control_initialized.add(host_key)
            else:
                Theme.warning(
                    f"SSH ControlMaster init failed for {host.name} (rc={result.returncode}), continuing without it"
                )
        except (subprocess.TimeoutExpired, Exception):
            Theme.warning(
                f"SSH ControlMaster initialization failed for {host.name}, continuing without it"
            )

    def run_ssh(
        self,
        host: HostInfo,
        remote_cmd: str,
        *,
        capture: bool = False,
        description: Optional[str] = None,
        allow_dry_run_execute: bool = False,
        silent: bool = False,
    ) -> Optional[str]:
        self._ensure_ssh_control_master(host)
        base = (
            ["ssh", "-p", str(host.port)] + self._ssh_options(host)
        )
        base += [host.ssh_target(), remote_cmd]
        try:
            return self.run(
                base,
                capture=capture,
                description=description,
                allow_dry_run_execute=allow_dry_run_execute,
                silent=silent,
            )
        except RuntimeError as e:
            if self.config.ssh_control_master and "255" in str(e):
                self._invalidate_ssh_control_master(host)
                base = (
                    ["ssh", "-p", str(host.port)]
                    + ["-o", f"ConnectTimeout={self.config.ssh_connect_timeout}"]
                )
                for key, value in host.extra_options.items():
                    base += ["-o", f"{key}={value}"]
                base += [host.ssh_target(), remote_cmd]
                return self.run(
                    base,
                    capture=capture,
                    description=description,
                    allow_dry_run_execute=allow_dry_run_execute,
                    silent=silent,
                )
            raise

    def _invalidate_ssh_control_master(self, host: HostInfo) -> None:
        host_key = f"{host.user}@{host.hostname}:{host.port}"
        with self._ssh_control_lock:
            self._ssh_control_initialized.discard(host_key)

        control_path = os.path.expanduser(self.config.ssh_control_path)
        control_path = control_path.replace("%h", host.hostname)
        control_path = control_path.replace("%p", str(host.port))
        control_path = control_path.replace("%r", host.user)

        if os.path.exists(control_path):
            try:
                os.unlink(control_path)
            except OSError:
                pass
        self.log(f"SSH control socket invalidated for {host.name}, reconnecting")

    def scp_to_remote(
        self, local_path: str, host: HostInfo, remote_path: str
    ) -> None:
        self._ensure_ssh_control_master(host)
        cmd = ["scp", "-P", str(host.port)] + self._ssh_options(host)
        cmd += [local_path, f"{host.ssh_target()}:{remote_path}"]
        description = f"Upload {os.path.basename(local_path)} to {host.name}"
        self.run(cmd, description=description)

    def scp_from_remote(
        self, host: HostInfo, remote_path: str, local_path: str
    ) -> None:
        self._ensure_ssh_control_master(host)
        cmd = ["scp", "-P", str(host.port)] + self._ssh_options(host)
        cmd += [f"{host.ssh_target()}:{remote_path}", local_path]
        description = (
            f"Download {os.path.basename(remote_path)} from {host.name}"
        )
        self.run(cmd, description=description)

    def _ssh_options(self, host: HostInfo) -> List[str]:
        opts = []

        if self.config.ssh_control_master:
            control_path = os.path.expanduser(self.config.ssh_control_path)
            control_path = control_path.replace("%h", host.hostname)
            control_path = control_path.replace("%p", str(host.port))
            control_path = control_path.replace("%r", host.user)
            opts += ["-o", f"ControlPath={control_path}"]

        opts += ["-o", f"ConnectTimeout={self.config.ssh_connect_timeout}"]

        for key, value in host.extra_options.items():
            opts += ["-o", f"{key}={value}"]
        return opts
