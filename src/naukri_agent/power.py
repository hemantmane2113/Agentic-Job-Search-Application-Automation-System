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
