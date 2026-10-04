"""Strobe: hard full-field flashing. Manual use only.

Deliberately NOT selectable by the auto mode. Rapid full-field flashing between
roughly 5 and 30 Hz is the photosensitive-seizure risk band, and that is not a
thing to hand to an automatic mode at a party where nobody has consented to it
and the host is not watching. It is available as a manual cue, and the engine
caps how long it can run (see EngineState.max_strobe_seconds) so a stuck button
or a forgotten cue cannot leave it going.

The cap is a safety rail, not a style choice.
"""

from __future__ import annotations

from ...fixtures.color import Emission
from .base import Look


class StrobeLook(Look):
    name = "strobe"
    description = "Hard white strobe — manual only, time-limited"
    #: Excluded from automatic selection. See the module docstring.
    manual_only = True

    def render(self, music, palette, dt):
        # The fixtures' own strobe channel does the flashing. Driving it from the
        # fixture is far cleaner than toggling intensity from our 40 Hz frame
        # loop, which would alias into visible irregularity.
        out = {}
        for f in self.patch:
            out[f.fid] = Emission(rgb=(1.0, 1.0, 1.0), intensity=1.0, strobe=0.55)
        return out


class BlinderLook(Look):
    name = "blinder"
    description = "Everything full white — for finding your keys"
    manual_only = True

    def render(self, music, palette, dt):
        return {f.fid: Emission(rgb=(1.0, 1.0, 1.0), intensity=1.0) for f in self.patch}
