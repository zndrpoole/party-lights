"""Masks: how bright each fixture is, 0..1.

A scene stacks masks. The first sets the level; each later one combines with
it by its `mode`:

    mul   multiply  -- e.g. a travelling head that also punches on the kick
    max   brightest -- e.g. ripples drawn over a steady bed
    min   darkest   -- e.g. a knockout cutting whatever is lit
    add   sum, capped at 1

Movement is computed from the continuous beat clock, never from onset events,
so a head arrives on the beat rather than a frame after it. Decays scale with
the softness control; motion speeds do not.
"""

from __future__ import annotations

import math
import random

from .base import Ctx, coord


class Mask:
    mode = "mul"

    def __init__(self, mode: str | None = None):
        if mode is not None:
            self.mode = mode

    def render(self, ctx: Ctx, fids: list[str]) -> dict[str, float]:
        raise NotImplementedError


def combine(a: float, b: float, mode: str) -> float:
    if mode == "mul":
        return a * b
    if mode == "max":
        return a if a > b else b
    if mode == "min":
        return a if a < b else b
    if mode == "add":
        return min(1.0, a + b)
    raise ValueError(f"unknown mask mode {mode!r}")


def _bump(d: float, width: float) -> float:
    return math.exp(-(d * d) / (2.0 * width * width)) if width > 0 else float(d == 0)


class Travel(Mask):
    """Heads of light moving along an axis, locked to the beat clock.

    One class covers the classic moves:
        chase   axis="path",  pingpong=True   -- along the run and back
        sweep   axis="along", pingpong=True   -- a wave front across the room
        rotor   axis="angle", wrap=True       -- spinning round the centre

    `beats` is one full cycle (there and back for a ping-pong). `heads` spaces
    several heads evenly round a wrapping axis. A kick widens the heads, which
    ties the motion to the music without disturbing its timing.
    """

    def __init__(self, axis: str = "path", degrees: float = 0.0, beats: float = 8,
                 width: float = 0.12, pingpong: bool = True, wrap: bool = False,
                 heads: int = 1, punch: float = 0.4, mode: str | None = None):
        super().__init__(mode)
        self.axis, self.degrees, self.beats = axis, degrees, beats
        self.width, self.pingpong, self.wrap = width, pingpong, wrap
        self.heads, self.punch = max(1, heads), punch

    def render(self, ctx, fids):
        x = ctx.cycle(self.beats)
        if self.pingpong:
            x = 2.0 * x if x < 0.5 else 2.0 * (1.0 - x)
        width = self.width * (1.0 + self.punch * ctx.music.hit("kick"))
        out = {}
        for fid in fids:
            p = coord(ctx.space, fid, self.axis, self.degrees)
            level = 0.0
            for h in range(self.heads):
                head = (x + h / self.heads) % 1.0 if self.heads > 1 else x
                d = abs(p - head)
                if self.wrap:
                    d = min(d, 1.0 - d)
                level = max(level, _bump(d, width))
            out[fid] = level
        return out


class Ripple(Mask):
    """Rings of light from a point, one per hit.

    Successive hits alternate outward and inward when `alternate` is set, the
    way the mirror look bursts and closes. `origin` is a stage point, or None
    for the centre of the PARs.
    """

    mode = "max"

    def __init__(self, origin: tuple[float, float] | None = None, region: str = "kick",
                 travel_s: float = 0.5, width: float = 0.16, alternate: bool = True,
                 mode: str | None = None):
        super().__init__(mode)
        self.origin, self.region = origin, region
        self.travel_s, self.width, self.alternate = travel_s, width, alternate
        self._rings: list[list] = []     # [age_s, outward, strength]
        self._hits = 0

    def render(self, ctx, fids):
        for ring in self._rings:
            ring[0] += ctx.dt
        self._rings = [r for r in self._rings if r[0] < self.travel_s * 1.6]
        if ctx.music.onset(self.region):
            outward = not self.alternate or self._hits % 2 == 0
            self._hits += 1
            self._rings.append([0.0, outward, min(1.0, 0.7 + 0.3 * ctx.music.hit(self.region))])

        ox, oy = self.origin or ctx.space.centre
        dist = {fid: ctx.space.dist(fid, ox, oy) for fid in fids}
        far = max(dist.values(), default=1.0) or 1.0
        out = {}
        for fid in fids:
            d = dist[fid] / far
            level = 0.0
            for age, outward, strength in self._rings:
                x = age / self.travel_s
                r = x if outward else 1.0 - x
                fade = math.exp(-max(0.0, x - 1.0) * 4.0)
                level = max(level, strength * fade * _bump(d - r, self.width))
            out[fid] = level
        return out


class Alternate(Mask):
    """Groups taking turns on the beat clock: halves of the room trading
    every `beats` beats, or a theatre chase stepping through `groups` groups.

    `axis="side"` with groups=2 is ping-pong between the two halves -- with
    PARs on a wall and a tree line facing each other, call and response
    across the lawn. `interleave` (on path) gives every third fixture lit,
    stepping: the classic marquee.
    """

    def __init__(self, axis: str = "side", groups: int = 2, beats: float = 2,
                 low: float = 0.08, interleave: bool = False, mode: str | None = None):
        super().__init__(mode)
        self.axis, self.groups, self.beats = axis, max(2, groups), beats
        self.low, self.interleave = low, interleave

    def _group(self, ctx, fid, n):
        p = coord(ctx.space, fid, self.axis)
        if self.interleave:
            return int(round(p * (n - 1))) % self.groups
        return min(self.groups - 1, int(p * self.groups))

    def render(self, ctx, fids):
        lit = ctx.count(self.beats) % self.groups
        n = len(fids)
        return {fid: 1.0 if self._group(ctx, fid, n) == lit else self.low for fid in fids}


class Pulse(Mask):
    """Every fixture jumps on a hit and settles back to `floor`.

    `downbeat` adds the extra weight a bar's first kick deserves with a lock.
    """

    def __init__(self, region: str = "kick", decay_s: float = 0.4, floor: float = 0.3,
                 downbeat: float = 0.0, mode: str | None = None):
        super().__init__(mode)
        self.region, self.decay_s, self.floor, self.downbeat = region, decay_s, floor, downbeat
        self._env = 0.0
        self._peak = 1.0

    def render(self, ctx, fids):
        self._env *= ctx.decay(self.decay_s)
        if ctx.music.onset(self.region):
            m = ctx.music
            on_one = m.tempo_locked and m.beat_index % 4 == 0 and (
                m.beat_phase < 0.25 or m.beat_phase > 0.85)
            self._peak = 1.0 if on_one or not self.downbeat else 1.0 - self.downbeat
            self._env = 1.0
        level = self.floor + (self._peak - self.floor) * self._env
        return {fid: level for fid in fids}


class SidePulse(Mask):
    """Each half of the room answers a different part of the kit.

    Default: the left half on the kick, the right on the snare. On a
    backbeat that is a conversation across the room.
    """

    def __init__(self, regions: tuple[str, str] = ("kick", "snare"), axis: str = "side",
                 decay_s: float = 0.4, floor: float = 0.15, mode: str | None = None):
        super().__init__(mode)
        self.regions, self.axis = regions, axis
        self.decay_s, self.floor = decay_s, floor
        self._env = [0.0, 0.0]

    def render(self, ctx, fids):
        k = ctx.decay(self.decay_s)
        for i, region in enumerate(self.regions):
            self._env[i] *= k
            if ctx.music.onset(region):
                self._env[i] = 1.0
        out = {}
        for fid in fids:
            side = 1 if coord(ctx.space, fid, self.axis) >= 0.5 else 0
            out[fid] = self.floor + (1.0 - self.floor) * self._env[side]
        return out


class Knockout(Mask):
    """The room stays lit and each hit briefly cuts it.

    Inverts the usual idea: darkness is the accent. Hard-edged, so it reads
    especially well through fog.
    """

    mode = "min"

    def __init__(self, region: str = "kick", depth: float = 0.85, decay_s: float = 0.18,
                 mode: str | None = None):
        super().__init__(mode)
        self.region, self.depth, self.decay_s = region, depth, decay_s
        self._env = 0.0

    def render(self, ctx, fids):
        self._env *= ctx.decay(self.decay_s)
        if ctx.music.onset(self.region):
            self._env = 1.0
        level = 1.0 - self.depth * self._env
        return {fid: level for fid in fids}


class Sparkle(Mask):
    """Random fixtures glint on hits -- texture rather than pattern."""

    mode = "max"

    def __init__(self, region: str = "hat", per_hit: int = 2, decay_s: float = 0.6,
                 mode: str | None = None):
        super().__init__(mode)
        self.region, self.per_hit, self.decay_s = region, per_hit, decay_s
        self._levels: dict[str, float] = {}
        self._rng = random.Random()

    def render(self, ctx, fids):
        k = ctx.decay(self.decay_s)
        self._levels = {f: v * k for f, v in self._levels.items() if v * k > 0.01}
        if ctx.music.onset(self.region) and fids:
            for fid in self._rng.sample(fids, min(self.per_hit, len(fids))):
                self._levels[fid] = min(1.0, 0.6 + 0.3 * ctx.music.hit(self.region))
        return {fid: self._levels.get(fid, 0.0) for fid in fids}


class Breathe(Mask):
    """A slow swell: a wave over `beats` beats riding an envelope of the
    music measured in seconds, so it follows the song but never a hit."""

    def __init__(self, beats: float = 16, low: float = 0.4, attack_s: float = 1.2,
                 release_s: float = 3.5, axis: str | None = "path",
                 mode: str | None = None):
        super().__init__(mode)
        self.beats, self.low = beats, low
        self.attack_s, self.release_s, self.axis = attack_s, release_s, axis
        self._swell = 0.0

    def render(self, ctx, fids):
        m = ctx.music
        drive = 0.0 if m.silent else min(1.0, 0.6 * max(m.smooth("bass"), m.smooth("sub"))
                                         + 0.4 * m.energy)
        tau = (self.attack_s if drive > self._swell else self.release_s) * ctx.time_scale
        self._swell += (drive - self._swell) * (1.0 - math.exp(-ctx.dt / tau))
        x = ctx.cycle(self.beats)
        out = {}
        for fid in fids:
            # Offsetting the wave by position makes it roll across the room
            # rather than the whole rig breathing as one.
            offset = coord(ctx.space, fid, self.axis) if self.axis else 0.0
            wave = 0.5 + 0.5 * math.sin(2.0 * math.pi * (x - offset))
            out[fid] = min(1.0, self.low + (1.0 - self.low) * (0.45 * wave + 0.55 * self._swell))
        return out
