"""Chase: a bright head travelling along the physical run, locked to the beat.

This is the look that most needs the tempo tracker, and the clearest
demonstration of why phase tracking beats reacting to onsets. The head position
is computed from beat *phase*, so it is already moving toward the next fixture
before the beat lands and arrives exactly on it. A chase driven by onset
callbacks always looks a fraction late, and at 128 BPM a fraction late is
visible.

Fixture order comes from `position` in rig.yaml, which is why that file asks you
to list the PARs in the order they are physically arranged. Out of order, this
look is just random flashing.
"""

from __future__ import annotations

import math

from ...fixtures.color import Emission, clamp
from .base import Look

#: Width of the travelling head, in fixtures. Below about 1.5 the movement
#: strobes rather than sweeps.
WIDTH = 1.9
#: Residual brightness on fixtures the head has left.
TAIL = 0.07


class ChaseLook(Look):
    name = "chase"
    description = "Bright head sweeping the room, locked to the beat"
    needs_tempo = True

    def __init__(self, patch):
        super().__init__(patch)
        self._pos = 0.0
        self._bounce = 1

    def reset(self) -> None:
        self._pos = 0.0
        self._bounce = 1

    def render(self, music, palette, dt):
        pars = self.pars
        n = max(1, len(pars))

        # One full sweep per bar. Using the continuous bar phase (rather than
        # stepping on beat events) is what makes the motion smooth and on time.
        if music.tempo_locked:
            sweep = music.bar_phase
        else:
            # No lock: free-run at a plausible walking pace so the look still
            # does something sensible rather than freezing.
            self._pos += dt / 2.0
            sweep = self._pos % 1.0

        # Ping-pong rather than wrapping: a head that jumps from the last
        # fixture back to the first reads as a glitch in a line of lights.
        tri = 2.0 * sweep if sweep < 0.5 else 2.0 * (1.0 - sweep)
        head = tri * (n - 1)

        # Colour steps once per bar, so the sweep keeps a single colour for its
        # whole travel instead of smearing through the palette.
        bar = music.beat_index // 4
        color = palette.at(bar)

        # Hits widen the head, which makes the chase feel connected to the music
        # without breaking its timing.
        punch = max(music.hit("kick"), music.hit("snare") * 0.6)
        width = WIDTH * (1.0 + 0.8 * punch)

        out = {}
        for i, f in enumerate(pars):
            d = abs(i - head)
            level = math.exp(-(d * d) / (2.0 * width * width))
            out[f.fid] = Emission(
                rgb=color,
                intensity=clamp(TAIL + (0.95 - TAIL) * level),
            )

        for f in self.accents:
            # The accent counter-moves: brightest when the head is mid-run, so
            # it fills the gap at the turnarounds rather than competing.
            fill = 1.0 - abs(tri - 0.5) * 2.0
            out[f.fid] = Emission(
                rgb=palette.at(bar + 1),
                intensity=clamp(0.10 + 0.45 * fill),
                uv=0.1,
            )
        return out
