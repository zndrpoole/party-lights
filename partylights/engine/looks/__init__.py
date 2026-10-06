"""Built-in looks.

Adding a look is one file plus one entry in LOOKS. Nothing else in the engine
needs to know about it. Scenes built from layers live in scenes.py and are
appended after the hand-written looks.
"""

from .ambient import AmbientLook
from .base import Look
from .chase import ChaseLook
from .hush import HushLook
from .mirror import MirrorLook
from .pulse import PulseLook
from .scenes import SCENES
from .sparkle import SparkleLook
from .strobe import BlinderLook, StrobeLook
from .swell import SwellLook
from .unison import UnisonLook
from .uv_accent import UvAccentLook
from .wash import WashLook

#: Every available look, in the order the UI and the "next look" cue walk them.
LOOKS: tuple[type[Look], ...] = (
    AmbientLook,
    WashLook,
    SwellLook,
    PulseLook,
    UnisonLook,
    MirrorLook,
    ChaseLook,
    SparkleLook,
    UvAccentLook,
    HushLook,
    StrobeLook,
    BlinderLook,
) + SCENES

BY_NAME = {cls.name: cls for cls in LOOKS}


def build_all(patch, space=None, arc=None) -> dict[str, Look]:
    """Instantiate every look against a patch, sharing the engine's Space and
    song-arc director if given."""
    looks = {cls.name: cls(patch) for cls in LOOKS}
    for look in looks.values():
        if space is not None:
            look.space = space
        look.arc = arc
    return looks


def auto_selectable() -> list[str]:
    """Looks the automatic mode is allowed to choose.

    Excludes anything marked `manual_only` — currently the strobe and the
    blinder, which should only ever run because a human asked for them.
    """
    return [c.name for c in LOOKS if not getattr(c, "manual_only", False)]


__all__ = ["LOOKS", "BY_NAME", "Look", "build_all", "auto_selectable"]
