"""
One telegram-apply run at a time, and nothing else reading Telegram while it runs.

`telegram-apply` reads the user's Yes/No taps with Telegram's getUpdates. Telegram lets only one
reader do that at once, so the phone listener (which also reads getUpdates, to hear "/apply") must
stand aside while a run is going, and two runs must never overlap. A small lock file holding the
process id does both. A lock whose process has died is ignored, so a crash cannot block the phone
for good.
"""

from __future__ import annotations

import datetime
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


class ApplyLockHeld(Exception):
    """Another telegram-apply run is in progress."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) would TERMINATE the process on Windows, so ask the OS politely instead.
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # cannot tell: assume it is alive, the safe answer for a lock
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _holder_pid(path: Path) -> int | None:
    try:
        first = path.read_text(encoding="utf-8").split()[0]
        return int(first)
    except (OSError, IndexError, ValueError):
        return None


def apply_lock_held(path: Path) -> bool:
    """True while a live process holds the lock."""
    path = Path(path)
    if not path.exists():
        return False
    pid = _holder_pid(path)
    return pid is not None and _pid_alive(pid)


@contextmanager
def hold_apply_lock(path: Path) -> Iterator[None]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if apply_lock_held(path):
            raise ApplyLockHeld("another telegram-apply run is already in progress on this PC")
        logger.info("removing a stale apply lock")
        try:
            path.unlink()
        except OSError:
            pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ApplyLockHeld("another telegram-apply run is already in progress on this PC") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {datetime.datetime.now(datetime.UTC).isoformat()}\n")
        yield
    finally:
        try:
            path.unlink()
        except OSError:
            pass
