"""
Keep Windows awake while a job is running, and let it sleep again afterwards.

A scheduled task can WAKE a sleeping PC (the tasks are set to), but nothing stops Windows putting it back to sleep
partway through a run that lasts hours, because a browser driven by a script does not count as "busy" to Windows.
`keep_awake()` asks Windows, for as long as the `with` block lasts, not to go to sleep. It asks for the SYSTEM to stay
on only, not the display, so the screen can still turn off and the PC can be locked. It is a request, not a lock:
a lid close, a power-button press or a battery-critical shutdown still win, and when the block ends the request is
withdrawn and the normal sleep timer applies again.

Does nothing on systems without the Windows call, and never raises: failing to ask must not stop a run.
"""

from __future__ import annotations

import datetime
import logging
import os
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_state(flags: int) -> bool:
    """Call SetThreadExecutionState. True when Windows accepted it, False when unavailable or refused."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - never let a power request stop a run
        logger.debug("keep_awake: SetThreadExecutionState unavailable (%s)", type(exc).__name__)
        return False


def _parse_hhmm(text: str) -> datetime.time:
    hour, minute = (int(p) for p in text.strip().split(":"))
    return datetime.time(hour, minute)


class AwakeWindow:
    """
    Keeps the PC awake while the local time is inside [start, end), for a long-running process such as the phone
    listener, and lets it sleep outside that window. Call `update(local_time)` regularly (every few seconds is fine; it
    only talks to Windows when the answer changes). It must be called from the same thread each time, because Windows
    ties the request to the calling thread. If the window wraps midnight (start later than end) that is handled.

    This exists because a PC woken by a scheduled task goes straight back to sleep once the task ends, and a sleeping
    PC cannot hear a Telegram reply (found 2026-10-21: the PC fell asleep 1-5 minutes after each morning message).
    """

    def __init__(self, start: str, end: str) -> None:
        self.start, self.end = _parse_hhmm(start), _parse_hhmm(end)
        self.held = False
        self._unavailable = False

    def inside(self, local_time: datetime.time) -> bool:
        t = local_time.replace(second=0, microsecond=0)
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def update(self, local_time: datetime.time) -> bool:
        want = self.inside(local_time)
        if want and not self.held and not self._unavailable:
            if _set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                self.held = True
                logger.info("keep_awake: the PC will stay awake until %s so Telegram replies can be heard", self.end.strftime("%H:%M"))
            else:
                self._unavailable = True
                logger.info("keep_awake: not available here; the PC may sleep and miss Telegram replies")
        elif not want and self.held:
            self.release()
        return self.held

    def release(self) -> None:
        if self.held:
            _set_state(ES_CONTINUOUS)
            self.held = False
            logger.info("keep_awake: released; the PC may sleep until the next scheduled task")


@contextmanager
def keep_awake(reason: str = "naukri-agent job") -> Iterator[bool]:
    """Yields True when the PC is now being kept awake, False when that was not possible."""
    held = _set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    if held:
        logger.info("keep_awake: the PC will not sleep while this runs (%s); the screen may still turn off", reason)
    else:
        logger.info("keep_awake: not available here; the PC may sleep during this run (%s)", reason)
    try:
        yield held
    finally:
        if held:
            _set_state(ES_CONTINUOUS)  # withdraw the request: the normal sleep timer applies again
            logger.info("keep_awake: released")
