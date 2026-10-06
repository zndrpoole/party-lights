"""The song-arc director: build, pre-drop, the drop on the one, phrase grid.

Driven by synthetic MusicState timelines with known ground truth, at 128 BPM:
kicks on every beat while the kick is in, a low-end level per section, and the
structure tracker's BUILD/DROP events where it would raise them.

Bug class guarded: the drop landing late. The structure tracker's DROP event
arrives about two seconds after the drop; these pin that the director fires on
the drop kick itself once a build has gone quiet.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partylights.audio.analyser import MusicState
from partylights.audio.features import FeatureFrame
from partylights.audio.structure import BUILD, DROP
from partylights.config import DEFAULT_RIG, load_patch
from partylights.dmx.null import NullDriver
from partylights.dmx.universe import Universe
from partylights.engine import arc as A
from partylights.engine.arc import ArcDirector
from partylights.engine.engine import Engine
from partylights.engine.layers.base import Ctx
from partylights.engine.state import EngineState

BPM = 128.0
BEAT = 60.0 / BPM
DT = 0.01


def state(t, *, kick=False, low=0.6, high=1.0, events=(), locked=True, beat_offset=0):
    """`low` drives both the normalised and raw low end; `high` the raw highs,
    relative to a groove at 1.0."""
    frame = FeatureFrame(t=t, rms=0.3, loudness_db=-10.0, energy=0.7, silent=False,
                         bands_smooth={"bass": low, "sub": low},
                         bands_raw={"sub": 200.0 * low, "bass": 200.0 * low,
                                    "high": high, "air": 0.0},
                         onsets={"kick": kick}, onset_strength={"kick": 0.8 if kick else 0.0})
    beats = t / BEAT + beat_offset
    return MusicState(frame=frame, bpm=BPM, tempo_locked=locked, beat_index=int(beats),
                      beat_phase=beats % 1.0, events=list(events))


def song(sections, *, locked=True, beat_offset=0):
    """Yield (t, MusicState) for [(beats, kicks_in, low, events_at_start[, high]), ...]."""
    t = 0.0
    last_beat = -1
    for beats, kicks, low, events, *rest in sections:
        high = rest[0] if rest else 1.0
        end = t + beats * BEAT
        first = True
        while t < end - 1e-9:
            b = int(t / BEAT + 1e-9)
            kick = kicks and b != last_beat
            if kick:
                last_beat = b
            yield t, state(t, kick=kick, low=low, high=high, events=events if first else (),
                           locked=locked, beat_offset=beat_offset)
            first = False
            t += DT


#: 32 beats groove, 32 beats build (kicks on, bass out, riser climbing), a
#: 4-beat gap (no kick, low end gone), then the drop. Levels as measured on
#: tools/make_test_audio.py's edm_arc_128: the build's low end is about a
#: third of the groove's, the drop's about 1.5x.
GROOVE, BUILD_BEATS, GAP = 32, 32, 4
EDM = [(GROOVE, True, 0.6, ()), (BUILD_BEATS, True, 0.2, (), 4.0), (GAP, False, 0.02, (), 8.0),
       (32, True, 0.9, ())]
DROP_T = (GROOVE + BUILD_BEATS + GAP) * BEAT


def run(director, sections, sustained=0.7, **kw):
    log = []
    for t, m in song(sections, **kw):
        director.update(m, DT, sustained)
        log.append((t, m, director.state, director.consume_drop(), director.mods))
    return log


def first(log, pred):
    return next((entry for entry in log if pred(entry)), None)


# -- the arc ------------------------------------------------------------------

def test_build_gap_then_drop_fires_on_the_drop_kick():
    log = run(ArcDirector(), EDM)
    build = first(log, lambda e: e[2] == A.BUILDING)
    predrop = first(log, lambda e: e[2] == A.PREDROP)
    drop = first(log, lambda e: e[3])
    # Recognised within a bar and a half of starting (a bar to confirm).
    assert GROOVE * BEAT < build[0] < (GROOVE + 6) * BEAT
    # The gap is recognised within its first two beats ...
    assert DROP_T - GAP * BEAT < predrop[0] < DROP_T - (GAP - 2) * BEAT
    # ... and the drop is the very frame of the first kick after it.
    assert drop[0] == pytest.approx(DROP_T, abs=DT)
    assert drop[2] == A.DROPPING


def test_drop_reanchors_the_phrase_grid_on_its_downbeat():
    # Start the tempo tracker's count 5 beats off the track's phrases.
    director = ArcDirector()
    log = run(director, EDM, beat_offset=5)
    drop = first(log, lambda e: e[3])
    assert director.anchored
    assert director.anchor == drop[1].beat_index
    assert director.phrase_position(drop[1]) == pytest.approx(0.0, abs=0.01)


def test_a_build_with_no_kicks_does_not_go_dark():
    """A riser-only build has no kick to lose; its quiet is not a gap."""
    log = run(ArcDirector(), [(32, True, 0.6, ()), (32, False, 0.1, (), 4.0)])
    assert any(e[2] == A.BUILDING for e in log)        # the riser is heard ...
    assert not any(e[2] == A.PREDROP for e in log)     # ... but nothing goes dark


def test_a_build_straight_into_the_drop_fires_on_its_first_kick():
    sections = [(32, True, 0.6, ()), (32, True, 0.2, (), 4.0), (16, True, 0.9, ())]
    log = run(ArcDirector(), sections)
    drop = first(log, lambda e: e[3])
    assert drop is not None
    assert 64 * BEAT <= drop[0] < 64.5 * BEAT
    assert drop[1].beat_index + (drop[1].beat_phase > 0.5) == 64


def test_the_intro_to_groove_step_is_not_a_build():
    """The structure tracker's BUILD event fired here on the test track; the
    low end arriving is the opposite of a build."""
    log = run(ArcDirector(), [(32, True, 0.15, (BUILD,)), (32, True, 0.6, (BUILD,))])
    assert not any(e[2] == A.BUILDING for e in log)


def test_breakdown_holds_until_the_kick_and_low_end_return():
    from partylights.audio.structure import BREAKDOWN as BD
    sections = [(32, True, 0.6, ()), (2, False, 0.01, (BD,)), (30, False, 0.01, ()),
                (16, True, 0.6, ())]
    log = run(ArcDirector(), sections)
    states = [(t, st) for t, m, st, d, mods in log]
    in_bd = [t for t, st in states if st == A.BREAKDOWN_STATE]
    assert in_bd and in_bd[0] == pytest.approx(32 * BEAT, abs=DT)
    assert in_bd[-1] >= 63 * BEAT     # held right through the quiet part


def test_a_gap_that_never_drops_gives_the_lights_back():
    log = run(ArcDirector(), EDM[:3] + [(16, False, 0.05, ())])
    predrop = first(log, lambda e: e[2] == A.PREDROP)
    after = [e for e in log if e[0] > predrop[0] + A.PREDROP_MAX_BEATS * BEAT + 0.1]
    assert after and all(e[2] != A.PREDROP for e in after)
    assert not any(e[3] for e in log)


def test_late_drop_event_confirms_rather_than_fires_twice():
    late = DROP_T + 2.0
    sections = EDM[:3] + [(int(2.0 / BEAT), True, 0.8, ()), (16, True, 0.8, (DROP,))]
    log = run(ArcDirector(), sections)
    assert sum(e[3] for e in log) == 1
    assert first(log, lambda e: e[3])[0] < late


def test_drop_event_without_a_build_still_drops():
    log = run(ArcDirector(), [(32, True, 0.3, ()), (16, True, 0.8, (DROP,))])
    drop = first(log, lambda e: e[3])
    assert drop is not None and drop[0] == pytest.approx(32 * BEAT, abs=DT)


def test_build_doubles_speed_each_phrase_and_ramps_up():
    log = run(ArcDirector(), [(32, True, 0.6, ()), (64, True, 0.2, (), 4.0)])
    rates = [e[4].rate for e in log if e[2] == A.BUILDING]
    assert rates[0] == 1 and max(rates) == A.MAX_RATE
    assert rates == sorted(rates)
    risers = [e[4].riser for e in log if e[2] == A.BUILDING]
    assert risers[0] < 0.1 and risers[-1] == 1.0


def test_track_change_forgets_the_phrase_grid():
    director = ArcDirector()
    run(director, EDM, beat_offset=5)
    assert director.anchored
    director.reset()
    assert not director.anchored and director.anchor == 0 and director.state == A.IDLE


# -- safety -------------------------------------------------------------------

@pytest.mark.parametrize("bpm", [90, 128, 140, 174])
def test_sped_up_patterns_never_step_into_the_flash_risk_band(bpm):
    m = state(0.0)
    for beats in (0.5, 1, 2, 4, 8):
        ctx = Ctx(music=m, palette=None, dt=DT, space=None, time_scale=1.0, t=0.0,
                  beats=0.0, rate=A.MAX_RATE, bps=bpm / 60.0)
        r = ctx.speed(beats)
        assert r * (bpm / 60.0) / beats <= 4.5 or r == 1
        assert r & (r - 1) == 0          # still a power of two


# -- in the engine ------------------------------------------------------------

@pytest.fixture
def engine():
    patch = load_patch(DEFAULT_RIG)
    universe = Universe(NullDriver(), slot_count=patch.max_channel)
    st = EngineState(look="rotor")
    st.set_mode("manual")
    e = Engine(patch, universe, st)
    e._energy_env._value = 0.7
    return e


def test_engine_goes_dark_in_the_gap_and_hits_on_the_drop(engine):
    pars = [f.fid for f in engine.patch.group("pars")]
    gap_frame = drop_frame = None
    for t, m in song(EDM):
        engine._music = m
        engine.tick(DT)
        if engine.arc.state == A.PREDROP and t > DROP_T - BEAT:
            gap_frame = {fid: engine.universe.buffer()[engine.patch.by_id[fid].address - 1]
                         for fid in pars}
            accent = engine.patch.by_id["accent"]
            gap_accent = engine.universe.buffer()[accent.address - 1 : accent.last_channel]
        if engine._dropped:
            drop_frame = {fid: engine.universe.buffer()[engine.patch.by_id[fid].address - 1]
                          for fid in pars}
            break
    # Every PAR's dimmer at zero in the gap, the accent's white emitter lit ...
    assert gap_frame and all(v == 0 for v in gap_frame.values())
    assert gap_accent[4] > 150
    # ... and the whole run at full on the drop, in manual mode too.
    assert drop_frame and all(v == 255 for v in drop_frame.values())
    assert engine.active_look == "rotor"


def test_auto_holds_the_look_through_the_build_and_changes_on_the_drop(engine):
    engine.state.set_mode("auto")
    looks = []
    for t, m in song(EDM):
        engine._music = m
        engine.tick(DT)
        looks.append((t, engine.active_look, engine.arc.state))
    in_build = {name for t, name, st in looks if st in (A.BUILDING, A.PREDROP)}
    assert len(in_build) == 1
    after = next(name for t, name, st in looks if t >= DROP_T + DT)
    assert after == "unison" and after not in in_build


def _live_engine(look):
    """An engine with the output smoothing the live rig runs, which the drop
    hit used to be strangled by."""
    patch = load_patch(DEFAULT_RIG)
    universe = Universe(NullDriver(), slot_count=patch.max_channel)
    st = EngineState(look=look)
    st.set_mode("manual")
    e = Engine(patch, universe, st, attack_ms=40, release_ms=350)
    e._energy_env._value = 0.7
    return e


def test_drop_hit_reaches_full_through_live_smoothing():
    engine = _live_engine("pulse")
    pars = [engine.patch.by_id[f.fid] for f in engine.patch.group("pars")]
    for t, m in song(EDM):
        engine._music = m
        engine.tick(DT)
        if engine._dropped:
            frame = engine.universe.buffer()
            assert all(frame[f.address - 1] == 255 for f in pars)
            return
    pytest.fail("no drop")


def test_a_build_gets_brighter_even_when_the_look_follows_the_bass():
    """Measured live: pulse dimmed to a third of its groove brightness through
    a build, because a build takes the bass away."""
    engine = _live_engine("pulse")
    pars = [engine.patch.by_id[f.fid] for f in engine.patch.group("pars")]
    groove, late_build = [], []
    for t, m in song(EDM):
        engine._music = m
        engine.tick(DT)
        frame = engine.universe.buffer()
        mean = sum(frame[f.address - 1] for f in pars) / len(pars)
        if 8 * BEAT < t < GROOVE * BEAT:
            groove.append(mean)
        elif engine.arc.state == A.BUILDING and engine.arc.mods.floor > 0.4:
            late_build.append(mean)
    assert late_build
    assert sum(late_build) / len(late_build) > sum(groove) / len(groove)
