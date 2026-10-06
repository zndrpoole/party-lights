"""Colour fields: which colour each fixture shows.

Colour changes land on bar boundaries, never mid-bar, so a change reads as
part of the music. Outdoors the eye judges colour against the neighbouring
patch of wall or tree, which is why most of these place two palette colours
side by side rather than one colour everywhere.
"""

from __future__ import annotations

import math

from ...fixtures.color import Rgb
from .base import Ctx, coord


class Solid:
    """The whole rig in one palette colour, stepping every `bars` bars."""

    def __init__(self, bars: float = 8, offset: int = 0):
        self.bars, self.offset = bars, offset

    def render(self, ctx: Ctx, fids: list[str]) -> dict[str, Rgb]:
        c = ctx.palette.at(ctx.step(self.bars) + self.offset)
        return {fid: c for fid in fids}


class Gradient:
    """The palette laid across the room along one axis, drifting over time.

    `spread` is how much of the palette spans the rig: 0.5 puts two colours
    end to end. `blend=False` keeps solid palette colours with hard edges,
    which reads better on RGB-only PARs than the muddy in-between mixes.
    """

    def __init__(self, axis: str = "along", degrees: float = 0.0, spread: float = 0.5,
                 drift_bars: float = 16, blend: bool = True):
        self.axis, self.degrees, self.spread = axis, degrees, spread
        self.drift_bars, self.blend = drift_bars, blend

    def render(self, ctx: Ctx, fids: list[str]) -> dict[str, Rgb]:
        drift = ctx.beats / (4.0 * self.drift_bars) if self.drift_bars else 0.0
        n = len(ctx.palette)
        out = {}
        for fid in fids:
            pos = coord(ctx.space, fid, self.axis, self.degrees) * self.spread + drift
            if self.blend:
                out[fid] = ctx.palette.sample(pos)
            else:
                out[fid] = ctx.palette.at(int(math.floor(pos * n)))
        return out


class Split:
    """Two halves in partner colours: half the palette apart, which in the
    Halloween palettes is always an orange against a purple.

    `axis="side"` splits left from right of centre; any other axis splits at
    its midpoint. `swap_bars` trades the colours between halves.
    """

    def __init__(self, axis: str = "side", degrees: float = 0.0, bars: float = 8,
                 swap_bars: float = 0):
        self.axis, self.degrees, self.bars, self.swap_bars = axis, degrees, bars, swap_bars

    def render(self, ctx: Ctx, fids: list[str]) -> dict[str, Rgb]:
        step = ctx.step(self.bars)
        swap = ctx.step(self.swap_bars) % 2 if self.swap_bars else 0
        half = max(1, len(ctx.palette) // 2)
        out = {}
        for fid in fids:
            side = 1 if coord(ctx.space, fid, self.axis, self.degrees) >= 0.5 else 0
            out[fid] = ctx.palette.at(step + ((side + swap) % 2) * half)
        return out


class Noise:
    """Slowly drifting colour texture across the room.

    Value noise over the stage position and time, mapped into the palette.
    Neighbouring fixtures are related but never identical, and the pattern
    never repeats, which keeps a quiet stretch from looking like a loop.
    """

    def __init__(self, scale: float = 2.2, speed: float = 0.06, spread: float = 1.4):
        self.scale, self.speed, self.spread = scale, speed, spread

    def render(self, ctx: Ctx, fids: list[str]) -> dict[str, Rgb]:
        z = ctx.t * self.speed
        out = {}
        for fid in fids:
            s = ctx.space.spot(fid)
            v = noise3(s.x * self.scale, s.y * self.scale, z)
            out[fid] = ctx.palette.sample(v * self.spread)
        return out


# -- value noise ---------------------------------------------------------------

def _hash(i: int, j: int, k: int) -> float:
    n = (i * 73856093) ^ (j * 19349663) ^ (k * 83492791)
    n = (n << 13) ^ n
    return ((n * (n * n * 15731 + 789221) + 1376312589) & 0x7FFFFFFF) / 0x7FFFFFFF


def _fade(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def noise3(x: float, y: float, z: float) -> float:
    """Smooth pseudo-random field, 0..1, continuous in all three axes."""
    xi, yi, zi = math.floor(x), math.floor(y), math.floor(z)
    xf, yf, zf = _fade(x - xi), _fade(y - yi), _fade(z - zi)

    def lerp(a, b, t):
        return a + (b - a) * t

    def corner(dx, dy, dz):
        return _hash(xi + dx, yi + dy, zi + dz)

    x00 = lerp(corner(0, 0, 0), corner(1, 0, 0), xf)
    x10 = lerp(corner(0, 1, 0), corner(1, 1, 0), xf)
    x01 = lerp(corner(0, 0, 1), corner(1, 0, 1), xf)
    x11 = lerp(corner(0, 1, 1), corner(1, 1, 1), xf)
    return lerp(lerp(x00, x10, yf), lerp(x01, x11, yf), zf)
