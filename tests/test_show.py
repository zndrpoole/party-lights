"""Tests for per-song shows, against the first real design card."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partylights.config import DEFAULT_RIG, load_patch
from partylights.dmx.null import NullDriver
from partylights.dmx.universe import Universe
from partylights.engine.engine import Engine
from partylights.engine.show import Card, ShowRunner, ramp, song_seconds
from partylights.engine.state import EngineState
from partylights.fixtures.color import Emission

THE_DAYS = "2FAZskT9yRjp2Oow9szJD8"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def show():
    patch = load_patch(DEFAULT_RIG)
    clock = Clock()
    runner = ShowRunner(patch, clock=clock)
    runner.start(THE_DAYS, 0.0)
    return patch, runner, clock


def at(runner, clock, song_t, *, vibe=0.5, dt=0.01, base=None):
    """The cue layer's output at song time `song_t`, over a mid-grey show."""
    clock.now = runner._anchor + song_t
    runner._synced = clock.now
    base = base or {f.fid: Emission((0.2, 0.4, 1.0), 0.5) for f in runner.patch}
    return runner.shade(base, vibe, dt, 4.0)


def pars(patch, out):
    return [out[f.fid] for f in patch.group("pars")]


def test_parsing():
    assert song_seconds("1:20.03") == pytest.approx(80.03)
    assert song_seconds("3:51.72") == pytest.approx(231.72)
    assert song_seconds("1.4 s") == pytest.approx(1.4)
    assert ramp("0.0 → 0.8", (0, 0)) == (0.0, 0.8)
    assert ramp("0.5 -> 1", (0, 0)) == (0.5, 1.0)
    assert ramp(None, (0.1, 0.2)) == (0.1, 0.2)


def test_card_loads():
    card = Card.load(THE_DAYS)
    assert len(card.sections) == 17 and len(card.cues) == 14
    assert card.bar_s == pytest.approx(240 / 137.8, abs=0.01)
    assert len(card.palette) == 3


def test_section_looks_and_calm_swap(show):
    _, runner, clock = show
    clock.now = runner._anchor + 5.0
    look, pal, level = runner.plan(0.5)
    assert (look, level) == ("drift", 0.30)
    assert pal.name == "the-days"
    clock.now = runner._synced = runner._anchor + 81.0
    assert runner.plan(0.5)[0] == "knockout"
    # The host's slider shifts the card's 0.75: fully calm lands at 0.25.
    assert runner.plan(0.0)[0] == "unison"


def test_stop_is_dark_for_exactly_the_gap(show):
    patch, runner, clock = show
    assert all(e.intensity == 0 for e in pars(patch, at(runner, clock, 71.8)))
    assert all(e.intensity > 0 for e in pars(patch, at(runner, clock, 71.2)))
    assert all(e.intensity > 0 for e in pars(patch, at(runner, clock, 72.7)))


def test_quarter_beat_of_dark_then_the_drop(show):
    patch, runner, clock = show
    assert all(e.intensity == 0 for e in pars(patch, at(runner, clock, 79.8)))
    out = at(runner, clock, 80.04)
    assert all(e.intensity == pytest.approx(1.0, abs=0.01) for e in out.values())
    assert all(min(e.rgb) > 0.95 for e in out.values())
    # ...and it decays rather than sticking.
    for _ in range(60):
        out = at(runner, clock, 80.05)
    assert max(e.intensity for e in pars(patch, out)) < 0.9


def test_build_gates_on_the_beat_and_speeds_up(show):
    patch, runner, clock = show
    card = runner.card

    def lit_changes(t0, t1):
        states, t = [], t0
        while t < t1:
            states.append(pars(patch, at(runner, clock, t))[0].intensity > 0.2)
            t += 0.01
        return sum(a != b for a, b in zip(states, states[1:]))

    early, late = lit_changes(73.3, 74.9), lit_changes(78.0, 79.5)
    assert late > 3 * early > 0
    # Whitening climbs across the build.
    assert min(pars(patch, at(runner, clock, 79.4))[0].rgb) > \
        min(pars(patch, at(runner, clock, 73.5))[0].rgb)
    assert card.beat_phase(card.beats[10]) == pytest.approx(10.0)


def test_second_drop_strobes_unless_the_vibe_is_low(show):
    patch, runner, clock = show
    at(runner, clock, 177.3)                       # before the drop's flash
    runner._flash = 0.0
    assert all(e.strobe > 0 for e in at(runner, clock, 178.5).values())
    runner._flash = 0.0
    assert all(e.strobe == 0 for e in at(runner, clock, 178.5, vibe=0.2).values())


def test_second_stop_kills_the_uv(show):
    patch, runner, clock = show
    base = {f.fid: Emission((0, 0, 0), 0.0, uv=1.0) for f in patch}
    acc = [at(runner, clock, 169.3, base=base)[f.fid] for f in patch.group("accent")]
    assert acc and all(e.uv == 0 for e in acc)


def test_final_fade_and_the_show_lets_go(show):
    patch, runner, clock = show
    runner._flash = 0.0
    out = at(runner, clock, 232.6)
    assert max(e.intensity for e in out.values()) < 0.3
    assert max(e.intensity for e in at(runner, clock, 234.0).values()) == 0
    clock.now = runner._anchor + runner.card.end + 5
    assert runner.plan(0.5) is None and not runner.active


def test_sync_ignores_wobble_and_follows_a_seek(show):
    _, runner, clock = show
    clock.now = runner._anchor + 30.0
    anchor = runner._anchor
    runner.sync(THE_DAYS, 30.1)
    assert runner._anchor == anchor
    runner.sync(THE_DAYS, 60.0)
    assert runner.song_time() == pytest.approx(60.0)


def test_show_ends_when_the_player_goes_quiet(show):
    _, runner, clock = show
    clock.now += 7.0
    assert runner.plan(0.5) is None


def test_engine_follows_the_card():
    patch = load_patch(DEFAULT_RIG)
    universe = Universe(NullDriver(), slot_count=patch.max_channel, refresh_hz=40.0)
    universe.driver.open()
    state = EngineState(look="ambient")
    engine = Engine(patch, universe, state, tick_hz=100)
    clock = Clock()
    engine.show.clock = clock
    engine.show.start(THE_DAYS, 105.0)
    engine.tick(0.01)
    assert engine.active_look == "mirror"           # chorus 1
    assert engine.stats()["show"]["track"] == THE_DAYS
    engine.show.stop()
    engine.tick(0.01)
    assert engine.active_look == "ambient"           # silence, auto mode again
