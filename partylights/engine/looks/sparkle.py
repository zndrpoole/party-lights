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

#: Very short: a sparkle that lingers is a flash, and a rig full of flashes is
#: just a bright rig.
DECAY_S = 0.13
#: How many fixtures a single hat lights.
PER_HIT = 3
FLOOR = 0.05


class SparkleLook(Look):
    name = "sparkle"
    description = "Scattered flashes on hi-hats over a dim wash"

    def __init__(self, patch):
        super().__init__(patch)
        self._levels: dict[str, float] = {}
        self._rng = random.Random()

    def reset(self) -> None:
        self._levels = {}

    def render(self, music, palette, dt):
        decay = pow(0.001, dt / DECAY_S) if DECAY_S > 0 else 0.0
        self._levels = {k: v * decay for k, v in self._levels.items() if v * decay > 0.01}

        pars = self.pars
        if music.onset("hat") and pars:
            k = min(PER_HIT, len(pars))
            for f in self._rng.sample(pars, k):
                self._levels[f.fid] = clamp(0.6 + 0.4 * music.hit("hat"))

        # A kick lights everything faintly, so the rig has a pulse underneath
        # the sparkle rather than floating.
        bed = FLOOR + 0.22 * music.smooth("bass")

        out = {}
        for i, f in enumerate(pars):
            spark = self._levels.get(f.fid, 0.0)
            # Sparkles are near-white; the bed carries the colour. Mixing colour
            # into the sparkle makes it muddy on RGB-only fixtures.
            color = palette.at(i) if spark < 0.2 else (1.0, 0.95, 0.85)
            out[f.fid] = Emission(rgb=color, intensity=clamp(max(bed, spark)))

        for f in self.accents:
            out[f.fid] = Emission(
                rgb=palette.at(0),
                intensity=clamp(0.1 + 0.4 * music.smooth("bass")),
                uv=clamp(0.3 + 0.5 * music.smooth("air")),
            )
        return out
