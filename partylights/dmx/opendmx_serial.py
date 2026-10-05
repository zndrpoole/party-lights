"""Open DMX USB over the macOS FTDI virtual serial port.

This is the path that needs no driver surgery: Apple's AppleUSBFTDI already
claimed the FT232R and exposed it as /dev/cu.usbserial-*, so we just talk
termios at it. The cost is timing precision — BREAK is an ioctl that crosses USB,
so its real duration is set by USB latency rather than by us. In practice that
lands comfortably above the 88us minimum, which is all DMX512 asks.

Fixtures tolerate a long BREAK, so erring long is safe. Erring *short* is what
causes the classic symptom of fixtures ignoring the first slots of a frame.
"""

from __future__ import annotations

import logging
import time

from .driver import DMX_BAUD, Driver, DriverError

log = logging.getLogger(__name__)

try:
    import serial
except ImportError:  # pragma: no cover - surfaced at open() with a useful message
    serial = None


def _spin(seconds: float) -> None:
    """Busy-wait. Used only for sub-millisecond waits, where sleep() cannot help.

    At 40 Hz a 150us spin is a 0.6% duty cycle, which is cheaper than the jitter
    a time.sleep() of this length would introduce.
    """
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


class OpenDmxSerialDriver(Driver):
    name = "opendmx-serial"

    def __init__(
        self,
        port: str,
        *,
        break_s: float = 0.000176,
        mab_s: float = 0.000020,
        break_mode: str = "ioctl",
    ):
        """
        break_mode:
          "ioctl"  — TIOCSBRK/TIOCCBRK via pyserial's break_condition. Preferred.
          "baud"   — the old trick: drop to 96000 baud, send a zero byte (which
                     appears on the wire as a long low period), then restore.
                     Use this if a particular clone ignores the ioctl.
        """
        if break_mode not in ("ioctl", "baud"):
            raise ValueError(f"unknown break_mode {break_mode!r}")
        self.port = port
        self.break_s = break_s
        self.mab_s = mab_s
        self.break_mode = break_mode
        self._ser = None
        # Preallocated so the send path never allocates: start code + 512 slots.
        self._buf = bytearray(513)

    def open(self) -> None:
        if serial is None:
            raise DriverError("pyserial is not installed — pip install pyserial")
        # RTS and DTR must be OFF. On this interface (an Open DMX clone) they
        # gate the line driver: with them asserted the frames leave the Mac but
        # nothing reaches the cable, and receivers report no signal. pyserial
        # asserts both on open by default, so they are set before open() rather
        # than after, which would leave a window of dead output.
        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = DMX_BAUD
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_TWO
        ser.timeout = 0
        ser.write_timeout = 0.5
        ser.rtscts = False
        ser.dsrdtr = False
        ser.rts = False
        ser.dtr = False
        try:
            ser.open()
            self._ser = ser
        except Exception as e:
            raise DriverError(f"could not open {self.port}: {e}") from e
        log.info("Open DMX USB ready on %s (break_mode=%s)", self.port, self.break_mode)

    def _emit_break(self) -> None:
        ser = self._ser
        if self.break_mode == "ioctl":
            ser.break_condition = True
            _spin(self.break_s)
            ser.break_condition = False
            _spin(self.mab_s)
        else:
            ser.baudrate = 96000
            ser.write(b"\x00")
            ser.flush()
            ser.baudrate = DMX_BAUD

    def send(self, slots) -> None:
        ser = self._ser
        if ser is None:
            raise DriverError("driver is not open")
        n = len(slots)
        self._buf[0] = 0x00  # start code: 0 means "dimmer data"
        self._buf[1 : n + 1] = slots
        try:
            self._emit_break()
            ser.write(memoryview(self._buf)[: n + 1])
        except Exception as e:
            # One dropped frame is a flicker; a raise here would be a dark room.
            log.warning("frame write failed on %s: %s", self.port, e)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def describe(self) -> str:
        return f"Open DMX USB via serial {self.port} (break={self.break_mode})"
