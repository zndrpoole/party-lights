"""Where the fixtures are, for looks that move light across the room.

Until this existed the engine knew only patch order, so every look was either
"the whole room" or "along the line of PARs". That is fine indoors on one wall,
but outside the PARs might uplight a wall and a tree line facing each other, or
run in a U around the lawn, and a sweep that ignores that reads as noise.

Positions come from the stage view (/viz), which saves them to
config/layout.local.json as 0..1 fractions of the stage, y growing downwards.
Drag a fixture there and every spatial look follows on the next tick. With no
layout saved, the PARs sit on the same shallow arc the stage view draws by
default, so the two always agree.

Every coordinate a look reads is normalised over the PARs, the fixtures that
carry the movement, so a sweep crosses the whole rig in one pass however
spread out or bunched up it is. The accent is placed in the same frame but
usually sits outside the PARs' range; its values clamp rather than wrap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..fixtures.patch import Patch


@dataclass(frozen=True)
class Spot:
    """One fixture's place in the room, precomputed for the current layout."""

    x: float
    y: float
    #: 0..1 along the run in patch order -- the path a chase follows. Unlike
    #: x, this goes around a U rather than across it.
    path: float
    #: 0..1 turns around the PARs' centroid, from the right going clockwise on
    #: screen. What a rotor spins through.
    angle: float
    #: 0..1 from the centroid to the PAR furthest from it.
    radius: float
    #: 0 left of the centroid, 1 right of it.
    side: int


def default_layout(patch: Patch) -> dict[str, tuple[float, float]]:
    """The arrangement /viz draws with nothing saved. Keep in step with
    defaultLayout() in web/static/viz.js."""
    pars = [f for f in patch.ordered() if "pars" in f.groups]
    rest = [f for f in patch.ordered() if "pars" not in f.groups]
    out = {}
    for i, f in enumerate(pars):
        t = i / (len(pars) - 1) if len(pars) > 1 else 0.5
        out[f.fid] = (0.07 + 0.86 * t, 0.78 - 0.32 * math.sin(math.pi * t))
    for i, f in enumerate(rest):
        out[f.fid] = ((i + 1) / (len(rest) + 1), 0.2)
    return out


class Space:
    def __init__(self, patch: Patch, layout: dict | None = None):
        self.patch = patch
        self._spots: dict[str, Spot] = {}
        self._movers: list[str] = []
        self._centre = (0.5, 0.5)
        self._reach = 1.0
        self._along: dict[float, dict[str, float]] = {}
        self.set_layout(layout or {})

    # -- layout -----------------------------------------------------------

    def set_layout(self, layout: dict) -> None:
        """Adopt positions saved by /viz: {fid: {"x": .., "y": ..}}.

        Fixtures missing from it keep their default spot. Called from the web
        thread while the engine renders, so the new table is built aside and
        swapped in with one assignment.
        """
        points = default_layout(self.patch)
        for fid, pos in (layout or {}).items():
            if fid in points:
                try:
                    points[fid] = (float(pos["x"]), float(pos["y"]))
                except (TypeError, KeyError, ValueError):
                    pass

        movers = [f.fid for f in (self.patch.group("pars") or self.patch.ordered())]
        mpts = [points[fid] for fid in movers] or [(0.5, 0.5)]
        cx = sum(p[0] for p in mpts) / len(mpts)
        cy = sum(p[1] for p in mpts) / len(mpts)
        reach = max((math.hypot(x - cx, y - cy) for x, y in mpts), default=0.0) or 1.0
        rank = {fid: i for i, fid in enumerate(movers)}
        n = len(movers)

        spots = {}
        for f in self.patch:
            x, y = points[f.fid]
            i = rank.get(f.fid)
            path = (i / (n - 1) if n > 1 else 0.5) if i is not None else 0.5
            spots[f.fid] = Spot(
                x=x, y=y, path=path,
                angle=(math.atan2(y - cy, x - cx) / (2.0 * math.pi)) % 1.0,
                radius=min(1.0, math.hypot(x - cx, y - cy) / reach),
                side=0 if x < cx else 1,
            )
        self._movers = movers
        self._centre = (cx, cy)
        self._reach = reach
        self._spots = spots
        # Cleared last: a projection cached between these lines was computed
        # from the new spots, and one cached before is at worst a frame stale.
        self._along = {}

    # -- queries ----------------------------------------------------------

    @property
    def centre(self) -> tuple[float, float]:
        """The PARs' centroid, in stage coordinates."""
        return self._centre

    def spot(self, fid: str) -> Spot:
        return self._spots[fid]

    def along(self, fid: str, degrees: float = 0.0) -> float:
        """0..1 position along a direction across the PARs.

        0 degrees runs left to right, 90 top to bottom of the stage view. The
        range is the PARs' own extent in that direction, so the first and last
        PAR always sit at 0 and 1.
        """
        table = self._along.get(degrees)
        if table is None:
            table = self._project(degrees)
            self._along[degrees] = table
        return table.get(fid, 0.5)

    def _project(self, degrees: float) -> dict[str, float]:
        dx, dy = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
        spots = self._spots
        proj = {fid: s.x * dx + s.y * dy for fid, s in spots.items()}
        movers = [proj[m] for m in self._movers]
        lo, hi = (min(movers), max(movers)) if movers else (0.0, 0.0)
        if hi - lo < 1e-6:
            return {fid: 0.5 for fid in spots}
        return {fid: min(1.0, max(0.0, (p - lo) / (hi - lo))) for fid, p in proj.items()}

    def dist(self, fid: str, x: float, y: float) -> float:
        """Distance from a stage point, in units of the rig's radius."""
        s = self._spots[fid]
        return math.hypot(s.x - x, s.y - y) / self._reach
