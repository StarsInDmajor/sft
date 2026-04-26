# Contributing to sft

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/StarsInDmajor/sft.git
cd sft

# Create a virtual environment and install in editable mode
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# For plugin development, also install the plugins:
pip install -e ../sft-nix[dev]
pip install -e ../sft-marimo[dev]
```

## Running Tests

```bash
# All tests
pytest tests/ -v

# Single test file
pytest tests/test_pure.py -v

# Single test
pytest tests/test_pure.py::TestConfig::test_load_config -v
```

## Plugin Development

sft uses `importlib.metadata` entry points for plugin discovery. See the
[Plugin Architecture](README.md#plugin-architecture) section of the README
for details on how to write a new plugin.

### Plugin API

| Function                        | Purpose                                   |
| ------------------------------- | ----------------------------------------- |
| `register_subcommand(name, fn)` | Add a top-level CLI command                |
| `register_subcommand_parser(name, fn)` | Register argparse setup for a subcommand |
| `register_env_resolver(fn)`     | Register environment resolver hook        |
| `register_post_transfer_hook(fn)` | Register hook called after file transfer  |

## Pull Request Process

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes and add tests
4. Ensure all tests pass (`pytest`)
5. Commit with a clear message
6. Open a pull request against `main`

## Code Style

- Python 3.10+ with `from __future__ import annotations`
- Use type hints for public APIs
- Follow the existing code patterns (dataclasses for data, `ExecutionContext` for I/O)
