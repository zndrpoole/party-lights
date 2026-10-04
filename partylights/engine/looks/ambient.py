"""Ambient: slow breathing wash. The look for when nothing is happening.

Every rig needs a graceful idle. Without one, silence between tracks leaves the
room either pitch black or stuck on whatever frame the last song ended on, and
both read as a fault. This look is also the safe fallback whenever the engine is
unsure -- no audio, no tempo, a crashed analyser.

It deliberately ignores the music almost entirely. The motion comes from two
slow oscillators at incommensurate periods, so the rig never visibly repeats.
"""

from __future__ import annotations

import math

from ...fixtures.color import Emission
from .base import Look

#: Seconds per breath. Long enough to read as "alive" rather than "pulsing".
BREATH_S = 9.0
#: Seconds to drift once through the whole palette.
DRIFT_S = 75.0


class AmbientLook(Look):
    name = "ambient"
    description = "Slow breathing wash — for lulls, speeches and silence"

    def __init__(self, patch):
        super().__init__(patch)
        self._t = 0.0

    def reset(self) -> None:
        self._t = 0.0

    def render(self, music, palette, dt):
        self._t += dt
        out = {}
        pars = self.pars
        n = max(1, len(pars))

        for i, f in enumerate(pars):
            # Offsetting phase along the physical run turns a uniform pulse into
            # a slow wave travelling through the room.
            phase = self._t / BREATH_S + i / n * 0.6
            breath = 0.5 + 0.5 * math.sin(2.0 * math.pi * phase)
            level = 0.18 + 0.30 * breath
            color = palette.sample(self._t / DRIFT_S + i / n * 0.25)
            out[f.fid] = Emission(rgb=color, intensity=level)

        # The accent fixture gets to do what the PARs cannot: a warm amber wash
        # with a trace of UV, which makes white clothing glow faintly.
        for f in self.accents:
            glow = 0.5 + 0.5 * math.sin(2.0 * math.pi * self._t / (BREATH_S * 1.37))
            out[f.fid] = Emission(
                rgb=(1.0, 0.62, 0.22),
                intensity=0.20 + 0.18 * glow,
                uv=0.25,
            )
        return out
