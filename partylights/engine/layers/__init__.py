"""Layers: the parts scenes are built from.

    colour fields   Solid, Gradient, Split, Noise
    masks           Travel, Ripple, Alternate, Pulse, SidePulse, Knockout,
                    Sparkle, Breathe
    accent          Accent

Layers know nothing about looks; looks/layered.py assembles them into one,
and looks/scenes.py is the catalogue of assembled scenes.
"""

from .accent import Accent
from .colour import Gradient, Noise, Solid, Split
from .masks import (Alternate, Breathe, Knockout, Pulse, Ripple, SidePulse, Sparkle,
                    Travel)

__all__ = [
    "Accent", "Gradient", "Noise", "Solid", "Split",
    "Alternate", "Breathe", "Knockout", "Pulse", "Ripple", "SidePulse", "Sparkle", "Travel",
]
