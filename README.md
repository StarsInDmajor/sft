# sft — Smart File Transfer

**English** | [中文](README.zh-CN.md)

SSH-based remote development CLI. Transfer files, mount directories, run commands, and manage background jobs on remote hosts.

## Features

- **Smart file transfer**: Automatically selects the best mode (rsync, tar+zstd, git bundle) based on file count and type
- **Remote mounts**: SSHFS-based directory mounting with lifecycle management
- **Remote execution**: Run commands on remote hosts with environment syncing
- **Background jobs**: Launch, monitor, and fetch results from nohup background jobs
- **PBS integration**: Submit and manage HPC cluster jobs
- **Plugin system**: Extend with `sft-nix` (Nix environment sync) and `sft-marimo` (notebook orchestration)

## Installation

### From Git (recommended)

```bash
pip install git+https://github.com/StarsInDmajor/sft.git

# With plugins
pip install git+https://github.com/StarsInDmajor/sft-nix.git
pip install git+https://github.com/StarsInDmajor/sft-marimo.git
```

<!-- PyPI publishing coming soon -->
<!-- ```bash -->
<!-- pip install sft -->
<!-- pip install sft sft-nix sft-marimo -->
<!-- ``` -->

### Via Nix (recommended for NixOS users)

sft is consumed as a flake input. See the NixOS config repo for the full build setup:

```nix
inputs = {
  sft.url = "github:StarsInDmajor/sft";
  sft-nix.url = "github:StarsInDmajor/sft-nix";
  sft-marimo.url = "github:StarsInDmajor/sft-marimo";
};
```

The NixOS config builds all three as `packages.sft` using `buildPythonApplication` + `makeWrapper` for plugin injection.

## Configuration

Create `~/.config/sft/config.yaml`:

```yaml
hosts:
  my-server:
    hostname: "192.168.1.100"
    port: 22
    user: "myuser"
    aliases: ["srv"]

  my-cluster:
    hostname: "10.0.0.50"
    port: 22
    user: "researcher"
    scheduler: "pbs"

transfer:
  compression_threshold: 50
  zstd_level: 2
  rsync_compression: "auto"
  rsync_options: ["-a", "--delete", "--partial"]

ssh:
  control_master: true
  control_path: "~/.ssh/sft-control-%h-%p-%r"
  control_persist: 600
  connect_timeout: 30
```

### Config Schema

| Section  | Key                     | Type     | Default                         | Description                                 |
| -------- | ----------------------- | -------- | ------------------------------- | ------------------------------------------- |
| `hosts`  | (per-host)              |          |                                 | See below                                   |
|          | `hostname`              | string   | required                        | IP or domain                                |
|          | `port`                  | int      | 22                              | SSH port                                    |
|          | `user`                  | string   | required                        | SSH username                                |
|          | `aliases`               | string[] | []                              | Short names for `sft alias:~/path`          |
|          | `extra_options`         | map      | {}                              | Extra SSH options (e.g. `IdentityFile`)     |
|          | `scheduler`             | string?  | null                            | Set to `"pbs"` for PBS cluster hosts        |
| `transfer` | `compression_threshold` | int      | 50                              | Files above this count trigger zstd         |
|          | `zstd_level`            | int      | 2                               | zstd compression level (1-22)               |
|          | `rsync_compression`     | string   | `"auto"`                        | `auto` / `always` / `never`                 |
|          | `rsync_options`         | string[] | `["-a","--delete","--partial"]` | Extra rsync flags                           |
| `ssh`    | `control_master`        | bool     | true                            | Enable SSH connection multiplexing          |
|          | `control_path`          | string   | `"~/.ssh/sft-control-%h-%p-%r"` | ControlMaster socket path                   |
|          | `control_persist`       | int      | 600                             | Seconds to keep idle connections alive       |
|          | `connect_timeout`       | int      | 30                              | SSH connection timeout (seconds)            |
| `logging` | `verbose`               | bool     | false                           | Default verbosity                           |
|          | `log_file`              | string?  | null                            | Log file path (null = stderr only)          |

## Usage

```bash
# Transfer files (auto-selects best mode)
sft ./project my-server:~/project

# Mount remote directory
sft mount my-server:/path/to/dir

# Run command on remote host
sft run my-server "python train.py"

# Sync local files and run remotely
sft sync-run ./project my-server:~/project -- python train.py

# Submit PBS job
sft submit job.sh my-cluster

# Background job management
sft run my-server --background "python train.py"
sft jobs
sft logs <job_id>
sft fetch <job_id>
```

## Plugin Architecture

sft uses `importlib.metadata` entry points for plugin discovery. Plugins register under the `sft.plugins` group.

### Available Plugins

| Plugin       | Entry point              | Provides                                                       |
| ------------ | ------------------------ | -------------------------------------------------------------- |
| **sft-nix**  | `sft_nix.hooks`          | `.envrc`/`flake.nix` detection, nix develop wrapping, post-transfer flake sync |
| **sft-marimo** | `sft_marimo.hooks`     | `sft marimo start/stop/status/list` subcommands                |

### How Plugins Work

1. At startup, `sft.cli:main()` calls `plugins.discover_plugins()`
2. `discover_plugins()` loads all entry points in the `sft.plugins` group
3. Each plugin's entry point module is imported, which:
   - **sft-nix**: calls `apply_overrides()` to monkey-patch `sft.env` stub functions
   - **sft-marimo**: calls `register_subcommand("marimo", ...)` to add CLI commands

### Registration API

Plugins use `sft.plugins` functions to extend sft:

| Function                        | Purpose                                         |
| ------------------------------- | ----------------------------------------------- |
| `register_subcommand(name, fn)` | Add a top-level CLI command                     |
| `register_subcommand_parser(name, fn)` | Register argparse setup for a subcommand |
| `register_env_resolver(fn)`     | Register environment resolver hook              |
| `register_post_transfer_hook(fn)` | Register hook called after file transfer       |

### Writing a New Plugin

1. Create a Python package with `pyproject.toml`:

```toml
[project]
name = "sft-myplugin"
dependencies = ["sft"]

[project.entry-points."sft.plugins"]
myplugin = "sft_myplugin.hooks"
```

2. Create `src/sft_myplugin/hooks.py`:

```python
from sft.plugins import register_subcommand, register_subcommand_parser

def register():
    register_subcommand("mycommand", handle_mycommand)
    register_subcommand_parser("mycommand", add_my_parser)

def handle_mycommand(args, ctx):
    ...

def add_my_parser(subparsers, global_parent):
    parser = subparsers.add_parser("mycommand", parents=[global_parent])
    parser.add_argument("target")
```

3. Install alongside sft — the entry point will be discovered automatically.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full development guide.

### Quick Start

```bash
# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run CLI
sft --list-hosts

# Run tests
pytest tests/ -v
```

### NixOS Integration

sft can be consumed as a Nix flake input:

```nix
inputs = {
  sft.url = "github:StarsInDmajor/sft";
  sft-nix.url = "github:StarsInDmajor/sft-nix";
  sft-marimo.url = "github:StarsInDmajor/sft-marimo";
};
```

For local development, use path-based inputs and your normal NixOS rebuild workflow.

## Project Structure

```
sft/
├── src/sft/
│   ├── __init__.py
│   ├── __main__.py          # python -m sft support
│   ├── cli.py               # Argument parsing, command dispatch
│   ├── config.py            # Config loading, host definitions, target parsing
│   ├── config_project.py    # Per-project .sft/config override
│   ├── context.py           # ExecutionContext (SSH, dry-run, logging)
│   ├── env.py               # Environment detection stubs (overridden by sft-nix)
│   ├── manifest.py          # Transfer manifest (checksums, size, mtime)
│   ├── background.py        # Background job state management
│   ├── pbs.py               # PBS job submission helpers
│   ├── plugins.py           # Plugin registry + discover_plugins()
│   ├── shell.py             # Shell quoting utilities
│   ├── state.py             # State directory management
│   ├── transfer.py          # File transfer logic (rsync, tar+zstd)
│   ├── ui.py                # Terminal output (Theme, colors, steps)
│   └── commands/
│       ├── __init__.py
│       ├── mount.py          # sft mount / umount / mounts
│       ├── run.py            # sft run
│       ├── sync_run.py       # sft sync-run
│       ├── submit.py         # sft submit (PBS)
│       ├── jobs.py           # sft jobs / logs / stop / fetch / status
│       └── fetch.py          # sft fetch
├── tests/
│   ├── test_pure.py          # Unit tests (config, parsing, env stubs)
│   ├── test_transfer.py      # Transfer logic tests
│   └── test_integration.py   # CLI integration tests
├── config.yaml.example       # Example configuration
├── pyproject.toml
└── README.md
```

## License

MIT
