# sft — Smart File Transfer

SSH-based remote development CLI. Transfer files, mount directories, run commands, and manage background jobs on remote hosts.

## Features

- **Smart file transfer**: Automatically selects the best mode (rsync, tar+zstd, git bundle) based on file count and type
- **Remote mounts**: SSHFS-based directory mounting with lifecycle management
- **Remote execution**: Run commands on remote hosts with environment syncing
- **Background jobs**: Launch, monitor, and fetch results from nohup background jobs
- **PBS integration**: Submit and manage HPC cluster jobs
- **Plugin system**: Extend with `sft-nix` (Nix environment sync) and `sft-marimo` (notebook orchestration)

## Installation

```bash
pip install sft

# Optional: YAML config support
pip install sft[yaml]

# Development
pip install -e ".[dev]"
```

## Configuration

Create `~/.config/sft/config.yaml`:

```yaml
hosts:
  my-server:
    hostname: "192.168.1.100"
    port: 22
    user: "myuser"
    aliases: ["srv"]
```

See `config.yaml.example` for full options.

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

## Plugins

- **sft-nix**: Nix environment detection, .envrc/flake syncing, `nix develop` wrapping
- **sft-marimo**: Remote marimo notebook + OpenCode ACP agent orchestration

## License

MIT
