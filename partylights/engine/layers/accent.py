"""What the RGBWA+UV accent does inside a layered scene.

It is the only fixture with real white and UV, so it plays a different
instrument from the PARs rather than being a thirteenth of them: a steady bed
in a partner colour, UV riding the bass, and optionally a white punch on a
part of the kit the PARs are not following -- the snare, usually, when the
PARs are on the kick.
"""

from __future__ import annotations

from ...fixtures.color import Emission, clamp, mix
from .base import Ctx

WHITE = (1.0, 1.0, 1.0)


class Accent:
    def __init__(self, offset: int = 1, bed: float = 0.25, uv: float = 0.15,
                 uv_bass: float = 0.25, punch: str | None = None, punch_decay_s: float = 0.3,
                 bars: float = 8):
        """
        offset         palette steps away from the PARs' colour for the bed
        bed            steady brightness
        uv, uv_bass    UV level, and how much the bass adds on top
        punch          onset region that fires a white hit, or None
        bars           how often the bed colour steps, matching the PARs
        """
        self.offset, self.bed, self.uv, self.uv_bass = offset, bed, uv, uv_bass
        self.punch, self.punch_decay_s, self.bars = punch, punch_decay_s, bars
        self._env = 0.0

    def render(self, ctx: Ctx, fids: list[str]) -> dict[str, Emission]:
        m = ctx.music
        self._env *= ctx.decay(self.punch_decay_s)
        if self.punch and m.onset(self.punch):
            self._env = clamp(0.75 + 0.25 * m.hit(self.punch))
        colour = ctx.palette.at(ctx.step(self.bars) + self.offset)
        e = self._env
        em = Emission(
            rgb=mix(colour, WHITE, e),
            intensity=clamp(max(self.bed + 0.1 * m.smooth("bass"), e)),
            uv=clamp(self.uv + self.uv_bass * m.smooth("bass")),
            # Saturated bed, real white emitter on the punch.
            emitter_bias=0.3 + 0.7 * e,
        )
        return {fid: em for fid in fids}
