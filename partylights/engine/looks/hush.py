"""Hush: near-darkness for a breakdown. The PARs go out; only UV remains.

Darkness is the strongest contrast a rig has, and until this look the auto mode
never used it. Auto mode picks it when the analyser hears a breakdown, so the
room drops away with the music and the next look lands on a room whose eyes
have adjusted. The accent's UV keeps it from reading as a fault: white
clothing and teeth glow, which on Halloween is the point.
"""

from __future__ import annotations

from ...fixtures.color import Emission, clamp
from .base import Look


class HushLook(Look):
    name = "hush"
    wildness = 0.1
    description = "PARs out, blacklight only — for breakdowns"
    #: Output release. Long, so the room sinks into the dark rather than cutting.
    release_s = 1.2

    def render(self, music, palette, dt):
        out = {f.fid: Emission(rgb=palette.at(0), intensity=0.0) for f in self.pars}
        for f in self.accents:
            out[f.fid] = Emission(rgb=(0.0, 0.0, 0.0), intensity=1.0,
                                  uv=clamp(0.7 + 0.3 * music.smooth("bass")))
        return out
