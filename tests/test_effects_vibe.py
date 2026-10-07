"""Tests for the host's effects, the presets and the vibe control."""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partylights.audio.analyser import MusicState
from partylights.audio.features import FeatureFrame
from partylights.engine import palette as palettes
from partylights.engine.cues import PRESETS
from partylights.engine.effects import (HEART_MAX_BPM, MIN_GAP_S, EffectRack,
                                        HeartbeatEffect)
from partylights.engine.looks import BY_NAME, auto_selectable
from partylights.fixtures.color import Emission

from tests.test_engine import auto, kick_state, rig  # noqa: F401  (rig is a fixture)

DT = 0.01


def quiet(t=0.0, **kw):
    return MusicState(frame=FeatureFrame(t=t, rms=0.0, loudness_db=-120.0, energy=0.0), **kw)


def green_base(patch):
    """A stand-in show: every fixture a steady mid green."""
    return {f.fid: Emission((0.0, 1.0, 0.0), 0.5) for f in patch}


def make_rack(rig):
    patch, _, _, engine, _ = rig
    return EffectRack(patch, engine.space, random.Random(7))


def run(rack, patch, seconds, base=None, music=None):
    base = base if base is not None else green_base(patch)
    out = base
    for i in range(int(round(seconds / DT))):
        out = rack.apply(base, music or quiet(i * DT), DT)
    return out


# -- lightning ----------------------------------------------------------------

def test_lightning_flashes_white_then_hands_back_the_show(rig):
    patch = rig[0]
    rack = make_rack(rig)
    base = green_base(patch)
    assert rack.fire("lightning")["fired"]
    peak = max((rack.apply(base, quiet(), DT) for _ in range(30)),
               key=lambda o: o["par6"].intensity)
    assert peak["par6"].intensity > 0.9
    r, g, b = peak["par6"].rgb
    assert min(r, g, b) > 0.7, "the main flash is near white, not the show's green"
    out = run(rack, patch, 4.0, base)
    assert out == base
    assert rack.snapshot()["lightning"] is False


def test_lightning_never_exceeds_three_flashes_a_second_however_mashed(rig):
    patch = rig[0]
    rack = make_rack(rig)
    black = {f.fid: Emission((0, 0, 0), 0.0) for f in patch}
    onsets, lit, t = [], False, 0.0
    for step in range(int(20.0 / DT)):
        if step % 5 == 0:                       # pressed every 50 ms for 20 s
            rack.fire("lightning")
        out = rack.apply(black, quiet(t), DT)
        level = max(e.intensity for e in out.values())
        if level > 0.3 and not lit:
            onsets.append(t)
        lit = level > 0.3
        t += DT
    assert len(onsets) >= 15                     # it did keep striking
    for i, start in enumerate(onsets):
        assert sum(1 for o in onsets[i:] if o < start + 1.0) <= 3, onsets


def test_lightning_reports_when_it_is_cooling_down(rig):
    rack = make_rack(rig)
    assert rack.fire("lightning")["fired"]
    again = rack.fire("lightning")
    assert again["fired"] is False and 0 < again["wait"] <= MIN_GAP_S


# -- holds: candle and heartbeat ---------------------------------------------

def test_candle_fades_in_warm_and_flickers_each_fixture_on_its_own(rig):
    patch = rig[0]
    rack = make_rack(rig)
    rack.fire("candle")
    out = run(rack, patch, 2.0)
    for em in out.values():
        r, g, b = em.rgb
        assert r > g > b and b < 0.05, "flame colours, none of the green left"
        assert 0.15 < em.intensity < 0.7, "gentle: never out, never full"
    levels = {round(em.intensity, 3) for em in out.values()}
    assert len(levels) > len(out) // 2, "not one light pulsing in unison"


def test_a_hold_toggles_off_and_fades_back_to_the_show(rig):
    patch = rig[0]
    rack = make_rack(rig)
    rack.fire("candle")
    run(rack, patch, 2.0)
    assert rack.fire("candle") == {"effect": None}
    halfway = run(rack, patch, 0.5)
    assert halfway["par1"].rgb[1] > 0.1, "fading, not cut"
    assert run(rack, patch, 2.0) == green_base(patch)
    assert rack.snapshot() == {"hold": None, "lightning": False}


def test_picking_another_hold_crossfades_to_it(rig):
    patch = rig[0]
    rack = make_rack(rig)
    rack.fire("candle")
    run(rack, patch, 2.0)
    rack.fire("heartbeat")
    assert rack.snapshot()["hold"] == "heartbeat"
    out = run(rack, patch, 3.0)
    assert all(em.rgb == out["par1"].rgb for em in out.values())
    assert out["par1"].rgb[0] > 0.9 and out["par1"].rgb[1] < 0.1   # red


def test_lightning_strikes_over_a_held_effect(rig):
    patch = rig[0]
    rack = make_rack(rig)
    rack.fire("candle")
    run(rack, patch, 2.0)
    rack.fire("lightning")
    assert rack.snapshot() == {"hold": "candle", "lightning": True}
    run(rack, patch, 4.0)
    assert rack.snapshot() == {"hold": "candle", "lightning": False}


@pytest.mark.parametrize("bpm", [60.0, 80.0, 100.0, 128.0, 140.0, 174.0])
def test_heartbeat_follows_the_song_but_never_races(rig, bpm):
    heart = HeartbeatEffect(rig[0], rig[3].space, random.Random(1))
    heart.reset()
    music = quiet(bpm=bpm, tempo_locked=True, beat_index=5, beat_phase=0.0)
    _, period = heart._cycle(music, DT)
    assert 60.0 / period <= HEART_MAX_BPM + 1e-6
    # Locked to the song: a whole number of beats.
    beats = period * bpm / 60.0
    assert abs(beats - round(beats)) < 1e-6


def test_heartbeat_thumps_hardest_in_the_middle_of_the_yard(rig):
    patch = rig[0]
    heart = HeartbeatEffect(patch, rig[3].space, random.Random(1))
    heart.reset()
    out = heart.render({}, quiet(bpm=64.0, tempo_locked=True, beat_phase=0.02), DT)
    assert out["par6"].intensity > out["par1"].intensity > 0.3


# -- effects through cues and the engine --------------------------------------

def test_effects_leave_mode_look_and_palette_alone(rig):
    patch, universe, state, engine, cues = rig
    before = (state.mode, state.look, state.palette)
    for name in ("effect/lightning", "effect/candle", "effect/heartbeat"):
        cues.fire(name)
    assert (state.mode, state.look, state.palette) == before
    assert engine.stats()["effects"] == {"hold": "heartbeat", "lightning": True}


def test_resume_auto_and_effect_off_release_the_hold(rig):
    patch, universe, state, engine, cues = rig
    cues.fire("effect/candle")
    cues.fire("resume-auto")
    assert engine.effects.snapshot()["hold"] is None
    cues.fire("effect/heartbeat")
    cues.fire("effect/off")
    assert engine.effects.snapshot()["hold"] is None


def test_effects_reach_the_wire(rig):
    patch, universe, state, engine, cues = rig
    cues.fire("effect/heartbeat")
    for _ in range(300):
        engine.tick(DT)
    assert engine.stats()["effects"]["hold"] == "heartbeat"
    par = patch.by_id["par6"]
    raw = universe.buffer()[par.address - 1:par.address - 1 + par.footprint]
    r, g, b = par.profile.decode(raw)["display"]
    assert r > 10 * b and g == 0          # crimson: red with a touch of blue


def test_unknown_effect_is_rejected(rig):
    with pytest.raises(KeyError):
        rig[4].fire("effect/fog")


def test_effects_are_listed_as_cues(rig):
    available = rig[4].available()
    for name in ("lightning", "candle", "heartbeat", "off"):
        assert f"effect/{name}" in available


# -- presets ------------------------------------------------------------------

def test_every_preset_names_a_real_look_and_palette():
    for name, (look, palette, desc) in PRESETS.items():
        assert look in BY_NAME and palette in palettes.BY_NAME, name
        assert desc


def test_presets_avoid_looks_that_need_a_lock_or_a_human():
    for name, (look, _, _) in PRESETS.items():
        cls = BY_NAME[look]
        assert not cls.needs_tempo, f"{name}: a preset can fire before the tempo locks"
        assert not getattr(cls, "manual_only", False), name


def test_presets_run_calm_to_wild():
    wild = [BY_NAME[look].wildness for look, _, _ in PRESETS.values()]
    assert wild == sorted(wild)


@pytest.mark.parametrize("name", list(PRESETS))
def test_every_preset_fires(rig, name):
    patch, universe, state, engine, cues = rig
    look, palette, _ = PRESETS[name]
    out = cues.fire(f"preset/{name}")
    assert out["look"] == look and state.palette == palette and state.mode == "manual"


# -- vibe -----------------------------------------------------------------------

def test_middle_vibe_allows_every_auto_look(rig):
    engine = rig[3]
    assert rig[2].vibe == 0.5
    band = engine._band()
    assert all(engine._in_band(n, band) for n in auto_selectable())
    assert engine._arc_strength() == 1.0


def test_calm_vibe_never_picks_a_wild_look_however_loud(rig):
    patch, universe, state, engine, cues = rig
    cues.fire("vibe/0")
    engine._energy_env._value = 0.95
    seen = set()
    t, beat = 0.0, 0
    for _ in range(12):
        for _ in range(8):
            t += 40.0
            beat += 16
            auto(engine, kick_state(t, tempo_locked=True, bpm=128.0, beat_index=beat))
        seen.add(engine.active_look)
    assert seen and all(engine.looks[n].wildness <= 0.25 for n in seen), seen
    assert len(seen) >= 2, "a calm room still rotates"


def test_wild_vibe_keeps_off_the_calmest_looks(rig):
    patch, universe, state, engine, cues = rig
    cues.fire("vibe/1")
    engine._energy_env._value = 0.4
    seen = set()
    t, beat = 0.0, 0
    for _ in range(12):
        for _ in range(2):
            t += 40.0
            beat += 16
            auto(engine, kick_state(t, tempo_locked=True, bpm=128.0, beat_index=beat))
        seen.add(engine.active_look)
    assert all(engine.looks[n].wildness >= 0.4 for n in seen), seen


def test_wild_vibe_changes_look_twice_as_often(rig):
    patch, universe, state, engine, cues = rig
    cues.fire("vibe/1")
    engine._energy_env._value = 0.8
    auto(engine, kick_state(0.0, tempo_locked=True, bpm=128.0, beat_index=1))
    first = engine.active_look
    for beat in range(2, 32):
        auto(engine, kick_state(beat * 0.47, tempo_locked=True, bpm=128.0, beat_index=beat))
    assert engine.active_look == first
    auto(engine, kick_state(32 * 0.47, tempo_locked=True, bpm=128.0, beat_index=32))
    assert engine.active_look != first


def test_turning_the_vibe_down_moves_off_a_wild_look_at_the_next_phrase(rig):
    patch, universe, state, engine, cues = rig
    engine._energy_env._value = 0.8
    engine.select("knockout")
    cues.fire("vibe/0.1")
    for beat in range(1, 16):
        auto(engine, kick_state(beat * 0.47, tempo_locked=True, bpm=128.0, beat_index=beat))
    assert engine.active_look == "knockout", "waits for the phrase line"
    auto(engine, kick_state(16 * 0.47, tempo_locked=True, bpm=128.0, beat_index=16))
    assert engine.looks[engine.active_look].wildness <= 0.25 + 1.3 * 0.1


def test_calm_vibe_softens_the_drop_and_never_cuts_the_room_dark(rig):
    patch, universe, state, engine, cues = rig
    cues.fire("vibe/0")
    assert engine._arc_strength() == 0.0
    engine.arc.mods.blackout = 1.0
    engine.arc.mods.riser = 1.0
    shows = green_base(patch)
    assert engine._arc_cut(shows) == shows


def test_vibe_cues_step_and_clamp(rig):
    state, cues = rig[2], rig[4]
    cues.fire("vibe-up")
    assert state.vibe == pytest.approx(0.6)
    cues.fire("vibe/0.05")
    cues.fire("vibe-down")
    assert state.vibe == 0.0
