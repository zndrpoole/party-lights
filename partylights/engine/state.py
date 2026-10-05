"""Engine state: everything the host can change from the UI or a cue.

Held in one place behind one lock, because it is written from several threads --
the web request handler, the cue API, the Stream Deck, the keyboard page -- and
read by the engine tick. Keeping it in one object makes the thread-safety story
a single sentence rather than a per-field investigation.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..fixtures.color import Emission, Rgb, clamp


@dataclass
class ManualFixture:
    """A per-fixture manual override.

    `active` is separate from the values so the UI can park a setting and toggle
    it, which is how a host actually works: set a fixture up, then bring it in.
    """

    active: bool = False
    rgb: Rgb = (1.0, 1.0, 1.0)
    intensity: float = 1.0
    uv: float = 0.0
    strobe: float = 0.0

    def emission(self) -> Emission:
        return Emission(rgb=self.rgb, intensity=self.intensity,
                        uv=self.uv, strobe=self.strobe)


class EngineState:
    """Mutable, thread-safe control state."""

    def __init__(
        self,
        *,
        look: str = "ambient",
        palette: str = "halloween",
        max_strobe_seconds: float = 4.0,
    ):
        self._lock = threading.RLock()

        #: "auto" lets the engine choose the look from the music; "manual"
        #: pins whatever the host selected.
        self.mode = "auto"
        self.look = look
        self.palette = palette
        self.master = 1.0
        #: Sharp (-1) to smooth (+1); 0 is the tuning as written. See
        #: softness_scale().
        self.softness = 0.0
        #: Mirrors of the universe's own flags, so the UI can read one object.
        self.blackout = False
        self.freeze = False

        self.manual: dict[str, ManualFixture] = {}
        #: Test-bench overrides: fid -> {role: byte}, written to the wire as-is.
        self.raw: dict[str, dict[str, int]] = {}

        #: Safety rail. Strobe looks are time-limited so a forgotten cue cannot
        #: leave the room flashing. See engine/looks/strobe.py.
        self.max_strobe_seconds = max_strobe_seconds
        self._strobe_since: float | None = None

        #: Set by the jukebox poller; purely informational for the UI.
        self.track_title = ""
        self.track_artist = ""

    # -- generic access ---------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "look": self.look,
                "palette": self.palette,
                "master": round(self.master, 3),
                "softness": round(self.softness, 3),
                "blackout": self.blackout,
                "freeze": self.freeze,
                "track_title": self.track_title,
                "track_artist": self.track_artist,
                "manual": {
                    fid: {"active": m.active, "rgb": [round(c, 3) for c in m.rgb],
                          "intensity": round(m.intensity, 3), "uv": round(m.uv, 3),
                          "strobe": round(m.strobe, 3)}
                    for fid, m in self.manual.items()
                },
                "raw": {fid: dict(v) for fid, v in self.raw.items()},
            }

    def set_mode(self, mode: str) -> None:
        if mode not in ("auto", "manual"):
            raise ValueError(f"mode must be auto or manual, got {mode!r}")
        with self._lock:
            self.mode = mode

    def toggle_mode(self) -> str:
        with self._lock:
            self.mode = "manual" if self.mode == "auto" else "auto"
            return self.mode

    def set_look(self, name: str) -> None:
        with self._lock:
            self.look = name

    def set_palette(self, name: str) -> None:
        with self._lock:
            self.palette = name

    def set_master(self, value: float) -> None:
        with self._lock:
            self.master = clamp(value)

    def set_softness(self, value: float) -> None:
        with self._lock:
            self.softness = clamp(value, -1.0, 1.0)

    def softness_scale(self) -> float:
        """Multiplier for every fade time: x0.25 fully sharp, x4 fully smooth.

        Exponential so the middle of the slider is the written tuning and each
        half covers the same feel of range in both directions.
        """
        with self._lock:
            return 4.0 ** self.softness

    # -- manual overrides -------------------------------------------------

    def manual_for(self, fid: str) -> ManualFixture:
        with self._lock:
            return self.manual.setdefault(fid, ManualFixture())

    def update_manual(self, fid: str, **kwargs) -> ManualFixture:
        with self._lock:
            m = self.manual.setdefault(fid, ManualFixture())
            for key, value in kwargs.items():
                if not hasattr(m, key):
                    raise ValueError(f"unknown manual field {key!r}")
                if key == "rgb":
                    value = (clamp(value[0]), clamp(value[1]), clamp(value[2]))
                elif key == "active":
                    value = bool(value)
                else:
                    value = clamp(float(value))
                setattr(m, key, value)
            return m

    def clear_manual(self) -> None:
        with self._lock:
            for m in self.manual.values():
                m.active = False

    def active_manual(self) -> dict[str, Emission]:
        with self._lock:
            return {fid: m.emission() for fid, m in self.manual.items() if m.active}

    # -- test bench -------------------------------------------------------

    def set_raw(self, fid: str, values: dict[str, int]) -> dict[str, int]:
        """Merge raw channel bytes into a fixture's bench override."""
        with self._lock:
            held = self.raw.setdefault(fid, {})
            for role, value in values.items():
                value = int(value)
                if not 0 <= value <= 255:
                    raise ValueError(f"{role} must be 0..255, got {value}")
                held[role] = value
            return dict(held)

    def release_raw(self, fids=None) -> None:
        """Hand fixtures back to the show. None releases every one."""
        with self._lock:
            if fids is None:
                self.raw.clear()
            else:
                for fid in fids:
                    self.raw.pop(fid, None)

    def active_raw(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {fid: dict(v) for fid, v in self.raw.items()}

    # -- strobe safety ----------------------------------------------------

    def strobe_allowed(self, look_name: str, now: float | None = None) -> bool:
        """Whether a strobe look may keep running.

        Starts a timer the first time a strobe look is seen and returns False
        once it has run for max_strobe_seconds. The engine falls back to another
        look at that point, so the room cannot be left flashing by a stuck
        button or a cue nobody cancelled.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            if look_name != "strobe":
                self._strobe_since = None
                return True
            if self.max_strobe_seconds <= 0:
                return True
            if self._strobe_since is None:
                self._strobe_since = now
                return True
            return (now - self._strobe_since) < self.max_strobe_seconds

    def strobe_elapsed(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        with self._lock:
            return 0.0 if self._strobe_since is None else now - self._strobe_since
