"""Settings loading and driver construction.

Config is a plain nested dict with dotted-path access, deliberately not a schema
library: the file is small, hand-edited at a party, and a missing key should fall
back to a sane default rather than refuse to start.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

#: Repo root, so the CLI works from any directory.
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DEFAULT_SETTINGS = CONFIG_DIR / "settings.yaml"
DEFAULT_RIG = CONFIG_DIR / "rig.yaml"


class Settings:
    """Dotted-path read-only view over the settings file."""

    def __init__(self, data: dict, source: str = ""):
        self._data = data or {}
        self.source = source

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict:
        value = self._data.get(name)
        return value if isinstance(value, dict) else {}

    @property
    def data(self) -> dict:
        return self._data

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        path = Path(path) if path else DEFAULT_SETTINGS
        if not path.exists():
            log.warning("no settings file at %s — using defaults throughout", path)
            return cls({}, source=str(path))
        with path.open() as fh:
            return cls(yaml.safe_load(fh) or {}, source=str(path))


def build_driver(settings: Settings, *, override: str | None = None):
    """Construct the output driver named in settings (or by `override`).

    Import of each driver is deferred so a missing optional dependency only
    matters if you actually selected that driver.
    """
    kind = (override or settings.get("dmx.driver", "null")).lower()

    if kind == "null":
        from .dmx.null import NullDriver
        return NullDriver()

    if kind == "serial":
        from .dmx.opendmx_serial import OpenDmxSerialDriver
        port = settings.get("dmx.port")
        if not port:
            raise ValueError("dmx.driver is 'serial' but dmx.port is not set")
        return OpenDmxSerialDriver(
            port=port,
            break_mode=settings.get("dmx.break_mode", "ioctl"),
        )

    if kind == "ftdi":
        from .dmx.opendmx_ftdi import OpenDmxFtdiDriver
        return OpenDmxFtdiDriver(serial_number=settings.get("dmx.serial_number") or None)

    raise ValueError(f"unknown dmx.driver {kind!r} — expected serial, ftdi or null")


def build_universe(settings: Settings, patch=None, *, driver_override: str | None = None):
    """Driver plus Universe, with the frame shortened to fit the patch.

    Sending only the slots the rig uses takes a 513-byte frame from ~23 ms down
    to ~4 ms, which is the difference between 40 Hz being tight and 40 Hz being
    comfortable.
    """
    from .dmx.universe import DMX_SLOTS, Universe

    driver = build_driver(settings, override=driver_override)
    slots = patch.max_channel if patch is not None else DMX_SLOTS
    return Universe(
        driver,
        slot_count=slots,
        refresh_hz=float(settings.get("dmx.refresh_hz", 40)),
    )


def load_patch(rig_path: str | Path | None = None):
    from .fixtures.patch import Patch
    return Patch.load(Path(rig_path) if rig_path else DEFAULT_RIG)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
    )
