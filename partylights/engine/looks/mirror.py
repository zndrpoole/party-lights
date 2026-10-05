"""Mirror: the room as two halves moving symmetrically.

Each kick launches a ring of light that bursts from the centre out to both ends,
and the next closes from the ends back to the centre. It has shape and motion
without the side-to-side sweep of a chase, and it needs no tempo lock: the
kicks drive it directly.

One colour at a time, on a steady bed. Mixing a second colour into the ring
looked clever on paper and muddy on RGB-only PARs.
"""

from __future__ import annotations

import math

from ...fixtures.color import Emission, clamp
from .base import Look

#: Seconds for a ring to cross from the centre to the ends.
RING_S = 0.45
#: Ring width, as a fraction of the half-run.
RING_W = 0.18
#: Bed brightness, above the PARs' min_dimmer (about 0.28).
BED = 0.34
#: Bars per colour change with a tempo lock; detected kicks without one.
BARS_PER_COLOUR = 4
KICKS_PER_COLOUR = 24


class MirrorLook(Look):
    name = "mirror"
    description = "Hits burst from the centre out, then close back in — symmetric"
    #: Output release. Short, so the ring stays a ring rather than a smear.
    release_s = 0.15

    def __init__(self, patch):
        super().__init__(patch)
        self.reset()

    def reset(self) -> None:
        self._rings: list[list] = []     # [age_s, outward, strength]
        self._kicks = 0

    def render(self, music, palette, dt):
        for ring in self._rings:
            ring[0] += dt
        self._rings = [r for r in self._rings if r[0] < RING_S * 1.6]
        if music.onset("kick"):
            outward = self._kicks % 2 == 0
            self._kicks += 1
            self._rings.append([0.0, outward, clamp(0.7 + 0.3 * music.hit("kick"))])

        pars = self.pars
        n = len(pars)
        centre = (n - 1) / 2.0
        half = max(centre, 1e-6)
        bed = BED + 0.10 * music.smooth("bass")
        step = self.colour_step(music, BARS_PER_COLOUR, KICKS_PER_COLOUR, self._kicks)
        color = palette.at(step)

        out = {}
        for i, f in enumerate(pars):
            d = abs(i - centre) / half              # 0 at centre, 1 at the ends
            ring_level = 0.0
            for age, outward, strength in self._rings:
                x = age / RING_S
                r = x if outward else 1.0 - x
                fade = math.exp(-max(0.0, x - 1.0) * 4.0)
                ring_level = max(ring_level, strength * fade *
                                 math.exp(-((d - r) ** 2) / (2 * RING_W ** 2)))
            out[f.fid] = Emission(rgb=color, intensity=clamp(max(bed, ring_level)))

        # The accent sits at the centre of the run in spirit: it flashes as an
        # outward ring leaves and as an inward one arrives.
        centre_level = 0.0
        for age, outward, strength in self._rings:
            r = age / RING_S if outward else 1.0 - age / RING_S
            centre_level = max(centre_level, strength * math.exp(-(r ** 2) / (2 * RING_W ** 2)))
        for f in self.accents:
            out[f.fid] = Emission(rgb=palette.at(step + 1),
                                  intensity=clamp(max(bed, 0.85 * centre_level)),
                                  uv=0.1, emitter_bias=0.3)
        return out
