"""Tests for the listening pass: the song-map analysis and the pass itself.

The analysis tests grade maps of synthetic songs (tools/make_test_song.py)
against their exact truth. Each threshold is what the analysis measured when
it was tuned, with a little room; a change that makes any of them worse is a
change that makes the lights land later or on the wrong bar.

The pass tests drive ListeningPass against a fake Spotify and a fake recorder
on a virtual clock, so a whole song "plays" in milliseconds -- including the
ways real takes go wrong.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partylights.songmap import songmap as sm
from partylights.songmap.beats import analyse_beats
from partylights.songmap.fingerprint import align, build
from partylights.songmap.frames import SR, FrameExtractor, extract
from partylights.songmap.listen import ListeningPass, parse_playlist
from partylights.songmap.recorder import Take
from tools.make_test_song import EDM, POP, render_song

SHORT = (("intro", 4), ("verse", 8), ("chorus", 8), ("outro", 4))


@pytest.fixture(scope="module")
def songs():
    out = {}
    for name, form, bpm, ending in (("pop118", POP, 118.0, "hard"), ("pop96", POP, 96.0, "fade"),
                                    ("edm128", EDM, 128.0, "hard"), ("edm174", EDM, 174.0, "hard")):
        sig, truth = render_song(form, bpm, lead=0.5, ending=ending)
        out[name] = (sig, truth, sm.analyse(sig))
    return out


def _nearest(found, truth):
    found = np.asarray(found)
    return np.array([found[np.argmin(np.abs(found - t))] - t for t in truth])


# -- beats ---------------------------------------------------------------------

@pytest.mark.parametrize("name", ["pop118", "pop96", "edm128", "edm174"])
def test_beats_land_on_the_beat(songs, name):
    _, truth, (m, _, _) = songs[name]
    assert m["timing"]["bpm"] == pytest.approx(truth["bpm"], abs=0.1)
    err = _nearest(m["timing"]["beats"], truth["beats"])
    within = np.abs(err) < 0.030
    # pop96 fades out: the last bars of the fade are too quiet to call.
    assert within.mean() >= (0.95 if name == "pop96" else 0.99), name
    # Bias guard (ONSET_BIAS_S): beats are when the drum is heard.
    assert abs(np.median(err[within])) < 0.004, name


@pytest.mark.parametrize("name", ["pop118", "edm128", "edm174"])
def test_bars_start_on_the_one(songs, name):
    _, truth, (m, _, _) = songs[name]
    beats = np.array(m["timing"]["beats"])
    bars = beats[m["timing"]["downbeats"]]
    err = _nearest(bars, truth["downbeats"])
    assert (np.abs(err) < 0.05).mean() >= 0.97, name


def test_a_quiet_intro_is_not_tracked_on_the_offbeat(songs):
    # The EDM intro is pads and offbeat hats: nothing on the beat at all.
    _, truth, (m, _, _) = songs["edm128"]
    verse = truth["sections"][1]["start"]
    intro = [t for t in truth["beats"] if t < verse]
    err = _nearest(m["timing"]["beats"], intro)
    assert np.median(np.abs(err)) < 0.01


# -- sections and moments ------------------------------------------------------------

@pytest.mark.parametrize("name", ["pop118", "edm128"])
def test_every_section_boundary_is_found_to_the_bar(songs, name):
    _, truth, (m, _, _) = songs[name]
    bar = 240.0 / truth["bpm"]
    found = [s["start"] for s in m["sections"]]
    err = _nearest(found, [s["start"] for s in truth["sections"]])
    assert (np.abs(err) < bar / 2).all(), name


def _role_at(m, t):
    return min(m["sections"], key=lambda s: abs(s["start"] - t))["role"]


def test_pop_roles(songs):
    _, truth, (m, _, _) = songs["pop118"]
    for s in truth["sections"]:
        assert _role_at(m, s["start"]) == s["role"], s


def test_edm_roles_drops_builds_and_gaps(songs):
    _, truth, (m, _, _) = songs["edm128"]
    for s in truth["sections"]:
        assert _role_at(m, s["start"]) == s["role"], s
    drops = m["moments"]["drops"]
    assert _nearest([d["time"] for d in drops], truth["drops"]) == pytest.approx(0, abs=0.03)
    assert all(d["kind"] == "drop" and d["after_gap"] for d in drops)
    builds = m["moments"]["builds"]
    assert [round(b["end"], 1) for b in builds] == [round(t, 1) for t in truth["drops"]]
    gaps = m["moments"]["gaps"]
    assert len(gaps) == 2
    assert _nearest([g["start"] for g in gaps], [g["start"] for g in truth["gaps"]]) \
        == pytest.approx(0, abs=0.05)


def test_a_stop_and_slam_is_heard(songs):
    _, truth, (m, _, _) = songs["pop118"]
    slams = [d for d in m["moments"]["drops"] if d["kind"] == "slam"]
    assert len(slams) == 1 and slams[0]["time"] == pytest.approx(truth["stabs"][0], abs=0.03)


def test_endings(songs):
    _, truth, (m, _, _) = songs["pop118"]
    end = m["moments"]["ending"]
    assert end["type"] == "hard" and end["final_hit"] == pytest.approx(truth["final_hit"], abs=0.03)
    _, truth, (m, _, _) = songs["pop96"]
    end = m["moments"]["ending"]
    assert end["type"] == "fade" and end["fade_start"] == pytest.approx(truth["fade_start"], abs=1.0)


# -- the front end -------------------------------------------------------------------

def test_streaming_frames_match_one_shot():
    """The live aligner feeds blocks as they arrive; the pass feeds a whole
    song. Both must produce the same frames, or live and map disagree."""
    sig, _ = render_song(SHORT, 120.0)
    whole = extract(sig[: SR * 10])
    ex = FrameExtractor()
    rng = np.random.default_rng(0)
    parts, i = [], 0
    while i < SR * 10:
        n = int(rng.integers(100, 9000))
        parts.append(ex.feed(sig[i:min(i + n, SR * 10)]))
        i += n
    streamed = sm.Frames.concat([p for p in parts if len(p)])
    assert streamed.t0 == pytest.approx(whole.t0)
    assert len(streamed) == len(whole)
    assert np.allclose(streamed.onset, whole.onset, atol=1e-4)
    assert np.allclose(streamed.mel, whole.mel, atol=1e-3)


def test_stored_frames_reanalyse_the_same(songs, tmp_path):
    _, _, (m, frames, fp) = songs["edm128"]
    store = sm.Store(tmp_path)
    store.save("x", m, frames, fp)
    again = sm.analyse_frames(sm.load_frames(store.frames / "x.npz"))
    a, b = np.array(m["timing"]["beats"]), np.array(again["timing"]["beats"])
    assert len(a) == len(b) and np.abs(a - b).max() < 0.005
    assert [s["start"] for s in m["sections"]] == [s["start"] for s in again["sections"]]
    assert store.fingerprint("x").data.shape == fp.data.shape


# -- fingerprints -----------------------------------------------------------------------

REAL = [Path.home() / "Desktop" / n for n in ("sample.wav", "sample2.wav")]


@pytest.mark.skipif(not all(p.exists() for p in REAL), reason="real captures not present")
@pytest.mark.parametrize("path", REAL)
def test_alignment_on_real_music_is_millisecond_accurate(path):
    from tools.analyze_file import decode
    from scipy.signal import butter, sosfilt
    x = decode(path)
    fp = build(extract(x))
    rng = np.random.default_rng(1)
    lp = butter(6, 11000 / (SR / 2), output="sos")
    for k in range(10):
        start = rng.uniform(2, len(x) / SR - 8)
        seg = x[int(start * SR):int((start + 6) * SR)]
        if k % 2:
            seg = sosfilt(lp, seg) * 0.4      # a lower bitrate, quieter, on the night
        m = align(fp, extract(seg, t0=50.0))  # no hint at all
        assert m.confident
        assert abs(m.offset - (start - 50.0)) < 0.005


def test_alignment_with_a_hint_rarely_trusts_a_wrong_bar():
    """Loop-built music repeats bars exactly; the hint from Spotify's position
    breaks the tie. Wrong answers must almost never claim confidence."""
    sig, _ = render_song(EDM, 128.0)
    fp = build(extract(sig))
    rng = np.random.default_rng(2)
    confident = bad = 0
    for _ in range(60):
        start = rng.uniform(2, len(sig) / SR - 8)
        seg = sig[int(start * SR):int((start + 6) * SR)]
        m = align(fp, extract(seg, t0=0.0), around=start + rng.normal(0, 0.4), sigma=0.5)
        ok = abs(m.offset - start) < 0.03
        confident += m.confident
        bad += m.confident and not ok
    assert confident >= 45 and bad <= 1


# -- the pass, on a virtual clock -----------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class FakeSpotify:
    """Plays one song on the virtual clock. Its reported position runs
    `bias` ahead of the audio, with `jitter` of noise per poll, like the
    real one. `script` can make things go wrong at a given song time."""

    def __init__(self, clock, duration, *, latency=0.35, bias=0.0, jitter=0.06,
                 script=None, autoplay=False):
        self.clock, self.duration = clock, duration
        self.latency, self.bias, self.jitter = latency, bias, jitter
        self.script = script or {}
        self.autoplay = autoplay
        self.start = None
        self.plays = 0
        self.rng = np.random.default_rng(3)
        self.track = {"id": "t1", "uri": "spotify:track:t1", "name": "Song",
                      "artists": [{"name": "Band", "id": "a1"}], "duration_ms": int(duration * 1000),
                      "album": {"id": "al1", "name": "Album", "images": []}}

    def play(self, uri, device):
        self.plays += 1
        self.start = self.clock.t + self.latency

    def pause(self, device=None):
        pass

    def state(self):
        if self.start is None or self.clock.t < self.start:
            return None
        pos = self.clock.t - self.start
        for at, what in self.script.items():
            if pos >= at and self.plays == 1:
                if what == "hijack":
                    return {"is_playing": True, "progress_ms": 1000,
                            "item": {"id": "other", "name": "Other", "artists": []}}
                if what == "seek":
                    pos += 20.0
        if pos >= self.duration:
            if self.autoplay:
                return {"is_playing": True, "progress_ms": int((pos - self.duration) * 1000),
                        "item": {"id": "next", "name": "Next", "artists": []}}
            return {"is_playing": False, "progress_ms": int(self.duration * 1000),
                    "item": self.track}
        noisy = pos + self.bias + self.rng.normal(0, self.jitter)
        return {"is_playing": True, "progress_ms": int(noisy * 1000), "item": self.track}

    def artist(self, aid):
        return None

    def download(self, url, dest):
        return False

    def no_repeat_no_shuffle(self, device):
        pass


class FakeRecorder:
    """Hears whatever FakeSpotify plays, sample-exact on the virtual clock."""

    device_name = "BlackHole 2ch (fake)"

    def __init__(self, clock, sp, song, *, overflows=0, silent=False, next_song=None):
        self.clock, self.sp, self.song = clock, sp, song
        self.overflows, self.silent, self.next_song = overflows, silent, next_song

    def arm(self, max_seconds):
        self.t_arm = self.clock.t

    def disarm(self):
        n = int((self.clock.t - self.t_arm) * SR)
        out = np.zeros(n, dtype=np.float32)
        if not self.silent and self.sp.start is not None:
            i0 = int(round((self.sp.start - self.t_arm) * SR))
            seg = self.song[: max(0, n - i0)]
            out[i0:i0 + len(seg)] = seg
            if self.next_song is not None and self.sp.autoplay:
                j0 = i0 + len(self.song)
                nxt = self.next_song[: max(0, n - j0)]
                out[j0:j0 + len(nxt)] = nxt
        stamps = np.arange(512, n, 512)
        clock = np.column_stack((self.t_arm + stamps / SR
                                 + np.random.default_rng(4).normal(0, 0.001, len(stamps)), stamps))
        return Take(out, clock, self.overflows, SR)


def _pass(tmp_path, clock, sp, rec):
    lines = []
    lp = ListeningPass(sp, rec, sm.Store(tmp_path), jukebox_dir=tmp_path, out=lines.append,
                       now=clock.now, sleep=clock.sleep)
    lp.device_id = "dev"
    return lp, lines


@pytest.fixture(scope="module")
def short_song():
    sig, truth = render_song(SHORT, 120.0, lead=0.0)
    return sig, truth


def test_a_take_is_placed_on_song_time(tmp_path, short_song):
    sig, truth = short_song
    clock = Clock()
    sp = FakeSpotify(clock, len(sig) / SR)
    lp, lines = _pass(tmp_path, clock, sp, FakeRecorder(clock, sp, sig))
    res = lp.run([sp.track])
    assert res["mapped"] == 1
    m = lp.store.load("t1")
    # Spotify's position jitters by 60 ms per poll; the map lands far closer.
    err = _nearest(m["timing"]["beats"], truth["beats"])
    assert np.median(np.abs(err)) < 0.02
    assert m["capture"]["takes"] == 1 and m["track"]["artists"] == ["Band"]
    assert (tmp_path / "fingerprints" / "t1.npz").exists()
    assert (tmp_path / "frames" / "t1.npz").exists()
    # Run again: already mapped, nothing played.
    sp.plays = 0
    lp.run([sp.track])
    assert sp.plays == 0


def test_a_bias_in_spotifys_position_carries_into_the_map(tmp_path, short_song):
    """Map times are Spotify's song time. If Spotify's clock leads the audio,
    the map leads with it -- consistently, which is what the live hint needs."""
    sig, truth = short_song
    clock = Clock()
    sp = FakeSpotify(clock, len(sig) / SR, bias=0.2)
    lp, _ = _pass(tmp_path, clock, sp, FakeRecorder(clock, sp, sig))
    lp.run([sp.track])
    err = _nearest(lp.store.load("t1")["timing"]["beats"], truth["beats"])
    assert np.median(err) == pytest.approx(0.2, abs=0.02)


@pytest.mark.parametrize("what", ["hijack", "seek"])
def test_an_interrupted_take_is_played_again(tmp_path, short_song, what):
    sig, _ = short_song
    clock = Clock()
    sp = FakeSpotify(clock, len(sig) / SR, script={10.0: what})
    lp, lines = _pass(tmp_path, clock, sp, FakeRecorder(clock, sp, sig))
    res = lp.run([sp.track])
    assert res["mapped"] == 1 and sp.plays == 2
    assert lp.store.load("t1")["capture"]["takes"] == 2
    assert any("playing it again" in l for l in lines)


def test_lost_samples_mean_a_retake(tmp_path, short_song):
    sig, _ = short_song
    clock = Clock()
    sp = FakeSpotify(clock, len(sig) / SR)
    rec = FakeRecorder(clock, sp, sig, overflows=1)
    lp, _ = _pass(tmp_path, clock, sp, rec)
    res = lp.run([sp.track])
    assert res["mapped"] == 0 and sp.plays == 3
    assert "overflow" in res["failed"][0][1]


def test_silence_stops_the_whole_pass(tmp_path, short_song):
    sig, _ = short_song
    clock = Clock()
    sp = FakeSpotify(clock, len(sig) / SR)
    lp, _ = _pass(tmp_path, clock, sp, FakeRecorder(clock, sp, sig, silent=True))
    with pytest.raises(SystemExit, match="BlackHole"):
        lp.run([sp.track])


def test_autoplay_does_not_leak_into_the_map(tmp_path, short_song):
    sig, _ = short_song
    loud_next = (np.random.default_rng(9).normal(0, 0.5, SR * 10)).astype(np.float32)
    clock = Clock()
    sp = FakeSpotify(clock, len(sig) / SR, autoplay=True)
    lp, _ = _pass(tmp_path, clock, sp, FakeRecorder(clock, sp, sig, next_song=loud_next))
    lp.run([sp.track])
    m = lp.store.load("t1")
    assert m["capture"]["followed_by_autoplay"]
    assert m["moments"]["ending"]["type"] == "hard"
    assert m["moments"]["ending"]["end"] < len(sig) / SR + 0.1


def test_playlist_links_parse():
    pid = "37i9dQZF1DXcBWIGoYBM5M"
    assert parse_playlist(f"https://open.spotify.com/playlist/{pid}?si=abc") == pid
    assert parse_playlist(f"spotify:playlist:{pid}") == pid
    assert parse_playlist(pid) == pid
    assert parse_playlist("https://open.spotify.com/track/xyz") is None
