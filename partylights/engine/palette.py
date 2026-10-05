"""Colour palettes.

Looks do not choose colours directly. They ask the palette for "colour 0",
"colour 1" and so on, and the palette decides what those are. Changing the mood
of the whole rig is then one palette swap rather than an edit to every look.

Palettes are deliberately small -- three to five colours. A chase through twelve
fixtures each with its own hue reads as noise; three colours distributed across
twelve fixtures reads as design. This is the single biggest difference between
lighting that looks considered and lighting that looks like a screensaver.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..fixtures.color import Rgb, hsv, mix


@dataclass(frozen=True)
class Palette:
    name: str
    colors: tuple[Rgb, ...]
    #: Shown in the UI so the host knows what they are picking.
    description: str = ""

    def __len__(self) -> int:
        return len(self.colors)

    def at(self, index: int) -> Rgb:
        """Colour by index, wrapping. Looks use this for per-fixture colour."""
        return self.colors[index % len(self.colors)]

    def sample(self, position: float) -> Rgb:
        """Colour at a continuous position 0..1, interpolating between entries.

        For gradients across the rig and for smooth drifts over time. Wraps, so
        position 1.0 returns to the first colour and a drift never jumps.
        """
        n = len(self.colors)
        if n == 1:
            return self.colors[0]
        x = (position % 1.0) * n
        i = int(x)
        return mix(self.colors[i % n], self.colors[(i + 1) % n], x - i)


def _warm(h: float, s: float = 1.0, v: float = 1.0) -> Rgb:
    return hsv(h, s, v)


#: The built-in palettes. Hues are chosen to stay distinguishable on RGB-only
#: fixtures: anything relying on subtle desaturation looks muddy on the PARs,
#: which have no white channel of their own.
PALETTES: tuple[Palette, ...] = (
    Palette(
        "halloween",
        (hsv(0.08, 1.0, 1.0),    # orange
         hsv(0.78, 0.95, 1.0),   # violet
         hsv(0.28, 1.0, 0.8)),   # toxic green
        "Orange, violet and green",
    ),
    Palette(
        "halloween-deep",
        # Order matters to the swell look, which pairs colour i with colour
        # i + 2: every pairing is then one orange with one purple.
        (hsv(0.075, 1.0, 1.0),   # pumpkin orange
         hsv(0.035, 1.0, 1.0),   # ember
         hsv(0.77, 1.0, 1.0),    # deep purple
         hsv(0.81, 1.0, 1.0)),   # violet
        "Orange, ember, purple, violet — Halloween without the green",
    ),
    Palette(
        "warm",
        (hsv(0.02, 0.95, 1.0),   # red
         hsv(0.08, 1.0, 1.0),    # amber
         hsv(0.13, 0.85, 1.0)),  # gold
        "Reds through gold — flattering, low drama",
    ),
    Palette(
        "cool",
        (hsv(0.55, 1.0, 1.0),    # cyan
         hsv(0.66, 1.0, 1.0),    # blue
         hsv(0.78, 0.9, 1.0)),   # indigo
        "Cyan through indigo",
    ),
    Palette(
        "neon",
        (hsv(0.88, 1.0, 1.0),    # magenta
         hsv(0.50, 1.0, 1.0),    # cyan
         hsv(0.16, 1.0, 1.0),    # yellow
         hsv(0.33, 1.0, 1.0)),   # green
        "High-saturation magenta, cyan, yellow, green",
    ),
    Palette(
        "sunset",
        (hsv(0.00, 1.0, 1.0),
         hsv(0.06, 1.0, 1.0),
         hsv(0.11, 0.9, 1.0),
         hsv(0.92, 0.8, 1.0)),
        "Red, orange, gold, pink",
    ),
    Palette(
        "mono-white",
        (hsv(0.0, 0.0, 1.0),),
        "Plain white — for finding your keys",
    ),
)

BY_NAME = {p.name: p for p in PALETTES}
DEFAULT = "halloween"


def get(name: str) -> Palette:
    """Palette by name, falling back to the default rather than raising.

    A typo in a config file at a party should not stop the lights working.
    """
    return BY_NAME.get(name, BY_NAME[DEFAULT])


def names() -> list[str]:
    return [p.name for p in PALETTES]
