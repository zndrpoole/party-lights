"""Fixture profiles: what a model's DMX channels mean.

A profile is data, not code. Supporting a new light means dropping one YAML file
into config/profiles/ and referencing it from rig.yaml — no Python changes, which
is the whole point given the rig is expected to grow.

Each channel declares a *role*. The renderer knows how to satisfy roles; it knows
nothing about specific models.

    dimmer  master intensity
    red/green/blue/white/amber   colour emitters
    uv      ultraviolet emitter, driven only when a look asks for it
    strobe  shutter; a configurable "open" value plus a rate range
    fixed   a constant we must keep asserting every frame

`fixed` is not padding. These fixtures have mode channels that, left at anything
but zero, make the fixture run its own internal program and silently ignore every
colour we send. Re-asserting the constant on every frame means a glitched or
partial frame cannot strand a fixture in auto mode for the rest of the night.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .color import (
    Emission,
    clamp,
    gamma_byte,
    linear_byte,
    split_amber,
    split_white,
)

log = logging.getLogger(__name__)

COLOUR_ROLES = {"red", "green", "blue", "white", "amber"}
KNOWN_ROLES = COLOUR_ROLES | {"dimmer", "uv", "strobe", "fixed"}

#: How amber and UV emitters read on screen, in linear RGB. Display only.
AMBER_MIX = (1.0, 0.55, 0.0)
UV_MIX = (0.30, 0.0, 0.65)


class ProfileError(ValueError):
    """A profile file is malformed or self-inconsistent."""


@dataclass
class ChannelSpec:
    #: 0-based position within the fixture, so address + offset is the DMX slot.
    offset: int
    role: str
    #: `fixed` role only: the constant to assert.
    value: int = 0
    #: `strobe` role only.
    open_value: int = 0
    min_rate: int = 8
    max_rate: int = 255
    note: str = ""

    def __post_init__(self):
        if self.role not in KNOWN_ROLES:
            raise ProfileError(f"unknown channel role {self.role!r} (known: {sorted(KNOWN_ROLES)})")
        if self.offset < 0:
            raise ProfileError(f"negative channel offset {self.offset}")


@dataclass
class FixtureProfile:
    model: str
    footprint: int
    channels: list[ChannelSpec]
    label: str = ""
    gamma: float = 2.2
    #: Lowest dimmer byte at which the fixture still shows the colour it was
    #: sent. Anything below is sent as fully dark instead. 0 disables.
    min_dimmer: int = 0
    notes: str = ""
    source: str = ""

    #: role -> offset, built once at load.
    _by_role: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if self.footprint < 1:
            raise ProfileError(f"{self.model}: footprint must be at least 1")
        seen: dict[str, int] = {}
        for ch in self.channels:
            if ch.offset >= self.footprint:
                raise ProfileError(
                    f"{self.model}: channel offset {ch.offset} ({ch.role}) "
                    f"exceeds footprint {self.footprint}"
                )
            if ch.role != "fixed":
                if ch.role in seen:
                    raise ProfileError(f"{self.model}: role {ch.role!r} declared twice")
                seen[ch.role] = ch.offset
        self._by_role = seen

    # -- capability queries, used by looks and by the UI -------------------

    def has(self, role: str) -> bool:
        return role in self._by_role

    @property
    def has_uv(self) -> bool:
        return self.has("uv")

    @property
    def has_strobe(self) -> bool:
        return self.has("strobe")

    @property
    def emitters(self) -> list[str]:
        return [r for r in ("red", "green", "blue", "white", "amber", "uv") if self.has(r)]

    # -- the one interesting method ---------------------------------------

    def render(self, em: Emission) -> bytearray:
        """Turn an Emission into this fixture's channel values."""
        out = bytearray(self.footprint)

        # Assert constants first so a later role can never leave one unset.
        for ch in self.channels:
            if ch.role == "fixed":
                out[ch.offset] = ch.value & 0xFF

        r, g, b = (clamp(em.rgb[0]), clamp(em.rgb[1]), clamp(em.rgb[2]))

        # Without a dimmer channel, intensity has to be folded into the colour.
        # With one, keep them separate: scaling colour to fade loses hue
        # resolution at the bottom of the fade, where it is most visible.
        if self.has("dimmer"):
            out[self._by_role["dimmer"]] = gamma_byte(em.intensity, self.gamma)
        else:
            k = clamp(em.intensity)
            r, g, b = r * k, g * k, b * k

        # Hand as much of the colour as possible to dedicated emitters.
        white = amber = 0.0
        if self.has("white"):
            (r, g, b), white = split_white((r, g, b), em.emitter_bias)
        if self.has("amber"):
            (r, g, b), amber = split_amber((r, g, b), em.emitter_bias)

        for role, level in (("red", r), ("green", g), ("blue", b),
                            ("white", white), ("amber", amber)):
            if self.has(role):
                out[self._by_role[role]] = gamma_byte(level, self.gamma)

        if self.has("uv"):
            out[self._by_role["uv"]] = gamma_byte(em.uv, self.gamma)

        # Cheap fixtures cannot hold a hue near the bottom of the dimmer: each
        # emitter drops out at a different level, so orange turns red. Below
        # the fixture's floor, go fully dark instead -- and zero the emitters
        # too, since some units leak colour even with the dimmer at 0.
        if self.has("dimmer") and out[self._by_role["dimmer"]] < self.min_dimmer:
            for role in ("dimmer", *COLOUR_ROLES, "uv"):
                if self.has(role):
                    out[self._by_role[role]] = 0

        if self.has("strobe"):
            spec = next(c for c in self.channels if c.role == "strobe")
            if em.strobe <= 0.0:
                out[spec.offset] = spec.open_value
            else:
                span = spec.max_rate - spec.min_rate
                out[spec.offset] = spec.min_rate + int(round(span * clamp(em.strobe)))

        return out

    #: Roles the test bench may set directly. Strobe and fixed channels are left
    #: out on purpose: strobe has no time limit there, and a non-zero mode
    #: channel hands the fixture to its internal programs.
    BENCH_ROLES = ("dimmer", "red", "green", "blue", "white", "amber", "uv")

    def raw_frame(self, values: dict[str, int]) -> bytearray:
        """Channel bytes with the given roles set verbatim, for the test bench.

        Starts from a dark render so the fixed channels are still asserted, then
        writes each value straight through: no gamma, no white/amber
        extraction, no min_dimmer. Roles this fixture lacks are ignored.
        """
        out = self.render(Emission(intensity=0.0))
        for role, value in values.items():
            if role in self.BENCH_ROLES and self.has(role):
                out[self._by_role[role]] = int(value) & 0xFF
        return out

    def decode(self, raw) -> dict:
        """Read this fixture's channel bytes back into what the light is doing.

        The inverse of render(), and deliberately working from bytes rather than
        from the Emission: the visualiser feeds it the exact frame that went to
        the driver, so it shows blackout, freeze, manual overrides and any
        profile mistake exactly as the real fixture would.

        Levels are linear light output (DMX drives PWM), not the perceptual
        level the look asked for; `display` re-encodes them for a screen.
        """
        raw = bytes(raw[: self.footprint]).ljust(self.footprint, b"\x00")
        level = lambda role: raw[self._by_role[role]] / 255.0 if self.has(role) else 0.0

        dimmer = level("dimmer") if self.has("dimmer") else 1.0
        emitters = {r: round(level(r), 4) for r in self.emitters}

        strobe = None
        if self.has("strobe"):
            spec = next(c for c in self.channels if c.role == "strobe")
            v = raw[spec.offset]
            span = max(1, spec.max_rate - spec.min_rate)
            strobe = (v - spec.min_rate) / span if v >= spec.min_rate else 0.0

        # Light output per emitter, mixed into one colour as the eye would see
        # the fixture from across the room. UV reads as a dim violet.
        w, a, uv = level("white"), level("amber"), level("uv")
        lin = [
            level("red") + w + a * AMBER_MIX[0] + uv * UV_MIX[0],
            level("green") + w + a * AMBER_MIX[1] + uv * UV_MIX[1],
            level("blue") + w + a * AMBER_MIX[2] + uv * UV_MIX[2],
        ]
        lin = [c * dimmer for c in lin]
        output = max(lin)
        # Normalise to keep the hue when emitters stack past full, then
        # gamma-encode for an sRGB screen.
        hue = [c / output for c in lin] if output > 1.0 else lin
        display = [int(round(255 * clamp(c) ** (1 / 2.2))) for c in hue]

        return {
            "raw": list(raw),
            "dimmer": round(dimmer, 4),
            "emitters": emitters,
            "strobe": None if strobe is None else round(clamp(strobe), 4),
            "output": round(clamp(output), 4),
            "display": display,
        }

    # -- loading ----------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict, source: str = "") -> "FixtureProfile":
        try:
            model = data["model"]
            footprint = int(data["footprint"])
            raw_channels = data["channels"]
        except KeyError as e:
            raise ProfileError(f"{source or 'profile'}: missing required key {e}") from e

        channels = [ChannelSpec(**ch) for ch in raw_channels]
        return cls(
            model=model,
            footprint=footprint,
            channels=channels,
            label=data.get("label", ""),
            gamma=float(data.get("gamma", 2.2)),
            min_dimmer=int(data.get("min_dimmer", 0)),
            notes=data.get("notes", ""),
            source=source,
        )

    @classmethod
    def load(cls, path: str | Path) -> "FixtureProfile":
        path = Path(path)
        with path.open() as fh:
            data = yaml.safe_load(fh)
        return cls.from_dict(data, source=str(path))


def load_profiles(directory: str | Path) -> dict[str, FixtureProfile]:
    """Load every profile in a directory, keyed by filename stem.

    rig.yaml refers to profiles by stem (e.g. `zq01104`), so the filename is the
    identifier and the `model` field inside is just documentation.
    """
    directory = Path(directory)
    profiles: dict[str, FixtureProfile] = {}
    for path in sorted(directory.glob("*.yaml")):
        profile = FixtureProfile.load(path)
        profiles[path.stem] = profile
        log.debug("loaded profile %s (%s, %dch)", path.stem, profile.model, profile.footprint)
    if not profiles:
        log.warning("no fixture profiles found in %s", directory)
    return profiles
