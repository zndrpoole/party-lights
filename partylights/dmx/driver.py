"""Output driver interface.

Every driver turns a DMX frame — the start code plus N slots — into whatever the
hardware needs. The ENTTEC Open DMX USB has no microcontroller, so for the real
drivers that means generating the BREAK and Mark-After-Break ourselves on every
frame. Nothing upstream of here knows or cares which driver is in use.
"""

from __future__ import annotations

import abc
import logging

log = logging.getLogger(__name__)

# DMX512 timing minimums, from the standard. We are allowed to exceed them and
# fixtures tolerate a generous BREAK, which matters because a userspace sleep on
# macOS cannot reliably resolve 88us.
BREAK_MIN_S = 0.000088
MAB_MIN_S = 0.000012

DMX_BAUD = 250000


class DriverError(RuntimeError):
    """Raised when a driver cannot be opened or has failed irrecoverably."""


class Driver(abc.ABC):
    """A DMX output. Open it, send frames at a steady rate, close it."""

    #: Short name used in config and logs.
    name = "abstract"

    @abc.abstractmethod
    def open(self) -> None:
        """Acquire the hardware. Raises DriverError with a human-readable cause."""

    @abc.abstractmethod
    def send(self, slots: memoryview | bytes) -> None:
        """Emit one frame carrying `slots` (slot 1 first; start code is ours to add).

        Must not raise for transient hiccups — log and carry on, so one bad frame
        never kills the party. Raise DriverError only when the link is truly gone.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Release the hardware. Must be safe to call when never opened."""

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def describe(self) -> str:
        """One line for the UI and startup log."""
        return self.name
