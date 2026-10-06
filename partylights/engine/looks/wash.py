"""Wash: colour gradient across the room, intensity driven by the mix.

The workhorse. Low-frequency energy drives brightness, so the rig breathes with
the groove without needing a tempo lock, and the spectral centroid nudges colour
so a bright section reads differently from a dark one.

Colour is spread as a *gradient along the physical run* rather than assigned per
fixture. A gradient survives the rig being any size: twelve PARs or twenty, it
still reads as one coherent field instead of twelve separate decisions.
"""

from __future__ import annotations

from ...fixtures.color import Emission, clamp, mix
from .base import Look

#: How far through the palette the gradient spans. Less than 1 so neighbouring
#: fixtures stay related; a full span makes the room look like a paint chart.
SPREAD = 0.45
#: Seconds for the gradient to drift one full palette cycle.
DRIFT_S = 80.0
#: Floor brightness, so the room is never actually dark mid-track.
BASE = 0.12


class WashLook(Look):
    name = "wash"
    description = "Colour gradient across the room, brightness follows the mix"
    #: Output release. Short or long because it is the drifting look.
    release_s = 0.45

    def __init__(self, patch):
        super().__init__(patch)
        self._t = 0.0
        self._hue_bias = 0.0

    def reset(self) -> None:
        self._t = 0.0
        self._hue_bias = 0.0

    def render(self, music, palette, dt):
        self._t += dt

        low = max(music.smooth("bass"), music.smooth("sub"))
        mid = music.smooth("mid")
        high = max(music.smooth("high"), music.smooth("air"))

        # Brightness: mostly low end, with a little from the whole mix so a
        # bassless passage does not go dark. The whole-mix share is kept small
        # because music.energy is instantaneous and spikes on every transient;
        # at 0.30 it made the wash shimmer instead of breathe.
        drive = clamp(0.85 * low + 0.15 * music.energy)

        # Brighter-sounding music drifts further along the palette. Smoothed
        # heavily because the raw centroid is jittery and colour flicker is far
        # more noticeable than brightness flicker.
        target = clamp(high * 0.6 + mid * 0.2)
        self._hue_bias += (target - self._hue_bias) * clamp(dt * 1.5)

        out = {}
        pars = self.pars
        n = max(1, len(pars))
        for i, f in enumerate(pars):
            pos = i / n * SPREAD + self._t / DRIFT_S + self._hue_bias * 0.25
            color = palette.sample(pos)
            # Stagger the drive slightly along the run so the whole rig does not
            # pump in perfect unison, which looks mechanical.
            local = clamp(drive * (0.82 + 0.18 * ((i % 3) / 2.0)))
            out[f.fid] = Emission(rgb=color, intensity=clamp(BASE + 0.88 * local))

        for f in self.accents:
            # The accent sits a third of the way further round the palette and
            # leans on its white emitter, so it reads as a different fixture
            # rather than a thirteenth PAR.
            color = palette.sample(self._t / DRIFT_S + 0.33)
            out[f.fid] = Emission(
                rgb=mix(color, (1.0, 0.85, 0.7), 0.35),
                intensity=clamp(BASE + 0.8 * drive),
                uv=0.15 * high,
            )
        return out
