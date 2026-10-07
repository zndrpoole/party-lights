"""Swell: full colours held for a long time, brightness rolling with the music.

For when the room wants mood rather than hits. Three things keep it smooth:

* Colour is held, not drifted. Neighbouring PARs share a solid palette colour,
  alternating between two colours along the run, and the pairing only rotates
  every HOLD_S (sooner on a section change, never inside MIN_HOLD_S). When it
  does, the change rolls across the room over a few seconds instead of
  snapping.
* Brightness follows a slow envelope of the music -- seconds, not frames -- so
  it swells into a chorus and settles in a verse, but no individual kick or
  snare shows.
* A slow wave travels along the run on top, so the room keeps moving even when
  the music is steady.

Brightness never drops below FLOOR - ROLL_DEPTH, so the colours stay full
rather than fading out.
"""

from __future__ import annotations

import math

from ...audio.structure import SECTION_CHANGE
from ...fixtures.color import Emission, clamp, mix
from .base import Look

#: Seconds a colour pairing holds before rotating on its own.
HOLD_S = 32.0
#: A section change may rotate early, but never sooner than this.
MIN_HOLD_S = 16.0
#: Seconds for one fixture to crossfade to its new colour.
XFADE_S = 4.0
#: Extra delay from the first fixture to the last, so a change rolls across.
STAGGER_S = 4.0
#: Neighbouring fixtures that share one colour. Blocks read as "full colour";
#: per-fixture alternation reads as busy.
BLOCK = 3
#: Envelope on the music, in seconds. Slow on purpose: this is the swell.
SWELL_ATTACK_S = 1.2
SWELL_RELEASE_S = 3.5
#: Brightness at silence, and how much the music adds on top.
FLOOR = 0.30
SWELL_RANGE = 0.45
#: The travelling wave: seconds to cross the run, and its depth either way.
ROLL_S = 11.0
ROLL_DEPTH = 0.14


def _ease(x: float) -> float:
    """Smoothstep, so a crossfade starts and lands gently."""
    x = clamp(x)
    return x * x * (3.0 - 2.0 * x)


class SwellLook(Look):
    name = "swell"
    wildness = 0.2
    description = "Full colours held for ages, slow swells and rolls — no hits"
    #: Output release. Short or long because it is the slowest look there is.
    release_s = 0.60

    def __init__(self, patch):
        super().__init__(patch)
        self.reset()

    def reset(self) -> None:
        self._t = 0.0
        self._swell = 0.0
        self._offset = 0
        self._prev_offset = 0
        self._changed_at = -1e9     # no crossfade in progress at start
        self._held_since = 0.0

    # -- colour ------------------------------------------------------------

    def _colour(self, palette, offset: int, block: int):
        """Blocks alternate between colour `offset` and its partner half the
        palette away, which in halloween-deep is always orange with purple."""
        n = len(palette)
        return palette.at(offset + (block % 2) * (n // 2))

    def _maybe_rotate(self, music) -> None:
        held = self._t - self._held_since
        section = SECTION_CHANGE in music.events
        if held >= HOLD_S or (section and held >= MIN_HOLD_S):
            self._prev_offset = self._offset
            self._offset += 1
            self._changed_at = self._t
            self._held_since = self._t

    # -- render ------------------------------------------------------------

    def render(self, music, palette, dt):
        self._t += dt
        self._maybe_rotate(music)

        # One slow envelope for the whole rig: low end mostly, some overall mix.
        drive = 0.0 if music.silent else clamp(
            0.6 * max(music.smooth("bass"), music.smooth("sub")) + 0.4 * music.energy)
        tau = (SWELL_ATTACK_S if drive > self._swell else SWELL_RELEASE_S) * self.time_scale
        self._swell += (drive - self._swell) * (1.0 - math.exp(-dt / tau))
        base = FLOOR + SWELL_RANGE * self._swell

        out = {}
        pars = self.pars
        n = max(1, len(pars))
        since = self._t - self._changed_at
        for i, f in enumerate(pars):
            block = i // BLOCK
            colour = self._colour(palette, self._offset, block)
            fade = _ease((since - STAGGER_S * i / n) / XFADE_S)
            if fade < 1.0:
                colour = mix(self._colour(palette, self._prev_offset, block), colour, fade)
            roll = ROLL_DEPTH * math.sin(2.0 * math.pi * (self._t / ROLL_S - i / n))
            out[f.fid] = Emission(rgb=colour, intensity=clamp(base + roll))

        # The accent takes the partner colour of the middle of the run and
        # keeps it saturated (little white extraction), with UV riding the
        # swell -- it is Halloween.
        mid_block = (n // 2) // BLOCK + 1
        fade = _ease((since - STAGGER_S * 0.5) / XFADE_S)
        accent = mix(self._colour(palette, self._prev_offset, mid_block),
                     self._colour(palette, self._offset, mid_block), fade)
        for f in self.accents:
            out[f.fid] = Emission(rgb=accent, intensity=clamp(base - 0.05),
                                  uv=0.25 + 0.35 * self._swell, emitter_bias=0.3)
        return out
