"""Open DMX USB driven directly over libusb, bypassing Apple's serial driver.

Why bother: BREAK here is a single FTDI control request rather than a termios
ioctl translated by a kernel driver, so frame timing is materially tighter. On a
music-reactive rig, frame jitter is visible.

The catch is that AppleUSBFTDI claims the FT232R the moment it is plugged in, and
libusb cannot claim an interface the kernel holds. On Intel Macs you can unload
it:

    sudo kextunload -b com.apple.driver.AppleUSBFTDI

On Apple Silicon, SIP prevents that, so this driver will fail to claim and you
should use the serial driver instead. open() says so explicitly rather than
leaving you to decode a libusb error code.
"""

from __future__ import annotations

import logging

from .driver import DMX_BAUD, Driver, DriverError

log = logging.getLogger(__name__)

FTDI_VID = 0x0403
FT232R_PID = 0x6001

_CLAIM_HINT = (
    "could not claim the FTDI interface — Apple's AppleUSBFTDI driver is probably "
    "holding it. On an Intel Mac: sudo kextunload -b com.apple.driver.AppleUSBFTDI "
    "(then unplug/replug). On Apple Silicon this is blocked by SIP, so set "
    "dmx.driver: serial in config/settings.yaml instead."
)


class OpenDmxFtdiDriver(Driver):
    name = "opendmx-ftdi"

    def __init__(
        self,
        *,
        serial_number: str | None = None,
        vid: int = FTDI_VID,
        pid: int = FT232R_PID,
        break_s: float = 0.000176,
        mab_s: float = 0.000020,
    ):
        self.serial_number = serial_number
        self.vid = vid
        self.pid = pid
        self.break_s = break_s
        self.mab_s = mab_s
        self._ftdi = None
        self._buf = bytearray(513)

    def _url(self) -> str:
        tail = f":{self.serial_number}" if self.serial_number else ":"
        return f"ftdi://0x{self.vid:04x}:0x{self.pid:04x}{tail}/1"

    def open(self) -> None:
        try:
            from pyftdi.ftdi import Ftdi
        except ImportError as e:
            raise DriverError("pyftdi is not installed — pip install pyftdi") from e

        ftdi = Ftdi()
        url = self._url()
        try:
            ftdi.open_from_url(url)
        except Exception as e:
            msg = str(e).lower()
            if "claim" in msg or "access" in msg or "busy" in msg or "permission" in msg:
                raise DriverError(_CLAIM_HINT) from e
            raise DriverError(f"could not open {url}: {e}") from e

        try:
            ftdi.set_baudrate(DMX_BAUD)
            ftdi.set_line_property(8, 2, "N")
            ftdi.purge_buffers()
        except Exception as e:
            ftdi.close()
            raise DriverError(f"could not configure FTDI for DMX: {e}") from e

        self._ftdi = ftdi
        log.info("Open DMX USB ready over libusb (%s)", url)

    def send(self, slots) -> None:
        ftdi = self._ftdi
        if ftdi is None:
            raise DriverError("driver is not open")
        n = len(slots)
        self._buf[0] = 0x00
        self._buf[1 : n + 1] = slots
        try:
            # BREAK and MAB are expressed as line-property changes; the USB
            # round-trip for each is itself longer than the DMX minimum, so we do
            # not need to pad them.
            ftdi.set_line_property(8, 2, "N", break_=True)
            ftdi.set_line_property(8, 2, "N", break_=False)
            ftdi.write_data(memoryview(self._buf)[: n + 1])
        except Exception as e:
            log.warning("frame write failed over libusb: %s", e)

    def close(self) -> None:
        if self._ftdi is not None:
            try:
                self._ftdi.close()
            except Exception:
                pass
            self._ftdi = None

    def describe(self) -> str:
        who = self.serial_number or "first match"
        return f"Open DMX USB via libusb/pyftdi ({who})"
