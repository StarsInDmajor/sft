# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2025-04-XX

### Added
- Smart file transfer with automatic mode selection (rsync, tar+zstd, git bundle)
- SSH-based remote directory mounting via SSHFS
- Remote command execution with environment syncing
- Background job management (launch, monitor, fetch results)
- PBS cluster job submission and monitoring
- Plugin system via `importlib.metadata` entry points
- `sft-nix` plugin: `.envrc`/`flake.nix` detection and `nix develop` wrapping
- `sft-marimo` plugin: remote marimo notebook + local ACP agent orchestration
- Per-project `.sftrc.toml` configuration
- Transfer manifest with change detection (`--diff`)
- SSH ControlMaster connection multiplexing
