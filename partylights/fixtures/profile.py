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

        if self.has("strobe"):
            spec = next(c for c in self.channels if c.role == "strobe")
            if em.strobe <= 0.0:
                out[spec.offset] = spec.open_value
            else:
                span = spec.max_rate - spec.min_rate
                out[spec.offset] = spec.min_rate + int(round(span * clamp(em.strobe)))

        return out

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
