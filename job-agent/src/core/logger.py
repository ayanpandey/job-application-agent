"""
Structured logger using Rich for beautiful terminal output.
"""

import logging
import os
import sys
from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
    "highlight": "bold magenta",
})

# True when this process is a run spawned by the desktop app, whose stdout is a
# pipe the window reads rather than a terminal.
_PIPED = os.getenv("JOB_AGENT_PLAIN_LOGS") == "1"

# Rich decides to drive the Win32 console API by asking whether the stream's
# *file descriptor* is 1 or 2 -- not whether it is a terminal. A child spawned
# with stdout=PIPE still has fd 1, so on Windows Rich took its legacy console
# path and wrote through `LegacyWindowsTerm`, encoding to the console codepage.
# The first line the run prints contains U+2550 box drawing:
#
#   UnicodeEncodeError: 'charmap' codec can't encode characters in position 0-1
#
# ...which killed the run instantly, on Windows only, and only when launched
# from the app rather than a terminal. `legacy_windows=False` keeps Rich on the
# ordinary "write text to a file" path, which is all a pipe can accept anyway.
#
# `width` matters too: for a non-terminal Rich assumes 80 columns and hard-wraps,
# which would split log lines mid-message in the window's log pane.
console = Console(
    theme=custom_theme,
    legacy_windows=False if _PIPED else None,
    no_color=_PIPED,
    width=200 if _PIPED else None,
)


def _attach_file_handler(logger: logging.Logger) -> None:
    """
    Also write every record to a file under the user's data directory.

    Until this existed there was no record of a run anywhere on disk. The
    desktop app pipes a run's stdout into its log pane and that pane is all
    there was: close the window, or ask afterwards what happened, and the
    session was simply gone. Diagnosing a live problem meant reading the
    database and inferring backwards.

    Best-effort and silent on failure. Logging that can take down the
    application it is meant to be reporting on is worse than no logging, and an
    unwritable data directory is a real state on a locked-down machine.
    """
    try:
        from logging.handlers import RotatingFileHandler

        from src.core import paths

        log_dir = paths.logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)

        # Rotating rather than unbounded: a run logs steadily for an hour and
        # nothing ever prunes this directory.
        handler = RotatingFileHandler(
            log_dir / "agent.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s|%(levelname)s|%(name)s|%(message)s", "%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(handler)
    except Exception:
        pass


def setup_logger(name: str = "linkedin_agent", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(level)
        handler: logging.Handler
        if os.getenv("JOB_AGENT_PLAIN_LOGS") == "1":
            # The desktop app runs a run as a child process and reads its stdout
            # line by line. Rich would wrap, colour and box that output, so the
            # parent would have to unpick ANSI escapes and reflowed lines to get
            # a message back. Plain records stay one line each.
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(asctime)s|%(levelname)s|%(message)s", "%H:%M:%S"))
        else:
            handler = RichHandler(
                console=console,
                show_time=True,
                show_path=False,
                rich_tracebacks=True,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        # Added after the console handler so a failure here cannot leave the
        # logger with no handler at all.
        _attach_file_handler(logger)
    return logger


logger = setup_logger()
