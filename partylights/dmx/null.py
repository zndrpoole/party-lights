"""A driver that goes nowhere.

Exists so the engine, the looks and the whole web UI can be built and exercised
with no hardware attached. It keeps the last frame so the UI preview can render
exactly what the real fixtures would have received.
"""

from __future__ import annotations

import logging
import threading

from .driver import Driver

log = logging.getLogger(__name__)


class NullDriver(Driver):
    name = "null"

    def __init__(self, *, log_every: int = 0):
        self._lock = threading.Lock()
        self._last = bytearray(512)
        self._frames = 0
        self._log_every = log_every
        self._open = False

    def open(self) -> None:
        self._open = True
        log.info("Null driver open — no hardware will be touched.")

    def send(self, slots) -> None:
        with self._lock:
            n = len(slots)
            self._last[:n] = slots
            self._frames += 1
            frames = self._frames
        if self._log_every and frames % self._log_every == 0:
            log.debug("null driver: %d frames, first 8 slots %s", frames, bytes(self._last[:8]).hex(" "))

    def close(self) -> None:
        self._open = False

    @property
    def frames(self) -> int:
        with self._lock:
            return self._frames

    def last_frame(self) -> bytes:
        with self._lock:
            return bytes(self._last)

    def describe(self) -> str:
        return "null (simulated output, no hardware)"
