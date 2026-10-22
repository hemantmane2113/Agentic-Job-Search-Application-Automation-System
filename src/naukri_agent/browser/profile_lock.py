"""
One browser at a time on the shared Naukri profile.

The daily run, the profile refresh, the research agent and `telegram-apply` all drive the SAME Chrome profile
(`data/browser_profile`), and Chrome lets only one process use a profile at once. When Windows starts several of them
in the same second (a PC switched on late runs every missed task together; seen 2026-10-21, when the daily run and the
profile refresh failed while an apply run held the profile) the later ones fail with a bare "Error".

`acquire()` makes them take turns instead: it holds an operating-system file lock for as long as a browser is open on
the profile, and a second process waits (up to a limit) rather than collide. The lock belongs to the process, so if the
process crashes or is killed Windows releases it by itself; there is no stale-lock cleanup to get wrong. It is
re-entrant inside one process. This module opens no browser and has no network or database access.
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# The byte that is locked sits far past the end of the file, so the text describing the holder (written at the start)
# stays readable by the processes that are waiting.
_LOCK_OFFSET = 1 << 20
_POLL_SECONDS = 3.0


class BrowserBusy(Exception):
    """The browser profile stayed in use by another process for the whole wait."""


_held: dict[str, list] = {}  # resolved path -> [fd, depth]


def _lock_fd(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


def _unlock_fd(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except OSError:
        pass


def _holder_text() -> str:
    command = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "python"
    return f"{os.getpid()} {datetime.datetime.now().astimezone().strftime('%H:%M')} {command}"


def describe_holder(path: Path) -> str:
    """'run-daily (running since 10:37)' from the text the holder wrote; 'another naukri-agent command' if unreadable."""
    try:
        parts = Path(path).read_text(encoding="utf-8").split()
        return f"{parts[2]} (running since {parts[1]})"
    except (OSError, IndexError):
        return "another naukri-agent command"


def holder_if_busy(path: Path) -> str | None:
    """None when no OTHER process is using the browser profile right now, else a short description of who is.
    Takes the lock only for an instant to find out."""
    path = Path(path)
    key = str(path.resolve())
    if key in _held or not path.exists():
        return None
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        if _lock_fd(fd):
            _unlock_fd(fd)
            return None
        return describe_holder(path)
    finally:
        os.close(fd)


def acquire(
    path: Path,
    wait_seconds: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Take the lock, waiting up to `wait_seconds` for another process to finish. Raises BrowserBusy if it never frees."""
    path = Path(path)
    key = str(path.resolve())
    if key in _held:  # this process already holds it (a second browser in the same command)
        _held[key][1] += 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    deadline = clock() + wait_seconds
    announced = False
    while not _lock_fd(fd):
        holder = describe_holder(path)
        if not announced:
            logger.info("the Naukri browser is in use by %s; waiting up to %d min for it", holder, int(wait_seconds // 60))
            announced = True
        if clock() >= deadline:
            os.close(fd)
            raise BrowserBusy(f"the Naukri browser is in use by {holder}; gave up after {int(wait_seconds // 60)} min")
        sleep(_POLL_SECONDS)
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, _holder_text().encode("utf-8"))
    except OSError:
        pass  # the text is only a courtesy to whoever waits
    _held[key] = [fd, 1]
    if announced:
        logger.info("the Naukri browser is free now; continuing")


def release(path: Path) -> None:
    key = str(Path(path).resolve())
    entry = _held.get(key)
    if entry is None:
        return
    entry[1] -= 1
    if entry[1] > 0:
        return
    del _held[key]
    _unlock_fd(entry[0])
    try:
        os.close(entry[0])
    except OSError:
        pass
