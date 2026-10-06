"""UV accent: the look that only exists because of the thirteenth fixture.

The twelve PARs are RGB-only. The ZQ01430 has white, amber and ultraviolet
emitters, so it can do two things nothing else in the rig can: genuinely warm
light, and blacklight. This look leans on both -- deep UV from the accent while
the PARs sit on a dark, saturated bed.

It reads as much darker than the other looks, which is the point: it is the
late-night look.
"""

from __future__ import annotations

import math

from ...fixtures.color import Emission, clamp
from .base import Look


class UvAccentLook(Look):
    name = "uv"
    description = "Blacklight from the accent fixture over a dark bed"
    #: Output release. Short or long because it is the late-night look.
    release_s = 0.60

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
            # Deep, slow, and dim. Anything brighter here washes out the UV
            # effect, which is the whole reason for the look.
            wave = 0.5 + 0.5 * math.sin(2.0 * math.pi * (self._t / 14.0 + i / n))
            out[f.fid] = Emission(
                rgb=palette.sample(0.6 + 0.2 * wave),
                intensity=clamp(0.06 + 0.18 * wave + 0.25 * music.smooth("bass")),
            )

        for f in self.accents:
            if not f.profile.has_uv:
                continue
            out[f.fid] = Emission(
                rgb=(0.0, 0.0, 0.0),
                intensity=1.0,
                uv=clamp(0.75 + 0.25 * music.smooth("bass")),
            )
        return out
