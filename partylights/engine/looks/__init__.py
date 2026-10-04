"""Built-in looks.

Adding a look is one file plus one entry in LOOKS. Nothing else in the engine
needs to know about it.
"""

from .ambient import AmbientLook
from .base import Look
from .chase import ChaseLook
from .pulse import PulseLook
from .sparkle import SparkleLook
from .strobe import BlinderLook, StrobeLook
from .uv_accent import UvAccentLook
from .wash import WashLook

#: Every available look, in the order the UI and the "next look" cue walk them.
LOOKS: tuple[type[Look], ...] = (
    AmbientLook,
    WashLook,
    PulseLook,
    ChaseLook,
    SparkleLook,
    UvAccentLook,
    StrobeLook,
    BlinderLook,
)

BY_NAME = {cls.name: cls for cls in LOOKS}


def build_all(patch) -> dict[str, Look]:
    """Instantiate every look against a patch."""
    return {cls.name: cls(patch) for cls in LOOKS}


def auto_selectable() -> list[str]:
    """Looks the automatic mode is allowed to choose.

    Excludes anything marked `manual_only` — currently the strobe and the
    blinder, which should only ever run because a human asked for them.
    """
    return [c.name for c in LOOKS if not getattr(c, "manual_only", False)]


__all__ = ["LOOKS", "BY_NAME", "Look", "build_all", "auto_selectable"]
