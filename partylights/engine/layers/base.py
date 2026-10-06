"""What every layer sees, and the clock they share.

A layer is a small object with a `render(ctx, fids)` method: a colour field
returns a colour per fixture, a mask returns a level 0..1 per fixture. Layers
may keep animation state (a decaying hit, rings in flight); `LayeredLook`
rebuilds them on reset, so they never need a reset of their own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ...audio.analyser import MusicState
from ..palette import Palette
from ..space import Space

#: Tempo the beat clock free-runs at without a lock, so beat-shaped layers
#: still move at a believable pace rather than freezing.
FREE_BPM = 120.0

#: Fastest a pattern may step when the song arc speeds it up, in steps per
#: second. Full-room flashing between roughly 5 and 30 Hz is the
#: photosensitive-seizure risk band; a doubled-up build must stay under it.
#: 4.5 still allows eighth notes up to 135 BPM, which is most of house.
MAX_STEP_HZ = 4.5


@dataclass
class Ctx:
    music: MusicState
    palette: Palette
    dt: float
    space: Space
    #: The softness control. Multiply decays by it; leave motion speeds alone.
    time_scale: float
    #: Seconds since the look became active.
    t: float
    #: Continuous beats: beat_index + beat_phase with a lock, else free-running.
    beats: float
    #: Speed-up from the song-arc director, a power of two. Patterns whose
    #: period divides a 16-beat phrase stay in phase when it changes, because
    #: it only changes on a phrase boundary.
    rate: int = 1
    #: Beats per second, for capping the speed-up.
    bps: float = FREE_BPM / 60.0

    @property
    def locked(self) -> bool:
        return self.music.tempo_locked

    def step(self, bars: float) -> int:
        """Which colour step: one per `bars` bars, on the beat clock."""
        return int(self.beats // (4.0 * bars)) if bars > 0 else 0

    def speed(self, beats: float) -> int:
        """The arc's rate for a pattern `beats` long, halved until the pattern
        steps no faster than MAX_STEP_HZ. Stays a power of two."""
        r = max(1, int(self.rate))
        while r > 1 and r * self.bps / beats > MAX_STEP_HZ:
            r //= 2
        return r

    def cycle(self, beats: float) -> float:
        """0..1 position through a pattern `beats` long, sped up by `rate`."""
        return (self.beats * self.speed(beats) / beats) % 1.0 if beats > 0 else 0.0

    def count(self, beats: float) -> int:
        """Whole periods of `beats` elapsed, sped up by `rate`."""
        return int(self.beats * self.speed(beats) // beats) if beats > 0 else 0

    def decay(self, seconds: float) -> float:
        """Per-frame multiplier that takes a level to ~5% in `seconds`."""
        s = seconds * self.time_scale
        return math.exp(-3.0 * self.dt / s) if s > 0 else 0.0


def beat_clock(music: MusicState, t: float) -> float:
    if music.tempo_locked:
        return music.beat_index + music.beat_phase
    return t * FREE_BPM / 60.0


def coord(space: Space, fid: str, name: str, degrees: float = 0.0) -> float:
    """A fixture's 0..1 coordinate along one of the room's axes.

    along   straight across the rig at `degrees` (0 = left to right)
    path    patch order, the route a chase takes around a U
    angle   turns around the centre, for rotation
    radius  out from the centre
    side    0 on the left half, 1 on the right
    """
    if name == "along":
        return space.along(fid, degrees)
    s = space.spot(fid)
    if name == "path":
        return s.path
    if name == "angle":
        return (s.angle - degrees / 360.0) % 1.0
    if name == "radius":
        return s.radius
    if name == "side":
        return float(s.side)
    raise ValueError(f"unknown coordinate {name!r}")
