"""Configuration loading, host definitions, and target parsing."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from sft.context import ExecutionContext

try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False

from sft.ui import Theme


# Default configuration values — no hosts are hardcoded.
# Users must provide their own hosts via ~/.config/sft/config.yaml.
DEFAULT_CONFIG: Dict[str, Any] = {
    "hosts": {},
    "transfer": {
        "compression_threshold": 50,
        "zstd_level": 2,
        "rsync_compression": "auto",
        "rsync_options": ["-a", "--delete", "--partial"],
    },
    "ssh": {
        "control_master": True,
        "control_path": "~/.ssh/sft-control-%h-%p-%r",
        "control_persist": 600,
        "connect_timeout": 30,
    },
    "logging": {
        "verbose": False,
        "log_file": None,
    },
}


@dataclass
class SftConfig:
    """Configuration container for SFT."""

    hosts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    compression_threshold: int = 50
    zstd_level: int = 2
    rsync_compression: str = "auto"
    rsync_options: List[str] = field(
        default_factory=lambda: ["-a", "--delete", "--partial"]
    )
    ssh_control_master: bool = True
    ssh_control_path: str = "~/.ssh/sft-control-%h-%p-%r"
    ssh_control_persist: int = 600
    ssh_connect_timeout: int = 30
    verbose: bool = False
    log_file: Optional[str] = None


def load_config(config_path: Optional[str] = None) -> SftConfig:
    """Load configuration from file or use defaults."""
    config = SftConfig()

    if config_path is None:
        config_dir = os.path.expanduser("~/.config/sft")
        yaml_path = os.path.join(config_dir, "config.yaml")
        json_path = os.path.join(config_dir, "config.json")

        if os.path.exists(yaml_path):
            config_path = yaml_path
        elif os.path.exists(json_path):
            config_path = json_path

    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                if config_path.endswith(".yaml") or config_path.endswith(".yml"):
                    if HAS_YAML:
                        import yaml

                        data = yaml.safe_load(f)
                    else:
                        Theme.warning(
                            "PyYAML not installed. Install with: pip install pyyaml"
                        )
                        Theme.warning("Falling back to default host configuration")
                        data = DEFAULT_CONFIG
                else:
                    data = json.load(f)

            if "hosts" in data:
                config.hosts = data["hosts"]
            if "transfer" in data:
                transfer = data["transfer"]
                config.compression_threshold = transfer.get("compression_threshold", 50)
                config.zstd_level = transfer.get("zstd_level", 2)
                config.rsync_compression = transfer.get("rsync_compression", "auto")
                config.rsync_options = transfer.get(
                    "rsync_options", ["-a", "--delete", "--partial"]
                )
            if "ssh" in data:
                ssh = data["ssh"]
                config.ssh_control_master = ssh.get("control_master", True)
                config.ssh_control_path = ssh.get(
                    "control_path", "~/.ssh/sft-control-%h-%p-%r"
                )
                config.ssh_control_persist = ssh.get("control_persist", 600)
                config.ssh_connect_timeout = ssh.get("connect_timeout", 30)
            if "logging" in data:
                logging_cfg = data["logging"]
                config.verbose = logging_cfg.get("verbose", False)
                config.log_file = logging_cfg.get("log_file")
        except Exception as e:
            print(
                f"Warning: Failed to load config: {e}, using defaults",
                file=sys.stderr,
            )
            config.hosts = DEFAULT_CONFIG["hosts"]
    else:
        config.hosts = DEFAULT_CONFIG["hosts"]

    return config


_config: Optional[SftConfig] = None


def get_config() -> SftConfig:
    """Get global config instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


@dataclass
class HostInfo:
    name: str
    hostname: str
    port: int
    user: str
    aliases: List[str]
    extra_options: Dict[str, str]
    scheduler: Optional[str] = None

    def ssh_target(self) -> str:
        return f"{self.user}@{self.hostname}"


@dataclass
class ParsedTarget:
    is_remote: bool
    path: str
    host: Optional[HostInfo]
    user_override: Optional[str]


def load_hosts(
    config: SftConfig,
) -> Tuple[Dict[str, HostInfo], Dict[str, str]]:
    hosts: Dict[str, HostInfo] = {}
    alias_map: Dict[str, str] = {}
    for name, entry in config.hosts.items():
        hostname = entry.get("hostname")
        user = entry.get("user")
        if not hostname or not user:
            raise ValueError(f"Host {name} missing required 'hostname' or 'user' field")
        host = HostInfo(
            name=name,
            hostname=hostname,
            port=int(entry.get("port", 22)),
            user=user,
            aliases=entry.get("aliases", []),
            extra_options=entry.get("extra_options", {}),
            scheduler=entry.get("scheduler"),
        )
        hosts[name] = host
        alias_map[name] = name
        for alias in host.aliases:
            alias_map[alias] = name
    return hosts, alias_map


def normalize_local_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def cleanup_path(path: str, dry_run: bool) -> None:
    """Safely remove a path if it exists, handling TOCTOU race conditions."""
    if dry_run:
        return
    p = Path(path)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists():
        p.unlink(missing_ok=True)


def parse_target(
    spec: str,
    hosts: Dict[str, HostInfo],
    alias_map: Dict[str, str],
) -> ParsedTarget:
    if ":" not in spec:
        return ParsedTarget(
            is_remote=False,
            path=normalize_local_path(spec),
            host=None,
            user_override=None,
        )
    host_part, remote_path = spec.split(":", 1)
    if not remote_path:
        raise ValueError("Remote target must include a path after :")
    user_override = None
    if "@" in host_part:
        user_override, host_part = host_part.split("@", 1)
    host_key = alias_map.get(host_part, host_part)
    host_info = hosts.get(host_key)
    if not host_info:
        raise ValueError(f"Unknown host: {host_part}")
    return ParsedTarget(
        is_remote=True,
        path=remote_path,
        host=host_info,
        user_override=user_override,
    )


def determine_mode(src: ParsedTarget, dst: ParsedTarget) -> str:
    if src.is_remote and dst.is_remote:
        return "remote->remote"
    if src.is_remote:
        return "remote->local"
    if dst.is_remote:
        return "local->remote"
    return "local->local"
