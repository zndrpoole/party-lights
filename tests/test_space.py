"""Fixture geometry, and the spatial layers and scenes built on it.

Bug class guarded: a spatial look that ignores where the fixtures actually are.
The stage view's layout used to be purely visual, so moving a fixture there
changed nothing in the room. These pin that the layout reaches the looks, and
that the moves follow it.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partylights.audio.analyser import MusicState
from partylights.audio.features import FeatureFrame
from partylights.config import DEFAULT_RIG, load_patch
from partylights.dmx.null import NullDriver
from partylights.dmx.universe import Universe
from partylights.engine import palette as palettes
from partylights.engine.engine import Engine
from partylights.engine.layers import Alternate, Knockout, Ripple, Split, Travel
from partylights.engine.layers.base import Ctx
from partylights.engine.space import Space
from partylights.engine.state import EngineState
from partylights.web.server import create_app

PARS = [f"par{i}" for i in range(1, 13)]


@pytest.fixture
def patch():
    return load_patch(DEFAULT_RIG)


def music(t=0.0, *, beat=0.0, kick=False, locked=True):
    frame = FeatureFrame(t=t, rms=0.3, loudness_db=-10.0, energy=0.8, silent=False,
                         bands_smooth={"bass": 0.5}, onsets={"kick": kick},
                         onset_strength={"kick": 0.8 if kick else 0.0})
    return MusicState(frame=frame, tempo_locked=locked, beat_index=int(beat),
                      beat_phase=beat % 1.0, bar_phase=(beat % 4) / 4.0)


def ctx(space, m, dt=0.01, t=0.0):
    return Ctx(music=m, palette=palettes.get("halloween-deep"), dt=dt, space=space,
               time_scale=1.0, t=t, beats=m.beat_index + m.beat_phase)


def brightest(levels):
    return max(levels, key=levels.get)


def circle(n=12):
    """PARs in a ring, par1 at the right going clockwise on screen."""
    return {f"par{i + 1}": {"x": 0.5 + 0.4 * math.cos(2 * math.pi * i / n),
                            "y": 0.5 + 0.4 * math.sin(2 * math.pi * i / n)}
            for i in range(n)}


# -- geometry -----------------------------------------------------------------

def test_default_layout_runs_left_to_right(patch):
    space = Space(patch)
    xs = [space.along(fid, 0) for fid in PARS]
    assert xs[0] == 0.0 and xs[-1] == 1.0
    assert xs == sorted(xs)
    assert [space.spot(f).side for f in PARS] == [0] * 6 + [1] * 6


def test_layout_changes_the_axes(patch):
    space = Space(patch)
    # Reverse the room: par1 now on the right.
    space.set_layout({fid: {"x": 0.9 - 0.8 * i / 11, "y": 0.5} for i, fid in enumerate(PARS)})
    assert space.along("par1", 0) == 1.0 and space.along("par12", 0) == 0.0
    # Patch order (the chase path) does not move with the layout.
    assert space.spot("par1").path == 0.0


def test_unknown_and_malformed_layout_entries_are_ignored(patch):
    space = Space(patch)
    space.set_layout({"ghost": {"x": 0.1, "y": 0.1}, "par1": {"x": "left"}})
    assert space.spot("par1").x == pytest.approx(0.07)


def test_engine_shares_one_space_with_every_look(patch):
    universe = Universe(NullDriver(), slot_count=patch.max_channel)
    engine = Engine(patch, universe, EngineState())
    engine.set_layout(circle())
    assert all(look.space is engine.space for look in engine.looks.values())
    assert engine.looks["rotor"].space.spot("par4").y > 0.8


def test_saving_a_layout_in_the_stage_view_reaches_the_engine(patch, tmp_path):
    universe = Universe(NullDriver(), slot_count=patch.max_channel)
    engine = Engine(patch, universe, EngineState())
    (tmp_path / "layout.json").write_text('{"par1": {"x": 0.9, "y": 0.5}}')
    app = create_app(patch=patch, universe=universe, state=EngineState(), engine=engine,
                     cues=None, layout_path=tmp_path / "layout.json",
                     tuning_path=tmp_path / "tuning.json")
    # Read at startup ...
    assert engine.space.spot("par1").x == 0.9
    # ... and followed on every save.
    app.test_client().post("/api/layout", json={"par1": {"x": 0.3, "y": 0.4}})
    assert engine.space.spot("par1").x == 0.3


# -- moves follow the room ----------------------------------------------------

def test_sweep_starts_at_the_left_and_turns_at_the_right(patch):
    space = Space(patch)
    sweep = Travel(axis="along", beats=8, pingpong=True)
    assert brightest(sweep.render(ctx(space, music(beat=0)), PARS)) == "par1"
    assert brightest(sweep.render(ctx(space, music(beat=4)), PARS)) == "par12"


def test_sweep_follows_a_rearranged_room(patch):
    space = Space(patch)
    space.set_layout({fid: {"x": 0.9 - 0.8 * i / 11, "y": 0.5} for i, fid in enumerate(PARS)})
    sweep = Travel(axis="along", beats=8, pingpong=True)
    assert brightest(sweep.render(ctx(space, music(beat=0)), PARS)) == "par12"


def test_rotor_turns_around_a_ring(patch):
    space = Space(patch, circle())
    rotor = Travel(axis="angle", beats=12, pingpong=False, wrap=True)
    # One fixture per beat, all the way round.
    seen = [brightest(rotor.render(ctx(space, music(beat=b)), PARS)) for b in range(12)]
    assert seen == PARS


def test_alternate_trades_halves_on_the_beat_clock(patch):
    space = Space(patch)
    alt = Alternate(axis="side", beats=2, low=0.1)
    first = alt.render(ctx(space, music(beat=0.5)), PARS)
    later = alt.render(ctx(space, music(beat=2.5)), PARS)
    assert first["par1"] == 1.0 and first["par12"] == 0.1
    assert later["par1"] == 0.1 and later["par12"] == 1.0


def test_split_puts_partner_colours_on_each_half(patch):
    space = Space(patch)
    c = Split(axis="side").render(ctx(space, music()), PARS)
    pal = palettes.get("halloween-deep")
    assert c["par1"] == pal.at(0) and c["par12"] == pal.at(2)


def test_ripple_leaves_the_centre_then_reaches_the_ends(patch):
    space = Space(patch)
    ripple = Ripple(travel_s=0.5, width=0.15)
    ripple.render(ctx(space, music(kick=True), dt=0.0), PARS)
    early = ripple.render(ctx(space, music(), dt=0.05), PARS)
    for _ in range(8):
        late = ripple.render(ctx(space, music(), dt=0.05), PARS)
    assert early["par6"] > early["par1"]
    assert late["par1"] > late["par6"]


def test_knockout_cuts_on_the_kick_and_recovers(patch):
    space = Space(patch)
    ko = Knockout(depth=0.9, decay_s=0.2)
    assert ko.render(ctx(space, music()), PARS)["par1"] == 1.0
    assert ko.render(ctx(space, music(kick=True)), PARS)["par1"] == pytest.approx(0.1)
    for _ in range(40):
        level = ko.render(ctx(space, music(), dt=0.01), PARS)["par1"]
    assert level > 0.9


# -- scenes -------------------------------------------------------------------

def test_scene_moves_with_the_layout_saved_in_the_engine(patch):
    universe = Universe(NullDriver(), slot_count=patch.max_channel)
    engine = Engine(patch, universe, EngineState())
    look = engine.looks["sweep"]
    pal = palettes.get("halloween")
    before = look.render(music(beat=0), pal, 0.01)
    engine.set_layout({fid: {"x": 0.9 - 0.8 * i / 11, "y": 0.5} for i, fid in enumerate(PARS)})
    look.reset()
    after = look.render(music(beat=0), pal, 0.01)
    levels = lambda em: {fid: em[fid].intensity for fid in PARS}
    assert brightest(levels(before)) == "par1"
    assert brightest(levels(after)) == "par12"


def test_scenes_report_their_layers(patch):
    universe = Universe(NullDriver(), slot_count=patch.max_channel)
    engine = Engine(patch, universe, EngineState())
    assert engine.looks["ripple"].layer_names == [
        "Split", "Pulse(mul)", "Ripple(max)", "Accent[snare]"]
