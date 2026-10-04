"""The patch: which fixtures exist, what they are, and where they live.

This is the file that makes "I bought more lights" a config edit. rig.yaml lists
fixture entries; an entry with a `count` expands into a bank of consecutively
addressed fixtures, so twelve PARs are four lines rather than twelve blocks.

Addresses can be explicit or left to expand automatically from a start address
using each profile's footprint. Overlaps are a hard error at load — two fixtures
sharing a channel is the kind of bug that presents as "fixture 7 flickers
sometimes" and costs an evening to find.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .color import Emission
from .profile import FixtureProfile, load_profiles

log = logging.getLogger(__name__)

DMX_SLOTS = 512


class PatchError(ValueError):
    """rig.yaml is malformed, or describes an impossible rig."""


@dataclass
class PatchedFixture:
    """One physical light at one DMX address."""

    fid: str
    label: str
    profile: FixtureProfile
    address: int
    #: Membership tags. Looks and the UI address fixtures by group.
    groups: tuple[str, ...] = ()
    #: Order along the physical run, which is what a chase needs to look like
    #: movement rather than noise. Defaults to patch order.
    position: int = 0
    enabled: bool = True

    @property
    def footprint(self) -> int:
        return self.profile.footprint

    @property
    def last_channel(self) -> int:
        return self.address + self.footprint - 1

    def channels(self) -> range:
        return range(self.address, self.last_channel + 1)

    def render_into(self, universe, em: Emission) -> None:
        """Convert an Emission for this model and write it to the universe."""
        if not self.enabled:
            return
        universe.set_block(self.address, self.profile.render(em))

    def describe(self) -> str:
        return (f"{self.fid} ({self.profile.model}) @ {self.address}"
                f"-{self.last_channel} [{', '.join(self.groups) or 'no groups'}]")


class Patch:
    """An ordered collection of patched fixtures, with group lookup."""

    def __init__(self, fixtures: list[PatchedFixture]):
        self.fixtures = fixtures
        self._validate()
        self.by_id = {f.fid: f for f in fixtures}
        self._by_group: dict[str, list[PatchedFixture]] = {}
        for f in fixtures:
            for grp in f.groups:
                self._by_group.setdefault(grp, []).append(f)
        for members in self._by_group.values():
            members.sort(key=lambda f: f.position)

    def _validate(self) -> None:
        seen_ids: set[str] = set()
        # channel -> fixture id, so a clash can name both culprits
        owner: dict[int, str] = {}
        for f in self.fixtures:
            if f.fid in seen_ids:
                raise PatchError(f"duplicate fixture id {f.fid!r}")
            seen_ids.add(f.fid)
            if f.address < 1 or f.last_channel > DMX_SLOTS:
                raise PatchError(
                    f"{f.fid}: address {f.address} with a {f.footprint}-channel "
                    f"footprint runs to {f.last_channel}, outside 1..{DMX_SLOTS}"
                )
            for ch in f.channels():
                if ch in owner:
                    raise PatchError(
                        f"channel {ch} is claimed by both {owner[ch]!r} and {f.fid!r} "
                        f"— fix the addresses in rig.yaml"
                    )
                owner[ch] = f.fid

    # -- queries ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self.fixtures)

    def __iter__(self):
        return iter(self.fixtures)

    def group(self, name: str) -> list[PatchedFixture]:
        """Fixtures in a group, ordered by physical position. Empty if unknown."""
        return list(self._by_group.get(name, ()))

    @property
    def groups(self) -> list[str]:
        return sorted(self._by_group)

    def ordered(self) -> list[PatchedFixture]:
        """Every enabled fixture in physical order — the spine of any chase."""
        return sorted((f for f in self.fixtures if f.enabled), key=lambda f: f.position)

    @property
    def max_channel(self) -> int:
        """Highest channel in use, which sets how short we can make the frame."""
        return max((f.last_channel for f in self.fixtures), default=0)

    def summary(self) -> str:
        lines = [f"{len(self.fixtures)} fixtures, channels 1..{self.max_channel}"]
        lines += ["  " + f.describe() for f in self.ordered()]
        return "\n".join(lines)

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, rig_path: str | Path, profiles_dir: str | Path | None = None) -> "Patch":
        rig_path = Path(rig_path)
        with rig_path.open() as fh:
            rig = yaml.safe_load(fh) or {}

        if profiles_dir is None:
            profiles_dir = rig_path.parent / "profiles"
        profiles = load_profiles(profiles_dir)
        if not profiles:
            raise PatchError(f"no fixture profiles found in {profiles_dir}")

        entries = rig.get("fixtures")
        if not entries:
            raise PatchError(f"{rig_path}: no `fixtures:` entries")

        fixtures: list[PatchedFixture] = []
        position = 0
        for entry in entries:
            fixtures.extend(_expand(entry, profiles, start_position=position))
            position = len(fixtures)

        patch = cls(fixtures)
        log.info("Patched %d fixtures using channels 1..%d", len(patch), patch.max_channel)
        return patch


def _expand(entry: dict, profiles: dict[str, FixtureProfile], start_position: int) -> list[PatchedFixture]:
    """Turn one rig.yaml entry into one or more fixtures.

    An entry with `count` becomes a bank: ids get a 1-based numeric suffix and
    addresses step by the profile's footprint from `address`.
    """
    try:
        profile_name = entry["profile"]
    except KeyError as e:
        raise PatchError(f"fixture entry missing `profile`: {entry!r}") from e

    profile = profiles.get(profile_name)
    if profile is None:
        raise PatchError(
            f"unknown profile {profile_name!r} — available: {sorted(profiles)}"
        )

    count = int(entry.get("count", 1))
    if count < 1:
        raise PatchError(f"{profile_name}: count must be at least 1, got {count}")

    base_id = entry.get("id")
    if not base_id:
        raise PatchError(f"fixture entry missing `id`: {entry!r}")

    try:
        address = int(entry["address"])
    except KeyError as e:
        raise PatchError(f"{base_id}: missing `address`") from e

    groups = tuple(entry.get("groups", ()) or ())
    label = entry.get("label") or profile.label or profile.model
    enabled = bool(entry.get("enabled", True))

    out: list[PatchedFixture] = []
    for i in range(count):
        fid = base_id if count == 1 else f"{base_id}{i + 1}"
        out.append(
            PatchedFixture(
                fid=fid,
                label=label if count == 1 else f"{label} {i + 1}",
                profile=profile,
                address=address + i * profile.footprint,
                groups=groups,
                position=start_position + i,
                enabled=enabled,
            )
        )
    return out
