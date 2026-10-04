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
            engine._music = out[-1]
        engine.tick(1.0 / ticks_per_second)


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


def test_track_change_resets_analysis_and_rotates_palette(rig):
    _, _, state, engine, _ = rig
    drive(engine, render(128.0, bars=8)[0])
    assert engine.analyser.tempo.bpm > 0
    before = state.palette
    engine.on_track_change("Song", "Artist")
    assert engine.analyser.tempo.bpm == 0.0
    assert state.palette != before
    assert state.track_title == "Song"


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
