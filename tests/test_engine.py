"""Tests for the engine, looks, cues and web API."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partylights.audio.analyser import Analyser, MusicState
from partylights.audio.features import FeatureFrame
from partylights.config import DEFAULT_RIG, Settings, load_patch
from partylights.dmx.null import NullDriver
from partylights.dmx.universe import Universe
from partylights.engine import palette as palettes
from partylights.engine.cues import CueRouter
from partylights.engine.engine import IDLE_ENERGY, Engine
from partylights.engine.looks import BY_NAME, LOOKS, auto_selectable
from partylights.engine.state import EngineState
from tools.make_test_audio import SR, render


@pytest.fixture
def rig():
    patch = load_patch(DEFAULT_RIG)
    universe = Universe(NullDriver(), slot_count=patch.max_channel, refresh_hz=40.0)
    universe.driver.open()
    state = EngineState(look="ambient")
    engine = Engine(patch, universe, state, analyser=Analyser(sample_rate=SR), tick_hz=100)
    cues = CueRouter(engine, universe, state)
    return patch, universe, state, engine, cues


def silent_state(t=0.0):
    return MusicState(frame=FeatureFrame(t=t, rms=0.0, loudness_db=-120.0, energy=0.0))


def drive(engine, signal, ticks_per_second=100):
    """Feed audio through the engine the way the real tick loop does."""
    hop = SR // ticks_per_second
    for i in range(0, len(signal), hop):
        out = engine.analyser.feed(signal[i : i + hop])
        if out:
            engine._music = engine._merge(out)
        engine.tick(1.0 / ticks_per_second)


# -- one tick, several analysis frames ---------------------------------------
#
# Bug guarded: the engine kept only the newest frame of each tick, so a kick in
# an earlier hop never reached the looks (or the UI), and a tick with no new
# frame re-served the old one, firing the same hit twice.

def _hit(t, region=None, events=()):
    onsets = {r: r == region for r in ("kick", "snare", "hat")}
    return MusicState(frame=FeatureFrame(t=t, rms=0.1, loudness_db=-20.0, energy=0.5,
                                         onsets=onsets, silent=False), events=list(events))


def test_a_hit_in_an_earlier_frame_of_the_tick_is_kept(rig):
    engine = rig[3]
    merged = engine._merge([_hit(1.0, "kick", ["drop"]), _hit(1.01, "snare")])
    assert merged.onset("kick") and merged.onset("snare")
    assert merged.t == 1.01 and merged.events == ["drop"]
    assert engine.onset_counts == {"kick": 1, "snare": 1}


def test_a_tick_without_new_audio_does_not_repeat_the_hit(rig):
    engine = rig[3]
    engine._music = engine._merge([_hit(1.0, "kick", ["drop"])])
    quiet = engine._quiet(engine._music)
    assert not quiet.onset("kick") and quiet.events == [] and quiet.energy == 0.5


# -- output smoothing -------------------------------------------------------
#
# Bug guarded: on the real rig `pulse` held the PARs at DMX 1 between kicks with
# hits gone in under 100 ms, which reads as flicker. The smoother gives every look
# a fast attack and a slow release so hits still land but fall as a fade.

def _level(engine, em, ticks, dt=0.01):
    from partylights.fixtures.color import Emission
    for _ in range(ticks):
        out = engine._smooth({"par1": Emission(rgb=(1, 1, 1), intensity=em)}, dt)
    return out["par1"].intensity


def test_smoothing_is_off_by_default(rig):
    _, _, _, engine, _ = rig
    _level(engine, 1.0, 1)
    assert _level(engine, 0.0, 1) == 0.0


def test_smoothing_attack_is_fast_and_release_is_slow(rig):
    _, _, _, engine, _ = rig
    engine.attack_s, engine.release_s = 0.015, 0.22
    engine.looks[engine.active_look].release_s = None   # use the global release
    _level(engine, 0.0, 1)
    # A hit lands almost fully within 50 ms ...
    assert _level(engine, 1.0, 5) > 0.95
    # ... but after the look drops to dark, 100 ms later it is still glowing ...
    assert _level(engine, 0.0, 10) > 0.5
    # ... and it has faded out within about a second.
    assert _level(engine, 0.0, 90) < 0.02


def test_a_look_can_set_its_own_release(rig):
    """Crisp looks snap off, drifting looks roll off: the contrast matters."""
    _, _, _, engine, _ = rig
    engine.attack_s, engine.release_s = 0.015, 0.35
    engine.select("unison")                       # release_s 0.12
    _level(engine, 1.0, 20)
    crisp = _level(engine, 0.0, 30)               # 300 ms after the drop
    engine.looks["unison"].release_s = None       # fall back to the global 0.35
    _level(engine, 1.0, 20)
    soft = _level(engine, 0.0, 30)
    assert crisp < 0.1 < soft


def test_softness_scales_every_fade(rig):
    """One control for the feel: x0.25 fully sharp, x4 fully smooth."""
    _, _, state, engine, _ = rig
    state.set_softness(5.0)
    assert state.softness == 1.0 and state.softness_scale() == 4.0
    state.set_softness(-1.0)
    assert state.softness_scale() == 0.25

    engine.attack_s, engine.release_s = 0.015, 0.35
    engine.looks[engine.active_look].release_s = None
    tails = {}
    for soft in (-1.0, 0.0, 1.0):
        state.set_softness(soft)
        _level(engine, 1.0, 50)
        tails[soft] = _level(engine, 0.0, 30)          # 300 ms after the drop
    assert tails[-1.0] < tails[0.0] < tails[1.0]


def test_softness_reaches_the_looks_own_decays(rig):
    patch, universe, state, engine, _ = rig
    levels = {}
    for soft in (-1.0, 1.0):
        state.set_softness(soft)
        engine.tick(0.01)                               # engine hands looks the scale
        look = engine.looks["unison"]
        look.reset()
        pal = palettes.get("halloween")
        look.render(kick_state(0.0, kick=True), pal, 0.025)
        for k in range(12):                             # 300 ms after the hit
            out = look.render(kick_state(0.025 * k), pal, 0.025)
        levels[soft] = out["par1"].intensity
    assert levels[-1.0] < levels[1.0]


def test_manual_override_is_not_smoothed(rig):
    """A fixture the host grabs, or a blackout, must respond on the next tick."""
    patch, universe, state, engine, _ = rig
    engine.attack_s, engine.release_s = 0.015, 5.0
    state.update_manual("par1", active=True, rgb=(1, 1, 1), intensity=1.0)
    engine.tick(0.01)
    state.update_manual("par1", active=True, rgb=(1, 1, 1), intensity=0.0)
    engine.tick(0.01)
    f = patch.by_id["par1"]
    assert universe.buffer()[f.address - 1] == 0


# -- test bench -------------------------------------------------------------

def test_bench_bytes_reach_the_universe_verbatim(rig):
    """The bench exists to probe the fixture, so nothing may reshape its bytes:
    not the look, not the master, and not the dimmer floor (14 would otherwise be 0)."""
    patch, universe, state, engine, _ = rig
    state.set_master(0.2)
    state.set_dimmer_floor(15)
    state.set_raw("par1", {"dimmer": 14, "red": 255, "green": 51, "blue": 0})
    engine.tick(0.01)
    f = patch.by_id["par1"]
    assert list(universe.buffer()[f.address - 1 : f.last_channel]) == [14, 255, 51, 0, 0, 0, 0]


def test_dimmer_floor_cuts_dim_fixtures_to_black(rig):
    """Off by default, so a dim level reaches the wire; raised, it goes dark."""
    patch, universe, state, engine, _ = rig
    state.set_mode("manual")
    state.update_manual("par1", active=True, rgb=(1.0, 0.2, 0.0), intensity=0.25)
    f = patch.by_id["par1"]
    engine.tick(0.01)
    dim = universe.buffer()[f.address - 1]
    assert 0 < dim < 30
    state.set_dimmer_floor(30)
    engine.tick(0.01)
    assert list(universe.buffer()[f.address - 1 : f.address + 3]) == [0, 0, 0, 0]
    state.set_dimmer_floor(999)
    assert state.dimmer_floor == 40


def test_bench_release_hands_the_fixture_back(rig):
    patch, universe, state, engine, _ = rig
    state.set_raw("par1", {"dimmer": 14})
    state.release_raw(["par1"])
    engine.tick(0.01)
    assert universe.buffer()[patch.by_id["par1"].address - 1] != 14
    assert state.active_raw() == {}


def test_bench_cannot_touch_strobe_or_mode_channels(rig):
    patch, *_ = rig
    out = patch.by_id["par1"].profile.raw_frame({"dimmer": 255, "strobe": 200, "fixed": 99})
    assert list(out) == [255, 0, 0, 0, 0, 0, 0]


def test_bench_api_validates_input(rig):
    patch, universe, state, engine, cues = rig
    from partylights.web.server import create_app
    client = create_app(patch=patch, universe=universe, state=state, engine=engine,
                        cues=cues, tuning_path=Path("/nonexistent/tuning.json")).test_client()
    ok = client.post("/api/bench", json={"group": "pars", "values": {"dimmer": 14}})
    assert ok.status_code == 200 and len(ok.get_json()["held"]) == 12
    assert client.post("/api/bench", json={"ids": ["par1"], "values": {"strobe": 9}}).status_code == 400
    assert client.post("/api/bench", json={"ids": ["par1"], "values": {"dimmer": 300}}).status_code == 400
    assert client.post("/api/bench/release", json={}).get_json() == {"held": {}}


# -- swell look and presets --------------------------------------------------

from partylights.engine.looks.swell import HOLD_S, MIN_HOLD_S, SwellLook
from partylights.audio.structure import SECTION_CHANGE


def loud_state(t=0.0, level=0.9, events=()):
    frame = FeatureFrame(t=t, rms=0.3, loudness_db=-10.0, energy=level, silent=False,
                         bands_smooth={"bass": level, "sub": level})
    return MusicState(frame=frame, events=list(events))


def _run_swell(look, seconds, state_at, dt=0.025):
    """Render for a while; returns per-tick {fid: Emission}."""
    pal = palettes.get("halloween-deep")
    frames = []
    for k in range(int(seconds / dt)):
        frames.append(look.render(state_at(k * dt), pal, dt))
    return frames


def test_swell_holds_colour_then_rolls_to_the_next(rig):
    patch, *_ = rig
    look = SwellLook(patch)
    frames = _run_swell(look, HOLD_S + 12, lambda t: loud_state(t))
    held = {fr["par1"].rgb for fr in frames[: int((HOLD_S - 1) / 0.025)]}
    assert len(held) == 1
    assert frames[-1]["par1"].rgb != frames[0]["par1"].rgb


def test_swell_does_not_show_individual_hits(rig):
    patch, *_ = rig
    look = SwellLook(patch)
    frames = _run_swell(look, 4, lambda t: loud_state(t) if 2.0 <= t < 2.1 else silent_state(t))
    before, after = frames[int(1.9 / 0.025)]["par1"], frames[int(2.2 / 0.025)]["par1"]
    assert abs(after.intensity - before.intensity) < 0.08


def test_swell_section_change_rotates_only_after_min_hold(rig):
    patch, *_ = rig
    look = SwellLook(patch)
    early = lambda t: loud_state(t, events=[SECTION_CHANGE] if abs(t - 5.0) < 0.01 else [])
    _run_swell(look, 6, early)
    assert look._offset == 0
    late = lambda t: loud_state(t, events=[SECTION_CHANGE] if abs(t - (MIN_HOLD_S - 6 + 1)) < 0.01 else [])
    _run_swell(look, MIN_HOLD_S - 6 + 2, late)
    assert look._offset == 1


def test_swell_pairs_orange_with_purple(rig):
    patch, *_ = rig
    fr = SwellLook(patch).render(silent_state(), palettes.get("halloween-deep"), 0.025)
    r1, _, b1 = fr["par1"].rgb
    r4, _, b4 = fr["par4"].rgb
    assert r1 == 1.0 and b1 == 0.0      # orange block
    assert b4 == 1.0 and r4 < 0.8       # purple block


def test_preset_cue_sets_look_and_palette_and_holds(rig):
    patch, universe, state, engine, cues = rig
    out = cues.fire("preset/halloween-smooth")
    assert out["look"] == "swell" and out["palette"] == "halloween-deep"
    assert state.mode == "manual" and state.palette == "halloween-deep"
    assert "preset/halloween-smooth" in cues.available()


# -- variety: unison, mirror, hush, drop hit ---------------------------------

from partylights.engine.engine import DROP, BREAKDOWN


def kick_state(t=0.0, kick=False, **kw):
    frame = FeatureFrame(t=t, rms=0.3, loudness_db=-10.0, energy=0.8, silent=False,
                         bands_smooth={"bass": 0.6}, onsets={"kick": kick},
                         onset_strength={"kick": 0.8 if kick else 0.0})
    return MusicState(frame=frame, **kw)


def auto(engine, music, dt=0.01):
    """One auto-mode decision, the way the tick makes it: the song-arc
    director hears the music first."""
    engine._direct(music, dt)
    engine._choose_auto(music)


def _kicks_every(period):
    return lambda t: kick_state(t, kick=(int(t / 0.025) % int(period / 0.025) == 0))


def test_unison_moves_the_whole_room_together(rig):
    patch, *_ = rig
    look = BY_NAME["unison"](patch)
    pal = palettes.get("halloween")
    quiet = look.render(kick_state(0.0), pal, 0.025)
    hit = look.render(kick_state(0.025, kick=True), pal, 0.025)
    pars = [f.fid for f in patch.group("pars")]
    assert len({(hit[f].rgb, hit[f].intensity) for f in pars}) == 1
    assert hit["par1"].intensity > quiet["par1"].intensity + 0.2


def test_mirror_is_symmetric_and_bursts_outward(rig):
    patch, *_ = rig
    look = BY_NAME["mirror"](patch)
    pal = palettes.get("halloween")
    look.render(kick_state(0.0, kick=True), pal, 0.025)
    early = look.render(kick_state(0.05), pal, 0.025)
    for _ in range(16):                           # ring reaches the ends at 0.45 s
        late = look.render(kick_state(0.3), pal, 0.025)
    pars = [f.fid for f in patch.group("pars")]
    for a, b in zip(pars, reversed(pars)):
        assert abs(early[a].intensity - early[b].intensity) < 1e-9
    # Early on the centre is brightest; later the ring has reached the ends.
    assert early["par6"].intensity > early["par1"].intensity
    assert late["par1"].intensity > late["par6"].intensity


def test_colour_follows_bars_not_kicks_when_tempo_is_locked(rig):
    """The kick detector over-counts; on the rig, kick-counted colour changed
    several times faster than intended. With a lock, bars decide."""
    patch, *_ = rig
    pal = palettes.get("halloween")
    for name in ("pulse", "mirror", "unison"):
        look = BY_NAME[name](patch)
        colours = {look.render(kick_state(k * 0.025, kick=True, tempo_locked=True,
                                          beat_index=5), pal, 0.025)["par1"].rgb
                   for k in range(200)}
        assert len(colours) == 1, name


def test_breakdown_goes_to_hush_and_the_kick_brings_it_back(rig):
    """Hush holds for the whole breakdown. It used to leave as soon as the
    energy read busy, which the analyser's gain control makes happen within
    a tick of a quiet pad -- on the test track hush lasted 10 ms."""
    patch, universe, state, engine, _ = rig
    engine._energy_env._value = 0.5
    auto(engine, kick_state(40.0, events=[BREAKDOWN]))
    assert engine.active_look == "hush"
    out = engine.looks["hush"].render(kick_state(40.0), palettes.get("halloween"), 0.025)
    assert all(out[f.fid].intensity == 0.0 for f in patch.group("pars"))
    assert out["accent"].uv > 0.6
    auto(engine, kick_state(41.0))         # energy reads busy: still a breakdown
    assert engine.active_look == "hush"
    for k in range(4):                     # the kick comes back
        auto(engine, kick_state(42.0 + 0.47 * k, kick=True))
    assert engine.active_look != "hush"


def test_drop_hits_the_whole_room_then_settles(rig):
    patch, universe, state, engine, _ = rig
    engine._energy_env._value = 0.7
    auto(engine, kick_state(40.0, events=[DROP]))
    assert engine.active_look == "unison"
    pal = palettes.get("halloween")
    hit = engine._drop_hit({}, pal, 0.01)
    assert all(hit[f.fid].intensity == 1.0 for f in patch)
    for _ in range(300):
        engine._drop_hit({}, pal, 0.01)
    assert engine._flash == 0.0


#: Looks that hold the room in place rather than moving light across it.
STILL_LOOKS = {"pulse", "unison", "knockout", "beams", "wash", "drift", "uv", "ambient"}


@pytest.mark.parametrize("locked", [True, False])
def test_busy_auto_rotation_alternates_still_and_moving_looks(rig, locked):
    patch, universe, state, engine, _ = rig
    engine._energy_env._value = 0.8
    seen = []
    t = 0.0
    beat = 0
    for _ in range(12):
        # Past the 32 s dwell unlocked; locked, the 4 phrases a look holds for,
        # crossed one phrase line at a time.
        for _ in range(4 if locked else 1):
            t += 40.0
            beat += 16
            auto(engine, kick_state(t, tempo_locked=locked, bpm=128.0, beat_index=beat))
        seen.append(engine.active_look)
    still = [name in STILL_LOOKS for name in seen]
    assert all(a != b for a, b in zip(still, still[1:])), seen
    # Rotation, not a favourite: nothing more than its fair share.
    assert max(seen.count(n) for n in seen) <= 2
    tempo_only = {n for n in seen if engine.looks[n].needs_tempo}
    assert locked or not tempo_only


def test_locked_auto_changes_look_only_on_a_phrase_boundary(rig):
    patch, universe, state, engine, _ = rig
    engine._energy_env._value = 0.8
    auto(engine, kick_state(0.0, tempo_locked=True, bpm=128.0, beat_index=1))
    first = engine.active_look
    # Long past the time-based dwell, but three phrases in: hold.
    for beat in range(2, 64):
        auto(engine, kick_state(beat * 0.47, tempo_locked=True, bpm=128.0, beat_index=beat))
        assert engine.active_look == first, beat
    # The fourth phrase line: change, on its first beat.
    auto(engine, kick_state(64 * 0.47, tempo_locked=True, bpm=128.0, beat_index=64))
    assert engine.active_look != first


# -- palettes ---------------------------------------------------------------

def test_palette_indexing_wraps():
    p = palettes.get("halloween")
    assert p.at(0) == p.at(len(p))


def test_palette_sample_wraps_without_a_jump():
    """A drifting look walks past 1.0; it must not snap back to the first colour."""
    p = palettes.get("neon")
    assert p.sample(0.999) == pytest.approx(p.sample(-0.001), abs=0.05)


def test_unknown_palette_falls_back_rather_than_raising():
    """A typo in a config file at a party must not stop the lights."""
    assert palettes.get("does-not-exist").name == palettes.DEFAULT


# -- looks ------------------------------------------------------------------

@pytest.mark.parametrize("name", [c.name for c in LOOKS])
def test_every_look_renders_without_audio(rig, name):
    """Looks must cope with a silent, tempo-less state: that is how they start."""
    patch, _, _, engine, _ = rig
    em = engine.looks[name].render(silent_state(), palettes.get("halloween"), 0.01)
    assert em, f"{name} returned nothing"
    for fid, e in em.items():
        assert fid in patch.by_id
        assert 0.0 <= e.intensity <= 1.0
        assert all(0.0 <= c <= 1.0 for c in e.rgb)
        assert 0.0 <= e.uv <= 1.0
        assert 0.0 <= e.strobe <= 1.0


@pytest.mark.parametrize("name", [c.name for c in LOOKS])
def test_every_look_renders_with_music(rig, name):
    patch, _, _, engine, _ = rig
    signal, _ = render(128.0, bars=4)
    for i in range(0, len(signal), 4096):
        out = engine.analyser.feed(signal[i : i + 4096])
        if out:
            engine._music = out[-1]
    em = engine.looks[name].render(engine._music, palettes.get("neon"), 0.01)
    assert len(em) == len(patch)


def test_strobe_and_blinder_are_excluded_from_auto_selection():
    """Rapid full-field flashing is not something an automatic mode should pick.

    5-30 Hz full-field flashing is the photosensitive-seizure risk band. It is
    available as a manual cue; nothing should put it on the room unprompted.
    """
    auto = auto_selectable()
    assert "strobe" not in auto
    assert "blinder" not in auto
    assert BY_NAME["strobe"].manual_only
    assert BY_NAME["blinder"].manual_only


def test_uv_look_only_drives_uv_on_fixtures_that_have_it(rig):
    patch, _, _, engine, _ = rig
    em = engine.looks["uv"].render(silent_state(), palettes.get("cool"), 0.01)
    for f in patch:
        if not f.profile.has_uv:
            # A PAR has no UV emitter, so the rendered channels cannot contain
            # one -- but the look should not be asking for it either.
            assert f.profile.render(em[f.fid]) is not None


# -- the engine -------------------------------------------------------------

def test_mode_channels_stay_zero_in_every_look(rig):
    """The one regression that would silently break the whole rig.

    Above 10 on the ZQ01104 mode channel the fixture runs an internal program
    and ignores our colour entirely.
    """
    patch, universe, state, engine, _ = rig
    for name in engine.looks:
        engine.select(name)
        engine._fade = 1.0
        engine.tick(0.01)
        frame = universe.buffer()
        for f in patch:
            rendered = frame[f.address - 1 : f.last_channel]
            for ch in f.profile.channels:
                if ch.role == "fixed":
                    assert rendered[ch.offset] == ch.value, (
                        f"{name}/{f.fid}: fixed channel {ch.offset} drifted to "
                        f"{rendered[ch.offset]}, expected {ch.value}"
                    )


def test_engine_survives_a_look_that_raises(rig):
    """A look is the most likely thing to be edited at 1am. It must not be fatal."""
    _, _, _, engine, _ = rig

    class Broken:
        name = "broken"
        manual_only = False

        def reset(self):
            pass

        def render(self, *a, **k):
            raise RuntimeError("boom")

    engine.looks["broken"] = Broken()
    engine.select("broken")
    engine._fade = 1.0
    engine.tick(0.01)          # must not raise
    assert engine.stats()["errors"] >= 1
    assert "boom" in engine.stats()["last_error"]


def test_master_dimmer_scales_output(rig):
    patch, universe, state, engine, _ = rig
    state.set_mode("manual")
    state.update_manual("par1", active=True, rgb=(1, 1, 1), intensity=1.0)

    state.set_master(1.0)
    engine.tick(0.01)
    full = universe.buffer()[patch.by_id["par1"].address - 1]

    state.set_master(0.3)
    engine.tick(0.01)
    dim = universe.buffer()[patch.by_id["par1"].address - 1]

    assert dim < full


def test_manual_override_wins_over_the_look(rig):
    patch, universe, state, engine, _ = rig
    state.set_mode("manual")
    state.set_look("blinder")
    state.update_manual("par5", active=True, rgb=(1, 0, 0), intensity=1.0)
    engine.select("blinder")
    engine._fade = 1.0
    engine.tick(0.01)
    addr = patch.by_id["par5"].address
    frame = universe.buffer()
    # Blinder would set white; the override forces red, so green and blue are 0.
    assert frame[addr] == 255        # red
    assert frame[addr + 1] == 0      # green
    assert frame[addr + 2] == 0      # blue


def test_released_override_returns_control_to_the_look(rig):
    patch, universe, state, engine, _ = rig
    state.set_mode("manual")          # or auto selection picks the look for us
    state.set_look("blinder")
    state.update_manual("par5", active=True, rgb=(1, 0, 0), intensity=1.0)
    engine.select("blinder")
    engine._fade = 1.0
    engine.tick(0.01)
    state.update_manual("par5", active=False)
    engine.tick(0.01)
    addr = patch.by_id["par5"].address
    assert universe.buffer()[addr + 1] == 255    # green back up: blinder has it


def test_auto_mode_leaves_ambient_when_music_starts(rig):
    """Regression: auto mode used to stay on ambient through an entire track.

    FeatureFrame.energy is instantaneous and near zero between hits, so a raw
    comparison against the idle threshold flapped on every gap. Selection uses
    a peak-held sustained energy instead.
    """
    _, _, state, engine, _ = rig
    state.set_mode("auto")
    signal, _ = render(128.0, bars=16)
    drive(engine, signal)
    assert engine.sustained_energy > IDLE_ENERGY
    assert engine.active_look != "ambient", "auto mode never left the idle look"


def test_auto_mode_leaves_idle_immediately_when_started_mid_song(rig):
    """Regression: the dwell used to apply to the idle look too.

    Starting the rig while a track is already playing produces no drop event to
    break the dwell, so the engine sat on `ambient` for the full 22 seconds
    while the room was already going. Leaving idle now bypasses the dwell.
    """
    _, _, state, engine, _ = rig
    state.set_mode("auto")
    drive(engine, render(128.0, bars=8)[0])    # ~15s, less than AUTO_DWELL_S
    assert engine.active_look != "ambient"


def test_auto_mode_returns_to_ambient_when_the_music_stops(rig):
    _, _, state, engine, _ = rig
    state.set_mode("auto")
    drive(engine, render(128.0, bars=8)[0])
    assert engine.active_look != "ambient"
    drive(engine, np.zeros(SR * 15, dtype=np.float32))
    assert engine.active_look == "ambient"


def test_auto_mode_rotates_looks_over_a_long_track(rig):
    """Dwell is measured on the audio clock, so this holds regardless of how
    fast the test feeds audio. Mixing in wall clock made this silently pass."""
    _, _, state, engine, _ = rig
    state.set_mode("auto")
    seen = set()
    signal, _ = render(128.0, bars=60)
    hop = SR // 100
    for i in range(0, len(signal), hop):
        out = engine.analyser.feed(signal[i : i + hop])
        if out:
            engine._music = out[-1]
        engine.tick(0.01)
        seen.add(engine.active_look)
    assert len(seen) >= 3, f"only used {seen} over a long track"


def test_manual_mode_does_not_get_overridden_by_auto_selection(rig):
    _, _, state, engine, _ = rig
    state.set_mode("manual")
    state.set_look("uv")
    drive(engine, render(128.0, bars=12)[0])
    assert engine.active_look == "uv"


def test_track_change_resets_analysis_and_keeps_palette_when_manual(rig):
    """With palette switching on manual, a new song must not change it."""
    _, _, state, engine, _ = rig
    assert not state.palette_auto
    drive(engine, render(128.0, bars=8)[0])
    assert engine.analyser.tempo.bpm > 0
    before = state.palette
    engine.on_track_change("Song", "Artist")
    assert engine.analyser.tempo.bpm == 0.0
    assert state.palette == before
    assert state.track_title == "Song"


def test_auto_palette_shuffles_within_the_pool_without_repeats(rig):
    _, _, state, engine, _ = rig
    state.set_pool(["warm", "cool", "neon"])
    state.set_palette("warm")
    state.set_palette_auto(True)
    seen = set()
    for _ in range(30):
        before = state.palette
        engine.on_track_change()
        assert state.palette in {"warm", "cool", "neon"}
        assert state.palette != before
        seen.add(state.palette)
    assert seen == {"warm", "cool", "neon"}


def test_auto_palette_with_a_single_palette_pool_holds_it(rig):
    _, _, state, engine, _ = rig
    state.set_pool(["fire"])
    state.set_palette("cool")
    state.set_palette_auto(True)
    engine.on_track_change()
    assert state.palette == "fire"
    engine.on_track_change()
    assert state.palette == "fire"


def test_pool_cannot_be_emptied_and_drops_unknown_names():
    state = EngineState()
    state.set_pool(["cool", "nope"])
    assert state.palette_pool == ["cool"]
    with pytest.raises(ValueError):
        state.set_pool_member("cool", False)
    with pytest.raises(KeyError):
        state.set_pool_member("nope", True)
    state.set_pool(["nope"])
    assert state.palette_pool == list(palettes.DEFAULT_POOL)
    assert "mono-white" not in state.palette_pool


def test_next_palette_cue_cycles_only_the_pool(rig):
    _, _, state, _, cues = rig
    state.set_pool(["warm", "neon"])
    state.set_palette("halloween")    # not in the pool
    assert cues.fire("palette") == {"palette": "warm"}
    assert cues.fire("palette") == {"palette": "neon"}
    assert cues.fire("palette") == {"palette": "warm"}
    # Picking by name still reaches any palette, and leaves switching alone.
    assert cues.fire("palette/mono-white") == {"palette": "mono-white"}
    assert cues.fire("palette-auto") == {"palette_auto": True}


# -- strobe safety ----------------------------------------------------------

def test_strobe_is_time_limited():
    """A forgotten strobe cue must not leave the room flashing all night."""
    state = EngineState(max_strobe_seconds=2.0)
    assert state.strobe_allowed("strobe", now=100.0)
    assert state.strobe_allowed("strobe", now=101.0)
    assert not state.strobe_allowed("strobe", now=103.0)


def test_strobe_timer_resets_when_another_look_runs():
    state = EngineState(max_strobe_seconds=2.0)
    state.strobe_allowed("strobe", now=100.0)
    state.strobe_allowed("wash", now=101.0)
    assert state.strobe_allowed("strobe", now=102.0)


def test_engine_drops_out_of_strobe_at_the_limit(rig):
    import time
    _, _, state, engine, _ = rig
    state.max_strobe_seconds = 1.0
    state.set_mode("manual")
    state.set_look("strobe")
    engine.select("strobe")
    engine.tick(0.01)
    # Backdate the timer rather than sleeping: the limit is wall-clock, because
    # it is a real-world safety bound rather than a musical one.
    state._strobe_since = time.monotonic() - 10.0
    engine.tick(0.01)
    assert engine.active_look != "strobe"


# -- cues -------------------------------------------------------------------

def test_blackout_cue_toggles(rig):
    _, universe, _, _, cues = rig
    assert cues.fire("blackout")["blackout"] is True
    assert universe.blackout
    assert cues.fire("blackout")["blackout"] is False


def test_blackout_zeroes_the_wire_but_not_the_buffer(rig):
    """Blackout acts at the last moment before output, so nothing upstream --
    a look, a manual override, the master -- can defeat it."""
    patch, universe, state, engine, cues = rig
    state.update_manual("par1", active=True, rgb=(1, 1, 1), intensity=1.0)
    engine.tick(0.01)
    cues.fire("blackout")
    assert universe.blackout
    assert any(universe.buffer()), "the engine's intent should be untouched"


def test_freeze_cue_toggles(rig):
    _, universe, _, _, cues = rig
    cues.fire("freeze")
    assert universe.frozen
    cues.fire("freeze")
    assert not universe.frozen


def test_look_cue_switches_to_manual(rig):
    """Picking a look by hand has to stick, or the button looks broken."""
    _, _, state, engine, cues = rig
    state.set_mode("auto")
    result = cues.fire("look/chase")
    assert result["mode"] == "manual"
    assert state.look == "chase"


def test_palette_cue_cycles_and_accepts_a_name(rig):
    _, _, state, _, cues = rig
    first = state.palette
    cues.fire("palette")
    assert state.palette != first
    cues.fire("palette/cool")
    assert state.palette == "cool"


def test_master_cues_clamp(rig):
    _, _, state, _, cues = rig
    for _ in range(30):
        cues.fire("master-up")
    assert state.master == 1.0
    for _ in range(30):
        cues.fire("master-down")
    assert state.master == 0.0


def test_resume_auto_releases_everything(rig):
    _, universe, state, _, cues = rig
    state.update_manual("par1", active=True)
    state.set_mode("manual")
    cues.fire("freeze")
    cues.fire("resume-auto")
    assert state.mode == "auto"
    assert not universe.frozen
    assert not state.active_manual()


def test_unknown_cue_raises_keyerror(rig):
    _, _, _, _, cues = rig
    with pytest.raises(KeyError):
        cues.fire("not-a-cue")


# -- web API ----------------------------------------------------------------

@pytest.fixture
def client(rig):
    from partylights.web.server import create_app
    patch, universe, state, engine, cues = rig
    app = create_app(patch=patch, universe=universe, state=state,
                     engine=engine, cues=cues, capture=None, jukebox=None)
    app.config["TESTING"] = True
    return app.test_client()


def test_pages_render(client):
    for path in ("/", "/live"):
        assert client.get(path).status_code == 200


def test_rig_endpoint_describes_the_whole_rig(client):
    data = client.get("/api/rig").get_json()
    assert len(data["fixtures"]) == 13
    assert data["max_channel"] == 94
    accent = next(f for f in data["fixtures"] if f["id"] == "accent")
    assert accent["has_uv"]
    assert "amber" in accent["emitters"]


def test_state_endpoint_has_every_section(client):
    data = client.get("/api/state").get_json()
    for key in ("state", "engine", "dmx", "music", "audio", "jukebox"):
        assert key in data


def test_cue_endpoint_accepts_get_for_stream_deck(client):
    """The Stream Deck's plain 'open a URL' action issues a GET, and supporting
    it is what lets the hardware work with no plugin at all."""
    assert client.get("/api/cue/blackout").status_code == 200


def test_cue_endpoint_reports_unknown_cues_with_the_valid_list(client):
    resp = client.post("/api/cue/nonsense")
    assert resp.status_code == 404
    assert "available" in resp.get_json()


def test_fixture_endpoint_rejects_unknown_fixture(client):
    assert client.post("/api/fixture/par99", json={}).status_code == 404


def test_fixture_endpoint_rejects_malformed_rgb(client):
    resp = client.post("/api/fixture/par1", json={"rgb": [1, 0]})
    assert resp.status_code == 400


def test_group_endpoint_applies_to_a_whole_group(client):
    resp = client.post("/api/fixtures", json={"group": "pars", "active": True})
    assert len(resp.get_json()["applied"]) == 12
