"""Structured logging system with file rotation and in-memory ring buffer for TUI inspection."""

import logging
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Deque, List, Optional

_MEMORY_BUFFER: Deque[logging.LogRecord] = deque(maxlen=1000)
_INITIALIZED = False


class MemoryLogHandler(logging.Handler):
    """Handler that appends log records to an in-memory ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        _MEMORY_BUFFER.append(record)


def setup_logging(log_file: Optional[Path] = None, level: int = logging.INFO) -> None:
    """Initialize root logger with file rotation and memory buffer."""
    global _INITIALIZED
    if _INITIALIZED:
        return

    root_logger = logging.getLogger("aiheaddev")
    root_logger.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. In-memory buffer handler
    mem_handler = MemoryLogHandler()
    mem_handler.setFormatter(formatter)
    root_logger.addHandler(mem_handler)

    # 2. File handler
    if log_file is None:
        log_dir = Path.home() / ".config" / "herdr" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "cockpit.log"
    else:
        log_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except Exception:
        pass

    _INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """Get a namespaced logger under the aiheaddev root."""
    if not _INITIALIZED:
        setup_logging()
    return logging.getLogger(f"aiheaddev.{name}")


def get_recent_logs(limit: int = 200, min_level: int = logging.DEBUG) -> List[str]:
    """Retrieve formatted log lines from the in-memory buffer."""
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    records = list(_MEMORY_BUFFER)
    filtered = [
        formatter.format(r)
        + (f"\n{r.exc_text}" if getattr(r, "exc_text", None) else "")
        for r in records
        if r.levelno >= min_level
    ]
    return filtered[-limit:]
