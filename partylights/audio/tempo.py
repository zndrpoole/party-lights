"""Tempo estimation and beat phase tracking.

This module is what separates lighting that looks *musical* from lighting that
merely reacts. Reacting to an onset is inherently late: by the time the transient
has been detected, windowed and thresholded, the hit has already happened, and a
fixture that starts fading up then always feels a beat behind.

Tracking phase instead means we know where the next beat *will* be, so a look can
start moving before it lands and arrive on time. That anticipation is the whole
reason to estimate tempo at all.

Two stages:

1. **Period**, by autocorrelating the onset envelope over a few seconds. Peaks
   in the autocorrelation are candidate beat periods.
2. **Phase**, by a phase-locked loop: a free-running clock at the estimated
   period, nudged toward each observed kick. Once locked it rides through a
   sparse passage where onset detection alone would lose the beat entirely.

Octave errors — locking to half or double the real tempo — are the classic
failure here, so candidates are scored against a prior centred on the tempo range
dance music actually occupies.
"""

from __future__ import annotations

import logging
from collections import deque

import numpy as np

log = logging.getLogger(__name__)

#: Comb strength needed to call the tempo locked. A clean drum loop scores
#: ~0.8; a busy bass-heavy mix ~0.1 with the right tempo; noise sits near 0.
LOCK_CONFIDENCE = 0.08
#: Consecutive agreeing estimates needed as well: noise can score a fluke comb
#: peak, but its winning tempo wanders from one estimate to the next.
LOCK_AGREE = 3
#: Period scoring: (multiple of the beat, weight) summed from the ACF.
COMB = ((1, 1.0), (2, 0.6), (4, 0.4))
#: Estimates within this fraction of the current tempo count as the same tempo.
SAME_TEMPO = 0.04
#: Consecutive estimates a different tempo must win before we switch to it.
SWITCH_AFTER = 4
#: How far each estimate pulls the clock toward the measured beat, 0..1.
PHASE_PULL = 0.5
#: Flux peaks the frame after a hit enters the window; compensate.
DETECT_LAG_FRAMES = 1.0


class TempoTracker:
    """Estimates tempo and maintains a beat phase clock.

    `phase` runs 0..1 across one beat, where 0 is the beat itself. Looks should
    generally drive from `phase`, not from onsets, and use onsets for accents.
    """

    def __init__(
        self,
        *,
        rate_hz: float = 93.75,
        min_bpm: float = 70.0,
        max_bpm: float = 180.0,
        history_s: float = 8.0,
        estimate_every_s: float = 0.5,
        prior_bpm: float = 124.0,
        prior_width: float = 0.9,
        phase_gain: float = 0.08,
        period_gain: float = 0.004,
    ):
        self.rate_hz = rate_hz
        self.min_bpm = min_bpm
        self.max_bpm = max_bpm
        self.prior_bpm = prior_bpm
        self.prior_width = prior_width
        self.phase_gain = phase_gain
        self.period_gain = period_gain
        self.estimate_every_s = estimate_every_s

        self._env: deque[float] = deque(maxlen=max(64, int(history_s * rate_hz)))
        self._last_estimate_t = -1e9
        self._last_t: float | None = None

        self.bpm = 0.0
        self.confidence = 0.0
        self.phase = 0.0
        self.beat_index = 0
        self._beat_fired = False
        self._challenger = 0.0
        self._challenger_n = 0
        self._agree = 0

        # Lag search range, in envelope samples.
        self._min_lag = max(2, int(round(60.0 / max_bpm * rate_hz)))
        self._max_lag = int(round(60.0 / min_bpm * rate_hz))

    # -- public state -----------------------------------------------------

    @property
    def beat_period(self) -> float:
        """Seconds per beat, or 0 when we have no estimate."""
        return 60.0 / self.bpm if self.bpm > 0 else 0.0

    @property
    def locked(self) -> bool:
        """Whether phase is trustworthy enough for a look to drive from it."""
        return (self.bpm > 0 and self.confidence >= LOCK_CONFIDENCE
                and self._agree >= LOCK_AGREE)

    @property
    def bar_phase(self) -> float:
        """Position within an assumed 4/4 bar, 0..1.

        Approximate: we count beats from wherever we locked on, so this is a
        consistent four-beat cycle but not necessarily aligned to the real
        downbeat. Good enough to drive slower movement; not to be trusted for
        anything that must land on bar one.
        """
        return ((self.beat_index % 4) + self.phase) / 4.0

    @property
    def phrase_phase(self) -> float:
        """Position within an assumed 16-beat phrase, 0..1. Same caveat."""
        return ((self.beat_index % 16) + self.phase) / 16.0

    def beat_fired(self) -> bool:
        """True on the single frame where phase wrapped past a beat."""
        return self._beat_fired

    def next_beat_in(self) -> float:
        """Seconds until the next predicted beat. The anticipation hook."""
        period = self.beat_period
        return (1.0 - self.phase) * period if period else 0.0

    # -- driving ----------------------------------------------------------

    def update(self, onset_strength: float, t: float, *, onset: bool = False) -> None:
        """Advance the tracker by one analysis frame.

        `onset_strength` is a continuous beat-salience signal (broadband flux);
        `onset` is accepted for compatibility but no longer steers the clock:
        on a bass-heavy mix kick onsets land all over the beat, and correcting
        phase toward them pulled the clock off it.
        """
        self._env.append(float(onset_strength))

        dt = 0.0 if self._last_t is None else max(0.0, t - self._last_t)
        self._last_t = t

        self._beat_fired = False
        period = self.beat_period
        if period > 0:
            # Free-run the clock.
            self.phase += dt / period
            while self.phase >= 1.0:
                self.phase -= 1.0
                self.beat_index += 1
                self._beat_fired = True

        if t - self._last_estimate_t >= self.estimate_every_s and len(self._env) >= self._max_lag * 2:
            self._estimate_period()
            self._correct_phase()
            self._last_estimate_t = t

    # -- period estimation ------------------------------------------------

    def _acf(self) -> np.ndarray | None:
        env = np.fromiter(self._env, dtype=np.float64, count=len(self._env))
        env = env - env.mean()
        if not np.any(env):
            return None
        n = 1 << int(np.ceil(np.log2(len(env) * 2)))
        spec = np.fft.rfft(env, n)
        acf = np.fft.irfft(spec * np.conj(spec), n)[: len(env)]
        if acf[0] <= 0:
            return None
        return acf / acf[0]

    def _estimate_period(self) -> None:
        acf = self._acf()
        if acf is None:
            self.confidence = 0.0
            return
        hi = min(self._max_lag, (len(acf) - 1) // 4)
        if hi <= self._min_lag:
            self.confidence = 0.0
            return

        # Score each period by the ACF at 1, 2 and 4 beats, at sub-sample
        # resolution. A real beat repeats every bar as well as every beat; a
        # triplet or 3:4 pattern that happens to peak at one lag does not line
        # up at its multiples. Single-lag scoring is what let this flip between
        # 106, 114 and 160 BPM on a 160 BPM track.
        lags = np.arange(self._min_lag, hi + 0.001, 0.25)
        idx = np.arange(len(acf))
        scores = sum(w * np.interp(k * lags, idx, acf) for k, w in COMB)
        scores = np.maximum(scores, 0.0)

        # Prior over tempo, for the remaining octave ambiguity.
        cand_bpm = 60.0 * self.rate_hz / lags
        prior = np.exp(-0.5 * (np.log2(cand_bpm / self.prior_bpm) / self.prior_width) ** 2)
        weighted = scores * prior

        best = int(np.argmax(weighted))
        if weighted[best] <= 0:
            self.confidence = 0.0
            return
        bpm = float(cand_bpm[best])
        # Confidence: comb strength at the winner, 0..1 (a perfect pulse train
        # scores sum(weights)). Unlike peak-versus-mean, this is low when the
        # music is not periodic, so `locked` can actually mean something.
        strength = float(np.clip(scores[best] / sum(w for _, w in COMB), 0.0, 1.0))

        if self.bpm <= 0:
            self.bpm = bpm
            self._challenger, self._challenger_n = 0.0, 0
            self._agree = 0
            self.confidence = strength
            log.info("Tempo locked: %.1f BPM (strength %.2f)", bpm, strength)
            return

        if abs(bpm - self.bpm) / self.bpm <= SAME_TEMPO:
            # Same tempo: refine gently.
            self.bpm += 0.3 * (bpm - self.bpm)
            self._agree += 1
            self._challenger, self._challenger_n = 0.0, 0
            self.confidence += 0.3 * (strength - self.confidence)
            return

        # A different tempo has to win several estimates in a row before we
        # follow it. One ambiguous window must not move the whole rig; a real
        # track change still gets through in about two seconds.
        if self._challenger and abs(bpm - self._challenger) / self._challenger <= SAME_TEMPO:
            self._challenger_n += 1
            self._challenger += 0.5 * (bpm - self._challenger)
        else:
            self._challenger, self._challenger_n = bpm, 1
        self.confidence *= 0.85
        self._agree = 0
        if self._challenger_n >= SWITCH_AFTER:
            log.info("Tempo changed: %.1f -> %.1f BPM", self.bpm, self._challenger)
            self.bpm = self._challenger
            self.confidence = strength
            self._challenger, self._challenger_n = 0.0, 0

    def _correct_phase(self) -> None:
        """Pull the clock toward where the recent beats actually were.

        Folds the salience history at the current period and finds the offset
        with the most energy: every hit in the window votes, so a bass note
        between beats is outvoted by the beats themselves.
        """
        period = self.beat_period
        if period <= 0:
            return
        p = period * self.rate_hz  # frames per beat
        env = np.fromiter(self._env, dtype=np.float64, count=len(self._env))
        n = len(env)
        k = np.arange(int((n - 2) // p))
        if len(k) < 4:
            return
        # Newer beats count more, so a drifting tempo is tracked, not averaged.
        weight = 0.85 ** k
        offsets = np.arange(0.0, p, 0.25)
        idx = n - 1 - offsets[:, None] - k[None, :] * p
        votes = (np.interp(idx, np.arange(n), env) * weight).sum(axis=1)
        since = offsets[int(np.argmax(votes))] + DETECT_LAG_FRAMES  # frames since the last beat
        target = (since / p) % 1.0
        err = target - self.phase
        err -= round(err)  # shortest way round
        self.phase = (self.phase + PHASE_PULL * err) % 1.0

    def reset(self, *, keep_tempo: bool = False) -> None:
        """Clear state. Called on a track change, where the tempo is new."""
        self._env.clear()
        self.phase = 0.0
        self.beat_index = 0
        self._beat_fired = False
        self._last_estimate_t = -1e9
        self._challenger, self._challenger_n = 0.0, 0
        self._agree = 0
        if not keep_tempo:
            self.bpm = 0.0
            self.confidence = 0.0

    def stats(self) -> dict:
        return {
            "bpm": round(self.bpm, 1),
            "confidence": round(self.confidence, 2),
            "phase": round(self.phase, 3),
            "locked": self.locked,
            "beat": self.beat_index,
        }
