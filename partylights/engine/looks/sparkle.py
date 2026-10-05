"""Sparkle: brief bright flashes on hi-hats, scattered across the rig.

Driven by the hat region, which is the reason onset detection is split by
frequency at all: hats are the fastest thing in most tracks and completely
inaudible to a broadband beat detector sitting next to a kick drum.

Random fixture selection rather than a pattern. For fast sparse events,
randomness reads as texture while any fixed pattern immediately reads as a
pattern -- and at hi-hat rates the eye picks a pattern out instantly.
"""

from __future__ import annotations

import random

from ...fixtures.color import Emission, clamp
from .base import Look

#: Short, but long enough to be seen as a glint fading out rather than a
#: single-frame blip. 0.13 was invisible flicker on the real fixtures.
DECAY_S = 0.7
#: How many fixtures a single hat lights.
PER_HIT = 2
#: The colour bed under the glints. Above the PARs' min_dimmer (about 0.28),
#: so the room holds its colour between hats instead of dropping to black.
FLOOR = 0.32


class SparkleLook(Look):
    name = "sparkle"
    description = "Scattered flashes on hi-hats over a dim wash"
    #: Output release. Short or long because a glint has to end to read as a glint.
    release_s = 0.20

    def __init__(self, patch):
        super().__init__(patch)
        self._levels: dict[str, float] = {}
        self._rng = random.Random()

    def reset(self) -> None:
        self._levels = {}

    def render(self, music, palette, dt):
        decay = pow(0.001, dt / (DECAY_S * self.time_scale)) if DECAY_S > 0 else 0.0
        self._levels = {k: v * decay for k, v in self._levels.items() if v * decay > 0.01}

        pars = self.pars
        if music.onset("hat") and pars:
            k = min(PER_HIT, len(pars))
            for f in self._rng.sample(pars, k):
                self._levels[f.fid] = clamp(0.55 + 0.3 * music.hit("hat"))

        # A kick lights everything faintly, so the rig has a pulse underneath
        # the sparkle rather than floating.
        bed = FLOOR + 0.18 * music.smooth("bass")

        out = {}
        for i, f in enumerate(pars):
            spark = self._levels.get(f.fid, 0.0)
            # A glint is the fixture's own palette colour getting brighter, not
            # a white flash: white over a coloured room read as flashing.
            color = palette.at(i)
            out[f.fid] = Emission(rgb=color, intensity=clamp(max(bed, spark)))

        for f in self.accents:
            out[f.fid] = Emission(
                rgb=palette.at(0),
                intensity=clamp(0.1 + 0.4 * music.smooth("bass")),
                uv=clamp(0.3 + 0.5 * music.smooth("air")),
            )
        return out
