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

The PARs make a closed ring round the lawn, so the head laps it rather than
running to one end and bouncing back. It turns round on each colour change,
when the head is back at the first fixture, so the reversal never jumps.
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
#: Bars for one lap of the ring. Was 1 for a there-and-back on a line, which
#: read as frantic at party tempos.
BARS_PER_SWEEP = 2
#: Seconds per lap when there is no tempo lock.
FREE_RUN_S = 4.0
#: Bars between colour changes, and between changes of direction: two laps.
BARS_PER_COLOUR = 4


class ChaseLook(Look):
    name = "chase"
    wildness = 0.75
    description = "Bright head lapping the ring of PARs, locked to the beat"
    #: Output release. Short or long because the head should read as a light, not a smear.
    release_s = 0.20
    #: The hit goes round the triangle as the head goes round the ring.
    bass = "round"
    needs_tempo = True

    def __init__(self, patch):
        super().__init__(patch)
        self._pos = 0.0

    def reset(self) -> None:
        self._pos = 0.0

    def render(self, music, palette, dt):
        pars = self.pars
        n = max(1, len(pars))

        bar = music.beat_index // 4
        # One lap every BARS_PER_SWEEP bars. Using the continuous bar phase
        # (rather than stepping on beat events) is what makes the motion
        # smooth and on time.
        if music.tempo_locked:
            lap = ((bar % BARS_PER_SWEEP) + music.bar_phase) / BARS_PER_SWEEP
            backwards = (bar // BARS_PER_COLOUR) % 2 == 1
        else:
            # No lock: free-run at an unhurried pace so the look still does
            # something sensible rather than freezing.
            self._pos += dt / FREE_RUN_S
            lap = self._pos % 1.0
            backwards = False
        head = (1.0 - lap if backwards else lap) * n

        # Colour steps every BARS_PER_COLOUR bars, on a lap boundary, so a lap
        # keeps one colour all the way round.
        color = palette.at(bar // BARS_PER_COLOUR)

        # Hits widen the head, which makes the chase feel connected to the music
        # without breaking its timing.
        punch = max(music.hit("kick"), music.hit("snare") * 0.6)
        width = WIDTH * (1.0 + 0.4 * punch)

        out = {}
        for i, f in enumerate(pars):
            d = abs(i - head) % n
            d = min(d, n - d)           # round the ring, not across it
            level = math.exp(-(d * d) / (2.0 * width * width))
            out[f.fid] = Emission(
                rgb=color,
                intensity=clamp(TAIL + (0.95 - TAIL) * level),
            )

        for f in self.accents:
            # The accent holds the next colour; the engine passes the bass hit
            # round the three scenery washes as the head laps (bass = "round").
            out[f.fid] = Emission(
                rgb=palette.at(bar // BARS_PER_COLOUR + 1),
                intensity=0.35,
                uv=0.1,
            )
        return out
