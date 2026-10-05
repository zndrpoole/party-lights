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

#: Seconds for a hit to decay to nothing. Was 0.28, which on the real rig read
#: as a flicker: with gamma on top the visible part of the fade was under 100 ms
#: and the PARs sat at DMX 1 between kicks. Long enough now that each hit visibly
#: glows down and overlaps the next kick, so the room pulses rather than blinks.
DECAY_S = 1.1
#: Brightness between hits. Zero: each kick fades out to black. A low floor
#: was tried (0.16), but it lands below the PARs' min_dimmer, where they show
#: a wrong hue rather than a dim version of the right one.
FLOOR = 0.0
#: Bars per colour change with a tempo lock; detected kicks without one (the
#: detector over-counts, so this is more than four bars' worth of real kicks).
BARS_PER_COLOUR = 4
KICKS_PER_COLOUR = 24


class PulseLook(Look):
    name = "pulse"
    description = "Alternating halves hit on every kick"
    #: Output release. Short or long because its own decay already shapes the roll-off.
    release_s = 0.12

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
        decay = pow(0.001, dt / (DECAY_S * self.time_scale)) if DECAY_S > 0 else 0.0
        self._level = [v * decay for v in self._level]

        if music.onset("kick"):
            self._side ^= 1
            # Peak kept off full so a kick reads as a swell, not a flash.
            self._level[self._side] = clamp(0.45 + 0.35 * music.hit("kick"))
            self._color_index += 1
        elif music.onset("snare"):
            # Snares light the *other* half at reduced level, which fills in a
            # backbeat without competing with the kick.
            other = self._side ^ 1
            self._level[other] = max(self._level[other], 0.25 * music.hit("snare") + 0.15)

        step = self.colour_step(music, BARS_PER_COLOUR, KICKS_PER_COLOUR, self._color_index)
        out = {}
        pars = self.pars
        for i, f in enumerate(pars):
            half = i % 2
            # Colour moves every few bars rather than every other kick, which
            # read as frantic.
            color = palette.at(step + half)
            out[f.fid] = Emission(
                rgb=color,
                intensity=clamp(FLOOR + (1.0 - FLOOR) * self._level[half]),
            )

        for f in self.accents:
            # Accent carries the sustained energy rather than the transients, so
            # there is something continuous underneath the flashing.
            out[f.fid] = Emission(
                rgb=palette.at(step + 1),
                intensity=clamp(0.12 + 0.5 * music.smooth("bass")),
                uv=0.2 * music.smooth("air"),
            )
        return out
