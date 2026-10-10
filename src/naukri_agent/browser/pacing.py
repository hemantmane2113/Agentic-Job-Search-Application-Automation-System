"""
Pacing: short random pauses between page loads and before a click, so the browser behaves at a human
rhythm instead of loading dozens of pages at machine-regular intervals. It also lightens the load on the
site. This is politeness and rate limiting, not disguise: nothing here hides that a program is driving.
"""

from __future__ import annotations

import random
import time
from typing import Callable


def pause(
    min_seconds: float,
    max_seconds: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    uniform: Callable[[float, float], float] = random.uniform,
) -> float:
    """Sleep a random time between the two bounds. A maximum of 0 (or less) means no pause. Returns the delay."""
    if max_seconds <= 0:
        return 0.0
    delay = uniform(max(0.0, min(min_seconds, max_seconds)), max_seconds)
    sleep(delay)
    return delay
