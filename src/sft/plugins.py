"""Plugin registration infrastructure for sft extensions.

Plugins register themselves via ``importlib.metadata.entry_points`` under
the ``sft.plugins`` group. Each plugin module may call the registration
functions exposed here to extend sft's behaviour.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, List, Optional

# --- Registries ---

_subcommands: Dict[str, Callable] = {}
_subcommand_parsers: Dict[str, Callable] = {}
_env_resolvers: List[Callable] = []
_post_transfer_hooks: List[Callable] = []


# --- Registration API ---


def register_subcommand(name: str, handler: Callable) -> None:
    """Register a top-level CLI subcommand (e.g. ``sft <name>``)."""
    _subcommands[name] = handler


def register_subcommand_parser(name: str, parser_fn: Callable) -> None:
    """Register a function that adds subcommand arguments to the argparse parser.

    ``parser_fn`` receives the argparse subparser and should add a subparser
    for its command.
    """
    _subcommand_parsers[name] = parser_fn


def register_env_resolver(resolver: Callable) -> None:
    """Register an environment resolver hook.

    The resolver is called with ``(args, ctx)`` and should return an
    ``EnvSource`` (or ``None`` if it cannot resolve the environment).
    """
    _env_resolvers.append(resolver)


def register_post_transfer_hook(hook: Callable) -> None:
    """Register a post-transfer hook.

    The hook is called with ``(src, dst, args, ctx)`` after a successful
    transfer.
    """
    _post_transfer_hooks.append(hook)


# --- Accessors ---


def get_subcommands() -> Dict[str, Callable]:
    return dict(_subcommands)


def get_subcommand_parsers() -> Dict[str, Callable]:
    return dict(_subcommand_parsers)


def get_env_resolvers() -> List[Callable]:
    return list(_env_resolvers)


def get_post_transfer_hooks() -> List[Callable]:
    return list(_post_transfer_hooks)


# --- Discovery ---


def discover_plugins() -> None:
    """Discover and load all installed sft plugins via entry points."""
    try:
        eps = importlib.metadata.entry_points(group="sft.plugins")
    except AttributeError:
        # Python < 3.12 fallback
        eps = importlib.metadata.entry_points().get("sft.plugins", [])

    for ep in eps:
        try:
            ep.load()
        except Exception as exc:
            import sys

            print(
                f"Warning: failed to load sft plugin '{ep.name}': {exc}",
                file=sys.stderr,
            )
