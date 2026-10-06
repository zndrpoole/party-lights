"""Unison: the whole room in one colour, hitting together.

The simplest picture in lighting, and the one the rig was missing: every other
look moves light *across* the room. Here nothing travels. All PARs share one
colour and one brightness, bump together on the kick -- hardest on the
downbeat -- and hold steady in between. Against the moving looks it reads as
weight; after it, they read as movement again.
"""

from __future__ import annotations

import math

from ...fixtures.color import Emission, clamp
from .base import Look

#: Steady level between hits, so the room holds its colour rather than
#: dropping out.
HOLD = 0.25
#: Peak on a downbeat kick, and on any other kick.
DOWNBEAT_PEAK = 1.0
KICK_PEAK = 0.75
#: Seconds for a hit to settle back to HOLD.
DECAY_S = 0.5
#: Bars per colour when the tempo is locked; kicks per colour when it is not.
BARS_PER_COLOUR = 8
KICKS_PER_COLOUR = 32


class UnisonLook(Look):
    name = "unison"
    description = "Whole room in one colour, hitting together on the downbeat"
    #: Output release. Short: the hit should snap, then the look's own decay rolls it off.
    release_s = 0.12

    def __init__(self, patch):
        super().__init__(patch)
        self.reset()

    def reset(self) -> None:
        self._env = 0.0
        self._peak = 0.0
        self._kicks = 0

    def render(self, music, palette, dt):
        self._env *= math.exp(-dt / (DECAY_S * self.time_scale))
        if music.onset("kick"):
            self._kicks += 1
            downbeat = music.tempo_locked and music.beat_index % 4 == 0 and (
                music.beat_phase < 0.25 or music.beat_phase > 0.85)
            self._peak = DOWNBEAT_PEAK if downbeat else KICK_PEAK
            self._env = 1.0

        hold = HOLD + 0.10 * music.smooth("bass")
        level = clamp(hold + max(0.0, self._peak - hold) * self._env)

        step = self.colour_step(music, BARS_PER_COLOUR, KICKS_PER_COLOUR, self._kicks)
        color = palette.at(step)

        out = {f.fid: Emission(rgb=color, intensity=level) for f in self.pars}
        for f in self.accents:
            # The accent holds the next colour steadily: a counterweight, not a
            # thirteenth PAR hitting along.
            out[f.fid] = Emission(rgb=palette.at(step + 1), intensity=clamp(hold),
                                  uv=0.15, emitter_bias=0.3)
        return out
