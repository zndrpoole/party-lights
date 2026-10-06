"""The Look interface.

A look is a pure-ish function from the state of the music to what each fixture
should be doing. It owns no DMX, no fixtures and no timing: it receives a
MusicState and returns an Emission per fixture id. The engine handles blending,
the manual override layer, the master dimmer and the actual output.

Keeping looks this narrow is what makes them cheap to write and safe to add
mid-party: a look cannot break the output path, because it cannot reach it.

Looks may hold their own animation state (a chase position, a decaying flash)
and should reset it in `reset()`.
"""

from __future__ import annotations

from ...audio.analyser import MusicState
from ...fixtures.color import BLACK, Emission
from ...fixtures.patch import Patch
from ..palette import Palette
from ..space import Space


class Look:
    #: Identifier used in config, the cue API and the UI.
    name = "base"
    #: One line shown in the UI.
    description = ""
    #: Whether this look needs a locked tempo to make sense. The engine avoids
    #: selecting these in auto mode until the tempo tracker has locked.
    needs_tempo = False
    #: Seconds for brightness to fall in the engine's output smoothing, or None
    #: for the global engine.release_ms. Crisp looks set it short so their hits
    #: snap; drifting looks set it long. The contrast between the two is what
    #: stops every look reading as the same slow wave.
    release_s: float | None = None
    #: Set by the engine every tick from the softness control: multiply any
    #: decay or envelope time by this. 1.0 is the tuning as written; above
    #: is smoother, below is sharper. Motion speeds are deliberately exempt.
    time_scale: float = 1.0
    #: The engine's song-arc director (engine/arc.py), shared by every look,
    #: or None outside an engine. Looks may read its phrase clock and mods.
    arc = None

    def __init__(self, patch: Patch):
        self.patch = patch
        #: Where the fixtures are. The engine replaces this with its own shared
        #: Space, so a layout change from /viz reaches every look at once.
        self.space = Space(patch)

    def reset(self) -> None:
        """Clear animation state. Called when this look becomes active."""

    def render(self, music: MusicState, palette: Palette, dt: float) -> dict[str, Emission]:
        """What every fixture should do right now.

        Returns a mapping of fixture id to Emission. Fixtures omitted from the
        result are left black, so a look only has to describe what it drives.
        """
        raise NotImplementedError

    # -- helpers for subclasses ------------------------------------------

    @staticmethod
    def colour_step(music, bars: int, kicks: int, kick_count: int) -> int:
        """Which palette step to show: one per `bars` bars when the tempo is
        locked, else one per `kicks` detected kicks.

        Bars are preferred because the kick detector also fires on other low
        hits; on the real rig, counting kicks changed colour several times
        faster than intended.
        """
        if music.tempo_locked:
            return (music.beat_index // 4) // bars
        return kick_count // kicks

    def all_black(self) -> dict[str, Emission]:
        return {f.fid: BLACK for f in self.patch}

    @property
    def pars(self):
        """The RGB wash fixtures, in physical order."""
        return self.patch.group("pars") or self.patch.ordered()

    @property
    def accents(self):
        """Fixtures with extra emitters (white/amber/UV). May be empty."""
        return self.patch.group("accent")
