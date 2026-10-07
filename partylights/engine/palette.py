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
    #: Which THEMES group the UI files it under.
    theme: str = "misc"

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


#: Palette themes, in the order the UI lists them, with their display names.
#: Functional is for working light rather than mood; misc catches the rest.
THEMES: tuple[tuple[str, str], ...] = (
    ("halloween", "Halloween"),
    ("party", "Party"),
    ("club", "Club"),
    ("functional", "Functional"),
    ("misc", "Misc"),
)


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
        theme="halloween",
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
        theme="halloween",
    ),
    Palette(
        "warm",
        (hsv(0.02, 0.95, 1.0),   # red
         hsv(0.08, 1.0, 1.0),    # amber
         hsv(0.13, 0.85, 1.0)),  # gold
        "Reds through gold — flattering, low drama",
        theme="party",
    ),
    Palette(
        "cool",
        (hsv(0.55, 1.0, 1.0),    # cyan
         hsv(0.66, 1.0, 1.0),    # blue
         hsv(0.78, 0.9, 1.0)),   # indigo
        "Cyan through indigo",
        theme="club",
    ),
    Palette(
        "neon",
        (hsv(0.88, 1.0, 1.0),    # magenta
         hsv(0.50, 1.0, 1.0),    # cyan
         hsv(0.16, 1.0, 1.0),    # yellow
         hsv(0.33, 1.0, 1.0)),   # green
        "High-saturation magenta, cyan, yellow, green",
        theme="club",
    ),
    Palette(
        "sunset",
        (hsv(0.00, 1.0, 1.0),
         hsv(0.06, 1.0, 1.0),
         hsv(0.11, 0.9, 1.0),
         hsv(0.92, 0.8, 1.0)),
        "Red, orange, gold, pink",
        theme="party",
    ),
    Palette(
        "blood-moon",
        (hsv(0.98, 1.0, 1.0),    # crimson
         hsv(0.00, 1.0, 1.0),    # red
         hsv(0.80, 1.0, 0.9)),   # deep violet
        "Crimson and red under a violet sky",
        theme="halloween",
    ),
    Palette(
        "witch",
        (hsv(0.30, 1.0, 0.9),    # toxic green
         hsv(0.78, 0.95, 1.0),   # purple
         hsv(0.22, 1.0, 1.0)),   # lime
        "Cauldron green and witch purple",
        theme="halloween",
    ),
    Palette(
        "candy-corn",
        (hsv(0.08, 1.0, 1.0),    # orange
         hsv(0.14, 1.0, 1.0),    # yellow
         hsv(0.0, 0.0, 1.0)),    # white
        "Orange, yellow, white",
        theme="halloween",
    ),
    Palette(
        "graveyard",
        (hsv(0.48, 1.0, 1.0),    # teal
         hsv(0.60, 1.0, 1.0),    # cold blue
         hsv(0.36, 0.6, 0.9)),   # pale green
        "Cold, misty blues and greens",
        theme="halloween",
    ),
    Palette(
        "tropical",
        (hsv(0.92, 0.9, 1.0),    # pink
         hsv(0.07, 1.0, 1.0),    # orange
         hsv(0.47, 1.0, 1.0),    # turquoise
         hsv(0.25, 1.0, 1.0)),   # lime
        "Pink, orange, turquoise, lime",
        theme="party",
    ),
    Palette(
        "ocean",
        (hsv(0.50, 1.0, 1.0),    # aqua
         hsv(0.62, 1.0, 1.0),    # blue
         hsv(0.68, 1.0, 0.9)),   # deep blue
        "Aqua through deep blue",
        theme="party",
    ),
    Palette(
        "fire",
        (hsv(0.00, 1.0, 1.0),    # red
         hsv(0.05, 1.0, 1.0),    # orange
         hsv(0.10, 1.0, 1.0)),   # amber
        "Red, orange, amber",
        theme="party",
    ),
    Palette(
        "ice",
        (hsv(0.52, 0.6, 1.0),    # pale cyan
         hsv(0.62, 1.0, 1.0),    # blue
         hsv(0.0, 0.0, 1.0)),    # white
        "Pale cyan, blue, white",
        theme="party",
    ),
    Palette(
        "synthwave",
        (hsv(0.88, 1.0, 1.0),    # magenta
         hsv(0.75, 1.0, 1.0),    # purple
         hsv(0.63, 1.0, 1.0)),   # blue
        "Magenta, purple, blue",
        theme="club",
    ),
    Palette(
        "acid",
        (hsv(0.23, 1.0, 1.0),    # lime
         hsv(0.87, 1.0, 1.0),    # magenta
         hsv(0.16, 1.0, 1.0)),   # yellow
        "Lime, magenta, yellow",
        theme="club",
    ),
    Palette(
        "miami",
        (hsv(0.93, 0.9, 1.0),    # hot pink
         hsv(0.48, 1.0, 1.0)),   # teal
        "Hot pink and teal",
        theme="club",
    ),
    Palette(
        "deep-uv",
        (hsv(0.72, 1.0, 1.0),    # indigo
         hsv(0.78, 1.0, 1.0),    # violet
         hsv(0.66, 1.0, 1.0)),   # blue
        "Deep blues and violets that sit well with UV",
        theme="club",
    ),
    Palette(
        "tame-impala",
        # Currents magenta, Lonerism/Innerspeaker orange, psychedelic teal and
        # violet. Ordered so the swell look (colour i with i + 2) always pairs
        # a warm with a cool: magenta/teal, orange/violet.
        (hsv(0.90, 0.9, 1.0),    # Currents magenta
         hsv(0.06, 1.0, 1.0),    # burnt orange
         hsv(0.47, 1.0, 0.9),    # psychedelic teal
         hsv(0.76, 0.95, 1.0)),  # violet
        "Psychedelic magenta, burnt orange, teal, violet",
        theme="party",
    ),
    Palette(
        "mono-white",
        (hsv(0.0, 0.0, 1.0),),
        "Plain white — for finding your keys",
        theme="functional",
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


#: Palettes eligible for automatic switching until the host changes the pool.
#: Plain white is a work light, not a mood, so it starts out of the pool.
DEFAULT_POOL: tuple[str, ...] = tuple(n for n in names() if n != "mono-white")
