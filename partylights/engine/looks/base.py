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


class Look:
    #: Identifier used in config, the cue API and the UI.
    name = "base"
    #: One line shown in the UI.
    description = ""
    #: Whether this look needs a locked tempo to make sense. The engine avoids
    #: selecting these in auto mode until the tempo tracker has locked.
    needs_tempo = False

    def __init__(self, patch: Patch):
        self.patch = patch

    def reset(self) -> None:
        """Clear animation state. Called when this look becomes active."""

    def render(self, music: MusicState, palette: Palette, dt: float) -> dict[str, Emission]:
        """What every fixture should do right now.

        Returns a mapping of fixture id to Emission. Fixtures omitted from the
        result are left black, so a look only has to describe what it drives.
        """
        raise NotImplementedError

    # -- helpers for subclasses ------------------------------------------

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
