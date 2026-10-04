"""Pulse: the whole rig hits on the kick, in alternating halves.

The most direct translation of music to light, and the one that most rewards
getting the onset detection right -- which is why kick onsets are detected in
their own narrow frequency region rather than from a broadband beat signal.

Alternating between odd and even fixtures on successive kicks gives the look
movement without needing a tempo lock. A single flash of everything on every
kick is the obvious version and it reads as a fault light.
"""

from __future__ import annotations

from ...fixtures.color import Emission, clamp
from .base import Look

#: Seconds for a hit to decay. Short enough to be punchy, long enough that the
#: fixture visibly glows down rather than snapping off.
DECAY_S = 0.28
#: Brightness between hits.
FLOOR = 0.06


class PulseLook(Look):
    name = "pulse"
    description = "Alternating halves hit on every kick"

    def __init__(self, patch):
        super().__init__(patch)
        self._level = [0.0, 0.0]   # decaying envelope per half
        self._side = 0
        self._color_index = 0

    def reset(self) -> None:
        self._level = [0.0, 0.0]
        self._side = 0
        self._color_index = 0

    def render(self, music, palette, dt):
        # Decay both halves. Exponential rather than linear so the tail looks
        # like a light going out rather than a fader being pulled.
        decay = pow(0.001, dt / DECAY_S) if DECAY_S > 0 else 0.0
        self._level = [v * decay for v in self._level]

        if music.onset("kick"):
            self._side ^= 1
            self._level[self._side] = clamp(0.55 + 0.45 * music.hit("kick"))
            self._color_index += 1
        elif music.onset("snare"):
            # Snares light the *other* half at reduced level, which fills in a
            # backbeat without competing with the kick.
            other = self._side ^ 1
            self._level[other] = max(self._level[other], 0.35 * music.hit("snare") + 0.2)

        out = {}
        pars = self.pars
        for i, f in enumerate(pars):
            half = i % 2
            color = palette.at(self._color_index // 2 + half)
            out[f.fid] = Emission(
                rgb=color,
                intensity=clamp(FLOOR + (1.0 - FLOOR) * self._level[half]),
            )

        for f in self.accents:
            # Accent carries the sustained energy rather than the transients, so
            # there is something continuous underneath the flashing.
            out[f.fid] = Emission(
                rgb=palette.at(self._color_index // 4),
                intensity=clamp(0.12 + 0.5 * music.smooth("bass")),
                uv=0.2 * music.smooth("air"),
            )
        return out
