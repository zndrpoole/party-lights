"""Keep trying the hardware instead of giving up on it.

The ENTTEC hangs off a USB cable that gets knocked out and re-plugged. Without
this, a missing adapter at launch meant the service fell back to the null driver
and stayed there until someone restarted it, and an adapter pulled mid-party
left the writer failing every frame into a dead file handle. Twice the rig sat
dark for days with the UI looking healthy.

This wraps a real driver. While the link is down, frames are dropped (there is
nowhere to send them) and the open is retried every few seconds from the writer
thread. Re-plugging the cable is all it takes to bring the lights back.
"""

from __future__ import annotations

import logging
import threading
import time

from .driver import Driver, DriverError

log = logging.getLogger(__name__)


class ReconnectingDriver(Driver):
    name = "reconnecting"

    def __init__(self, inner: Driver, *, retry_s: float = 2.0, clock=time.monotonic):
        self.inner = inner
        self.retry_s = retry_s
        self._clock = clock
        self._lock = threading.Lock()
        self._connected = False
        self._next_try = 0.0
        self._down_since: float | None = None
        self._down_wall: float | None = None
        self._last_error = ""
        self._reconnects = 0

    def open(self) -> None:
        """Try once. A missing adapter is not fatal; send() keeps retrying."""
        self._try_open(first=True)

    def _try_open(self, *, first: bool = False) -> None:
        try:
            self.inner.open()
        except DriverError as e:
            self._mark_down(str(e), quiet=not first)
            return
        with self._lock:
            was_down = self._down_since is not None and not first
            if was_down:
                self._reconnects += 1
                log.warning("DMX output back on %s after %.0f s down",
                            self.inner.describe(), self._clock() - self._down_since)
            self._connected = True
            self._down_since = None
            self._down_wall = None
            self._last_error = ""

    def _mark_down(self, error: str, *, quiet: bool = False) -> None:
        with self._lock:
            if self._connected or self._down_since is None:
                log.warning("DMX output down (%s); retrying every %.0f s", error, self.retry_s)
            elif not quiet:
                log.info("DMX output still down: %s", error)
            if self._down_since is None:
                self._down_since = self._clock()
                self._down_wall = time.time()
            self._connected = False
            self._last_error = error
            self._next_try = self._clock() + self.retry_s

    def send(self, slots) -> None:
        if not self._connected:
            if self._clock() < self._next_try:
                return
            self._try_open()
            if not self._connected:
                return
        try:
            self.inner.send(slots)
        except DriverError as e:
            try:
                self.inner.close()
            except Exception:
                pass
            self._mark_down(str(e))

    def close(self) -> None:
        with self._lock:
            self._connected = False
        self.inner.close()

    @property
    def connected(self) -> bool:
        return self._connected

    def status(self) -> dict:
        """Link state for /api/state, which the party watchdog reads."""
        with self._lock:
            return {
                "connected": self._connected,
                "down_since": self._down_wall,
                "error": self._last_error or None,
                "reconnects": self._reconnects,
            }

    def describe(self) -> str:
        if self._connected:
            return self.inner.describe()
        return f"{self.inner.describe()} [DISCONNECTED, retrying]"
