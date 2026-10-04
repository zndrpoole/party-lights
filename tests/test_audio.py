"""Tests for the audio analysis chain.

Most of these are regression guards on bugs that were genuinely hard to see.
Every one of them produced plausible-looking output while being wrong, which is
the dangerous failure mode for signal processing: nothing crashes, the numbers
look reasonable, and the lights just do not quite follow the music.

Ground truth comes from tools/make_test_audio.py, which renders band-limited
drums at a known tempo with known hit counts.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partylights.audio.analyser import TEMPO_SALIENCE_WEIGHTS, Analyser
from partylights.audio.capture import RingBuffer
from partylights.audio.features import (
    BAND_FLOOR_RMS, Envelope, FeatureExtractor, OnsetDetector, PeakFollower,
)
from partylights.audio.structure import DROP, StructureTracker, _shape_vector
from partylights.audio.tempo import TempoTracker
from tools.make_test_audio import SR, render

SENSITIVITY = 2.2


def analyse(signal):
    a = Analyser(sample_rate=SR, onset_sensitivity=SENSITIVITY)
    states = []
    for i in range(0, len(signal), 4096):
        states.extend(a.feed(signal[i : i + 4096]))
    return a, states


@pytest.fixture(scope="module")
def track_128():
    return render(128.0, bars=16)


# -- ring buffer ------------------------------------------------------------

def test_ring_buffer_returns_most_recent_samples():
    rb = RingBuffer(100)
    rb.push(np.arange(10, dtype=np.float32))
    assert rb.latest(4).tolist() == [6, 7, 8, 9]


def test_ring_buffer_wraps_correctly():
    rb = RingBuffer(10)
    rb.push(np.arange(16, dtype=np.float32))
    assert rb.latest(4).tolist() == [12, 13, 14, 15]
    assert len(rb.latest(10)) == 10


def test_ring_buffer_delay_reads_older_audio():
    """The delay parameter is how output-delay compensation is implemented."""
    rb = RingBuffer(100)
    rb.push(np.arange(20, dtype=np.float32))
    assert rb.latest(2, delay=5).tolist() == [13, 14]


def test_ring_buffer_handles_block_larger_than_capacity():
    rb = RingBuffer(8)
    rb.push(np.arange(20, dtype=np.float32))
    assert rb.latest(8).tolist() == [12, 13, 14, 15, 16, 17, 18, 19]


# -- followers --------------------------------------------------------------

def test_peak_follower_rises_instantly_and_decays_slowly():
    pf = PeakFollower(half_life_s=1.0, rate_hz=100.0, floor=1e-6)
    pf.update(1.0)
    assert pf.value == pytest.approx(1.0)
    for _ in range(100):  # one half-life
        pf.update(0.0)
    assert 0.4 < pf.value < 0.6


def test_peak_follower_never_goes_below_floor():
    pf = PeakFollower(half_life_s=0.01, rate_hz=100.0, floor=0.25)
    for _ in range(1000):
        pf.update(0.0)
    assert pf.value == pytest.approx(0.25)


def test_envelope_attacks_faster_than_it_releases():
    """Lighting needs a fast attack and a slow release; symmetric looks wrong."""
    env = Envelope(attack_s=0.001, release_s=0.5, rate_hz=100.0)
    env.update(1.0)
    after_attack = env.value
    env.update(0.0)
    fell = after_attack - env.value
    assert after_attack > 0.5
    assert fell < 0.1


# -- onset detection: regression on threshold collapse ----------------------

def test_onset_detector_does_not_fire_on_noise_when_input_is_sparse():
    """Regression: the adaptive threshold used to collapse to roughly zero.

    With mostly-silent input the median and MAD of recent flux both go to zero,
    so `median + k * MAD` goes to zero too and any nonzero flux fires. The
    observed symptom was a train of phantom onsets trailing each real hit as its
    decay tail wobbled above a threshold of effectively nothing.
    """
    det = OnsetDetector(sensitivity=2.2, rate_hz=100.0, debounce_s=0.0)
    fired = 0
    for i in range(500):
        # Sparse: a real hit every 100 frames, tiny noise otherwise.
        flux = 1.0 if i % 100 == 0 else 0.001
        if det.update(flux, i / 100.0):
            fired += 1
    # Four, not five: the detector needs 8 frames of history before it will
    # fire at all, so a hit in the first ~85 ms is missed. That is why onset
    # counts against ground truth come in at 31/32 rather than 32/32, and it is
    # the right trade -- the alternative is firing blind at startup.
    assert fired == 4, f"expected the 4 hits after warm-up, got {fired}"


def test_onset_detector_threshold_never_drops_below_min_flux():
    det = OnsetDetector(min_flux=0.3, rate_hz=100.0)
    for i in range(200):
        det.update(0.0, i / 100.0)
    assert det.threshold >= 0.3


def test_onset_detector_ignores_steady_flux():
    """Constant flux is not a stream of onsets -- an onset is a *change*."""
    det = OnsetDetector(sensitivity=2.2, min_flux=0.05, rate_hz=100.0, debounce_s=0.0)
    fired = sum(1 for i in range(300) if det.update(1.0, i / 100.0))
    # Only the first frames, before the history fills, can possibly fire.
    assert fired <= 1


def test_onset_detector_respects_debounce():
    """Hits closer together than the debounce collapse into one."""
    det = OnsetDetector(sensitivity=2.2, min_flux=0.05, rate_hz=100.0, debounce_s=0.5)
    fired = 0
    for i in range(400):
        # A pulse every 100 ms against a quiet floor; debounce is 500 ms, so
        # only about one in five should survive.
        flux = 1.0 if i % 10 == 0 else 0.001
        if det.update(flux, i / 100.0):
            fired += 1
    assert 5 <= fired <= 9, f"expected roughly 4s/0.5s = 8 onsets, got {fired}"


# -- band normalisation: regression on the invented-activity bug -------------

def test_empty_band_reads_near_zero_not_mid_scale():
    """Regression: an empty band used to report about 0.5 instead of 0.

    The band normaliser's floor was 1e-4 (-80 dBFS). When a band carries nothing
    its peak reference decays to the floor, and once the floor sits below the
    noise level, dividing by it amplifies numerical noise up to mid-scale. A
    track whose intro contained only kicks showed 0.5 in the air band.
    """
    # Pure low sine: everything above a few hundred Hz is genuinely empty.
    t = np.arange(SR * 6) / SR
    sig = (np.sin(2 * np.pi * 50.0 * t) * 0.5).astype(np.float32)
    fe = FeatureExtractor(sample_rate=SR)
    frames = []
    for i in range(0, len(sig), 4096):
        frames.extend(fe.feed(sig[i : i + 4096]))
    settled = frames[len(frames) // 2 :]
    for band in ("mid", "highmid", "high", "air"):
        mean = float(np.mean([f.bands[band] for f in settled]))
        assert mean < 0.1, f"{band} reads {mean:.3f} for a signal with no content there"


def test_band_floor_is_a_real_signal_level():
    """Guard the constant itself: an epsilon here reintroduces the bug above."""
    assert BAND_FLOOR_RMS >= 1e-3


# -- tempo: regression on the half-tempo bug --------------------------------

@pytest.mark.parametrize("bpm", [90.0, 110.0, 128.0, 140.0, 174.0])
def test_tempo_is_detected_within_two_percent(bpm):
    signal, _ = render(bpm, bars=16)
    a, states = analyse(signal)
    settled = [s for s in states if s.t > 2.0 and s.bpm > 0]
    assert settled, f"never locked at {bpm} BPM"
    got = float(np.median([s.bpm for s in settled]))
    assert abs(got - bpm) / bpm < 0.02, f"expected {bpm}, got {got:.1f}"


def test_tempo_salience_uses_kick_and_snare_not_kick_alone():
    """Regression: kick-only salience halves the tempo on a backbeat groove.

    With kicks on 1 and 3 only, the kick signal genuinely repeats every two
    beats, so the tracker correctly reports half tempo -- correct about the kick
    pattern, wrong about the beat. Adding snare fixes it because kick and snare
    together mark every beat.
    """
    assert "kick" in TEMPO_SALIENCE_WEIGHTS and "snare" in TEMPO_SALIENCE_WEIGHTS
    assert TEMPO_SALIENCE_WEIGHTS["snare"] > 0

    signal, _ = render(140.0, bars=16)
    fe = FeatureExtractor(sample_rate=SR)
    kick_only = TempoTracker(rate_hz=fe.rate_hz)
    combined = TempoTracker(rate_hz=fe.rate_hz)
    for i in range(0, len(signal), 4096):
        for f in fe.feed(signal[i : i + 4096]):
            onset = f.onsets.get("kick", False)
            kick_only.update(f.flux.get("kick", 0.0), f.t, onset=onset)
            combined.update(
                sum(f.flux.get(k, 0.0) * w for k, w in TEMPO_SALIENCE_WEIGHTS.items()),
                f.t, onset=onset,
            )
    assert abs(combined.bpm - 140.0) / 140.0 < 0.02
    # And confirm the failure mode is real, so this test is testing something.
    assert abs(kick_only.bpm - 70.0) / 70.0 < 0.05


def test_beat_phase_stays_locked_through_the_track(track_128):
    signal, truth = track_128
    a, states = analyse(signal)
    period = 60.0 / 128.0
    # Compare predicted beat times against the real grid, late in the track
    # where the tracker has had time to settle.
    errors = []
    for s in states:
        if s.t > 10.0 and s.beat:
            nearest = round(s.t / period) * period
            errors.append(abs(s.t - nearest))
    assert errors, "no beats fired"
    # Within a tenth of a beat of the true grid.
    assert float(np.median(errors)) < period * 0.1


def test_onset_counts_are_close_to_ground_truth(track_128):
    signal, truth = track_128
    a, states = analyse(signal)
    for region, tolerance in (("kick", 3), ("snare", 3), ("hat", 5)):
        detected = sum(1 for s in states if s.onset(region))
        expected = len(truth[region])
        assert abs(detected - expected) <= tolerance, (
            f"{region}: detected {detected}, expected {expected}"
        )


def test_tempo_resets_on_track_change():
    a = Analyser(sample_rate=SR)
    signal, _ = render(128.0, bars=8)
    for i in range(0, len(signal), 4096):
        a.feed(signal[i : i + 4096])
    assert a.tempo.bpm > 0
    a.on_track_change()
    assert a.tempo.bpm == 0.0


# -- structure: regression on the transform-ordering bug --------------------

def test_shape_vector_separates_sections_linear_energy_cannot():
    """Regression: the log transform is what makes novelty work at all.

    Linear band energy is dominated by the kick in both sections, so two
    sections that sound entirely different produce near-parallel vectors.
    Measured: 0.004 cosine distance linear, 0.24 in dB.
    """
    quiet = np.array([6.8, 4.9, 0.18, 0.001, 0.00025, 0.00017, 0.00015])
    loud = np.array([27.7, 20.1, 3.0, 1.2, 1.18, 1.05, 0.71])

    def cos_dist(a, b):
        return 1.0 - float(a @ b) / (np.linalg.norm(a) * np.linalg.norm(b))

    assert cos_dist(quiet, loud) < 0.02, "sanity: linear really is this bad"
    assert cos_dist(_shape_vector(quiet, 60.0), _shape_vector(loud, 60.0)) > 0.15


def test_shape_vector_is_gain_invariant():
    """Turning the party up must not look like a section change."""
    bands = np.array([1.0, 0.5, 0.2, 0.05, 0.01, 0.005, 0.001])
    a = _shape_vector(bands, 60.0)
    b = _shape_vector(bands * 8.0, 60.0)
    assert np.allclose(a, b)


def test_structure_detects_a_drop():
    intro, _ = render(128.0, bars=8, hats=False, snares=False, level=0.25)
    full, _ = render(128.0, bars=8)
    signal = np.concatenate([intro, full])
    drop_at = len(intro) / SR

    a, states = analyse(signal)
    drops = [s.t for s in states if DROP in s.events]
    assert drops, "no drop detected"
    # Detection lags by about the comparison window: it needs post-drop audio
    # to compare against. Anything inside two seconds is good.
    assert min(abs(t - drop_at) for t in drops) < 2.0


def test_structure_is_quiet_on_uniform_material(track_128):
    """A track that never changes should not keep reporting that it changed."""
    signal, _ = track_128
    a, states = analyse(signal)
    events = [e for s in states for e in s.events]
    assert len(events) <= 3, f"{len(events)} events on uniform material"


def test_structure_resets_on_track_change():
    st = StructureTracker()
    st._level.extend([0.1] * 10)
    st.reset()
    assert len(st._level) == 0
