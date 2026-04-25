"""CLI theme, spinner, and output formatting."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional


class Theme:
    """CLI Theme using ANSI escape codes and emojis."""
    CLR = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    BLUE = "\033[1;34m"
    GREEN = "\033[1;32m"
    YELLOW = "\033[1;33m"
    RED = "\033[1;31m"
    CYAN = "\033[1;36m"
    GREY = "\033[90m"

    SSH = "🔑"
    GIT = "📦"
    XFER = "🚄"
    ZIP = "🗜️"
    SCAN = "🔍"
    OK = "✨"
    FAIL = "💥"
    WARN = "⚠️"
    INFO = "ℹ️"

    @classmethod
    def format_step(cls, icon: str, action: str, target: str) -> str:
        return f"{icon} {cls.BOLD}{action}{cls.CLR} {cls.CYAN}{target}{cls.CLR}"

    @classmethod
    def step(cls, icon: str, action: str, target: str) -> None:
        print(f"{cls.format_step(icon, action, target)} ...")

    @classmethod
    def success(cls, message: str, duration: Optional[float] = None) -> None:
        time_str = f" {cls.DIM}({duration:.2f}s){cls.CLR}" if duration is not None else ""
        print(f"{cls.OK} {message}{time_str} {cls.GREEN}[OK]{cls.CLR}")

    @classmethod
    def error(cls, message: str) -> None:
        print(f"{cls.FAIL} {cls.RED}Error:{cls.CLR} {message}")

    @classmethod
    def warning(cls, message: str) -> None:
        print(f"{cls.WARN} {cls.YELLOW}Warning:{cls.CLR} {message}")

    @classmethod
    def info(cls, key: str, value: Any) -> None:
        print(f"  {cls.DIM}{key:<15}{cls.CLR} {cls.BOLD}{str(value)}{cls.CLR}")

    @classmethod
    def command(cls, cmd: str) -> None:
        print(f"  {cls.GREY}$ {cmd}{cls.CLR}")


class Spinner:
    """Simple thread-based spinner for CLI."""

    def __init__(self, message: str, icon: str = Theme.SCAN):
        self.message = message
        self.icon = icon
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.start_time = 0.0

    def __enter__(self):
        self.start_time = time.time()
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_event.set()
        self.thread.join()
        duration = time.time() - self.start_time
        print(f"\r{' ' * self._get_terminal_width()}\r", end="")

        if exc_type:
            return False

        Theme.success(self.message, duration)
        return False

    def _get_terminal_width(self) -> int:
        try:
            return os.get_terminal_size().columns
        except OSError:
            return 80

    def _spin(self):
        chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        i = 0
        width = self._get_terminal_width()
        base_msg = Theme.format_step(
            self.icon,
            self.message.split(" ")[0],
            " ".join(self.message.split(" ")[1:]),
        )
        while not self.stop_event.is_set():
            max_len = max(0, width - 4)
            display_msg = base_msg[:max_len] if len(base_msg) > max_len else base_msg
            print(
                f"\r{display_msg} {Theme.BLUE}{chars[i % len(chars)]}{Theme.CLR}",
                end="",
                flush=True,
            )
            time.sleep(0.1)
            i += 1
