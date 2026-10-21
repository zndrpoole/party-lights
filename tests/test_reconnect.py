"""DMX output survives the adapter being unplugged.

Bug guarded: twice before the party the rig sat dark for days while the UI
looked healthy. The service had launched with the ENTTEC missing, fallen back
to the null driver, and never looked for the adapter again; a cable pulled
mid-run left the serial driver logging a failed write every frame forever.
"""

import os

import pytest

from partylights.config import Settings, build_driver
from partylights.dmx.driver import Driver, DriverError
from partylights.dmx.null import NullDriver
from partylights.dmx.opendmx_serial import OpenDmxSerialDriver
from partylights.dmx.reconnect import ReconnectingDriver
from partylights.dmx.universe import Universe


class FakeHardware(Driver):
    name = "fake"

    def __init__(self):
        self.plugged = False
        self.is_open = False
        self.frames = []
        self.opens = 0

    def open(self):
        self.opens += 1
        if not self.plugged:
            raise DriverError("no such device")
        self.is_open = True

    def send(self, slots):
        if not self.plugged:
            raise DriverError("link gone")
        self.frames.append(bytes(slots))

    def close(self):
        self.is_open = False


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make():
    hw, clock = FakeHardware(), Clock()
    return hw, clock, ReconnectingDriver(hw, retry_s=2.0, clock=clock)


def test_missing_at_launch_is_not_fatal_and_connects_when_plugged():
    hw, clock, drv = make()
    drv.open()  # must not raise
    assert not drv.connected
    assert drv.status()["error"] == "no such device"
    assert "DISCONNECTED" in drv.describe()

    drv.send(b"\x01")
    assert hw.opens == 1  # retry is rate-limited, not every frame

    hw.plugged = True
    clock.t += 2.1
    drv.send(b"\x02")
    assert drv.connected
    assert hw.frames == [b"\x02"]
    assert drv.status() == {"connected": True, "down_since": None,
                            "error": None, "reconnects": 1}


def test_unplug_mid_run_closes_and_reopens():
    hw, clock, drv = make()
    hw.plugged = True
    drv.open()
    drv.send(b"\x01")

    hw.plugged = False
    drv.send(b"\x02")
    assert not drv.connected and not hw.is_open
    assert drv.status()["down_since"] is not None

    hw.plugged = True
    clock.t += 1.0
    drv.send(b"\x03")  # too soon to retry
    clock.t += 1.5
    drv.send(b"\x04")
    assert drv.connected and hw.is_open
    assert hw.frames == [b"\x01", b"\x04"]


def test_universe_reports_the_link():
    hw, _, drv = make()
    drv.open()
    assert Universe(drv).stats()["link"]["connected"] is False
    assert Universe(NullDriver()).stats()["link"] is None


def test_hardware_drivers_are_wrapped_and_null_is_not():
    s = Settings({"dmx": {"driver": "serial", "port": "/dev/nope"}})
    assert isinstance(build_driver(s), ReconnectingDriver)
    assert isinstance(build_driver(s, override="null"), NullDriver)


class FakeSerial:
    """pyserial's Serial, minus the hardware: opens only if the port path exists."""
    written = []

    def __init__(self):
        self.port = None
        self.fail = None

    def open(self):
        if not os.path.exists(self.port):
            raise OSError(2, f"No such file or directory: {self.port!r}")

    def write(self, data):
        if self.fail:
            raise self.fail
        FakeSerial.written.append(bytes(data))

    def flush(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_serial(monkeypatch):
    import partylights.dmx.opendmx_serial as mod
    monkeypatch.setattr(mod.serial, "Serial", FakeSerial)
    FakeSerial.written = []
    return FakeSerial


def test_serial_driver_raises_after_a_run_of_failed_writes(fake_serial, tmp_path):
    port = tmp_path / "cu.usbserial-TEST"
    port.touch()
    drv = OpenDmxSerialDriver(str(port), break_mode="baud")
    drv.open()
    drv.max_failures = 3
    drv._ser.fail = OSError(6, "Device not configured")
    drv.send(b"\x00" * 8)
    drv.send(b"\x00" * 8)
    with pytest.raises(DriverError):
        drv.send(b"\x00" * 8)


def test_serial_port_appearing_later_is_picked_up(fake_serial, tmp_path):
    port = tmp_path / "cu.usbserial-TEST"
    clock = Clock()
    drv = ReconnectingDriver(OpenDmxSerialDriver(str(port), break_mode="baud"),
                             retry_s=2.0, clock=clock)
    drv.open()
    assert not drv.connected

    port.touch()
    clock.t += 2.1
    drv.send(b"\x07" * 8)
    assert drv.connected
    assert fake_serial.written[-1] == b"\x00" + b"\x07" * 8
