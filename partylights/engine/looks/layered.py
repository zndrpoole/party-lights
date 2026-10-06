"""A look assembled from layers: colour field x masks, plus an accent role.

    brightness = floor + (peak - floor) * combined masks

A scene subclasses LayeredLook and returns its layers from `build()`. Because
the result is an ordinary Look, the engine's crossfades, smoothing, drop hit,
manual overrides and cue API all apply unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...fixtures.color import Emission, clamp
from ..layers.accent import Accent
from ..layers.base import FREE_BPM, Ctx, beat_clock
from ..layers.masks import Mask, combine
from .base import Look


@dataclass
class Stack:
    colour: object
    masks: list[Mask] = field(default_factory=list)
    accent: Accent | None = None


class LayeredLook(Look):
    #: Brightness with every mask at 0, and at 1.
    floor = 0.04
    peak = 0.95

    def __init__(self, patch):
        super().__init__(patch)
        self.reset()

    def build(self) -> Stack:
        raise NotImplementedError

    def reset(self) -> None:
        # Fresh layers are the reset: no layer has to clear its own state.
        self._stack = self.build()
        self._t = 0.0

    @property
    def layer_names(self) -> list[str]:
        """For the stage view: what this scene is made of."""
        s = self._stack
        names = [type(s.colour).__name__] + [f"{type(m).__name__}({m.mode})" for m in s.masks]
        if s.accent:
            names.append("Accent" + (f"[{s.accent.punch}]" if s.accent.punch else ""))
        return names

    def render(self, music, palette, dt):
        self._t += dt
        arc = self.arc
        locked = music.tempo_locked
        # With a lock, count beats from the arc's phrase anchor, so patterns
        # start on the track's bar one once a drop has shown where that is.
        beats = arc.beats(music) if arc and locked else beat_clock(music, self._t)
        ctx = Ctx(music=music, palette=palette, dt=dt, space=self.space,
                  time_scale=self.time_scale, t=self._t, beats=beats,
                  rate=arc.mods.rate if arc else 1,
                  bps=(music.bpm if locked and music.bpm > 0 else FREE_BPM) / 60.0)
        stack = self._stack
        fids = [f.fid for f in self.pars]

        colours = stack.colour.render(ctx, fids)
        level = None
        for mask in stack.masks:
            m = mask.render(ctx, fids)
            if level is None:
                level = m
            else:
                level = {fid: combine(level[fid], m[fid], mask.mode) for fid in fids}
        if level is None:
            level = {fid: 1.0 for fid in fids}

        span = self.peak - self.floor
        out = {fid: Emission(rgb=colours[fid], intensity=clamp(self.floor + span * level[fid]))
               for fid in fids}
        accents = [f.fid for f in self.accents if f.fid not in out]
        if stack.accent and accents:
            out.update(stack.accent.render(ctx, accents))
        return out
