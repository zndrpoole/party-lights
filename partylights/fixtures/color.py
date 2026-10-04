"""Colour, kept device-independent until the last possible moment.

The rig mixes two different colour spaces: twelve RGB-only PARs and one
RGBWA+UV six-in-one. If looks emitted raw channel bytes they would have to know
which fixture they were talking to, and every new model would mean editing every
look. So looks emit an `Emission` — intent, in normalised terms — and each
fixture profile converts that into its own channels.

Two conversions earn their place here:

* **Gamma.** DMX drives PWM, so light output is linear in the DMX value, but
  perception is not. A linear fade looks like it rushes the top end and crawls at
  the bottom. Encoding with an exponent near 2.2 makes a fade look even.
* **White and amber extraction.** A six-in-one fixture makes a far better pastel
  or warm tone using its dedicated white and amber emitters than by mixing RGB.
  Extracting them is what makes the ZQ01430 look like a nicer light than the
  PARs rather than just a differently-wired one.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass, field

Rgb = tuple[float, float, float]


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def hsv(h: float, s: float = 1.0, v: float = 1.0) -> Rgb:
    """Hue/sat/value to RGB, all 0..1, hue wrapping rather than clamping."""
    return colorsys.hsv_to_rgb(h % 1.0, clamp(s), clamp(v))


def to_hsv(rgb: Rgb) -> tuple[float, float, float]:
    r, g, b = rgb
    return colorsys.rgb_to_hsv(clamp(r), clamp(g), clamp(b))


def mix(a: Rgb, b: Rgb, t: float) -> Rgb:
    """Linear interpolation between two colours. t=0 gives a, t=1 gives b."""
    t = clamp(t)
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


def scale(rgb: Rgb, k: float) -> Rgb:
    return (clamp(rgb[0] * k), clamp(rgb[1] * k), clamp(rgb[2] * k))


def gamma_byte(level: float, gamma: float = 2.2) -> int:
    """Normalised level to a DMX byte, gamma-encoded so fades look even.

    Luminance is linear in the DMX value and perception goes roughly as the
    2.2nd root of luminance, so raising the level to 2.2 makes perceived
    brightness track the level.
    """
    level = clamp(level)
    if level <= 0.0:
        return 0
    return int(round(255.0 * (level ** gamma)))


def linear_byte(level: float) -> int:
    """Normalised level straight to a DMX byte, no perceptual shaping.

    For channels where the value is a rate or an index rather than a brightness.
    """
    return int(round(255.0 * clamp(level)))


#: Where the amber emitter sits in RGB terms. Used to decide how much of a warm
#: colour can be handed to amber instead of being faked with red plus green.
AMBER_RGB: Rgb = (1.0, 0.55, 0.0)


@dataclass
class Emission:
    """What a look wants a fixture to do, in device-independent terms.

    A fixture honours what it can and ignores the rest: the RGB PARs drop `uv`
    on the floor, and a fixture with no strobe channel ignores `strobe`.
    """

    rgb: Rgb = (0.0, 0.0, 0.0)
    #: Master dimmer, 0..1. Kept separate from rgb so a look can pulse intensity
    #: without destroying colour information on the way down.
    intensity: float = 1.0
    #: 0 means shutter open. Above 0 maps onto the fixture's strobe rate range.
    strobe: float = 0.0
    #: Only fixtures with a UV emitter honour this. UV is outside the RGB gamut,
    #: so it is never derived from rgb — a look has to ask for it explicitly.
    uv: float = 0.0
    #: How eagerly to use dedicated white/amber emitters where they exist.
    #: 0 keeps colour purely in RGB; 1 extracts as much as the colour allows.
    emitter_bias: float = 1.0

    def with_intensity(self, k: float) -> "Emission":
        return Emission(self.rgb, clamp(self.intensity * k), self.strobe, self.uv, self.emitter_bias)

    def blended(self, other: "Emission", t: float) -> "Emission":
        """Crossfade towards `other`. Used for look transitions."""
        t = clamp(t)
        return Emission(
            rgb=mix(self.rgb, other.rgb, t),
            intensity=self.intensity + (other.intensity - self.intensity) * t,
            strobe=self.strobe + (other.strobe - self.strobe) * t,
            uv=self.uv + (other.uv - self.uv) * t,
            emitter_bias=self.emitter_bias + (other.emitter_bias - self.emitter_bias) * t,
        )


BLACK = Emission(rgb=(0.0, 0.0, 0.0), intensity=0.0)


def split_white(rgb: Rgb, bias: float = 1.0) -> tuple[Rgb, float]:
    """Pull a neutral white component out of an RGB colour.

    The common grey level across all three channels is exactly what a dedicated
    white emitter does better — brighter, and without the colour cast you get
    from mixing three LEDs. Returns the residual RGB and the white level.
    """
    bias = clamp(bias)
    w = min(rgb) * bias
    if w <= 0.0:
        return rgb, 0.0
    return (rgb[0] - w, rgb[1] - w, rgb[2] - w), w


def split_amber(rgb: Rgb, bias: float = 1.0) -> tuple[Rgb, float]:
    """Pull an amber component out of an RGB colour.

    Only warm colours have anything to give: we take the largest amber level
    that fits inside the available red and green without touching blue, since
    amber contributes none.
    """
    bias = clamp(bias)
    if bias <= 0.0:
        return rgb, 0.0
    r, g, b = rgb
    ar, ag, _ = AMBER_RGB
    a = min(r / ar, g / ag) * bias if g > 0.0 and r > 0.0 else 0.0
    if a <= 0.0:
        return rgb, 0.0
    return (r - a * ar, g - a * ag, b), a
