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
        return self.bpm > 0 and self.confidence >= 0.25

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

        `onset_strength` should be a continuous beat-salience signal (kick flux
        works well); `onset` marks a discrete detected hit, which is what the
        PLL corrects against.
        """
        self._env.append(float(onset_strength))

        dt = 0.0 if self._last_t is None else max(0.0, t - self._last_t)
        self._last_t = t

        if t - self._last_estimate_t >= self.estimate_every_s and len(self._env) >= self._max_lag * 2:
            self._estimate_period()
            self._last_estimate_t = t

        self._beat_fired = False
        period = self.beat_period
        if period <= 0:
            return

        # Free-run the clock.
        self.phase += dt / period
        while self.phase >= 1.0:
            self.phase -= 1.0
            self.beat_index += 1
            self._beat_fired = True

        # Correct against an observed hit.
        if onset and self.confidence > 0.15:
            # Signed distance from this onset to the nearest predicted beat, in
            # beats. Positive means the onset landed after our predicted beat,
            # i.e. our clock is running ahead.
            err = self.phase if self.phase < 0.5 else self.phase - 1.0
            self.phase -= self.phase_gain * err
            if self.phase < 0.0:
                self.phase += 1.0
            # Persistent error means the period itself is off, not just phase.
            if self.bpm > 0:
                self.bpm /= (1.0 + self.period_gain * err)
                self.bpm = float(np.clip(self.bpm, self.min_bpm, self.max_bpm))

    # -- period estimation ------------------------------------------------

    def _estimate_period(self) -> None:
        env = np.fromiter(self._env, dtype=np.float64, count=len(self._env))
        env = env - env.mean()
        if not np.any(env):
            self.confidence = 0.0
            return

        # Autocorrelation via FFT: cheap enough to run twice a second.
        n = 1 << int(np.ceil(np.log2(len(env) * 2)))
        spec = np.fft.rfft(env, n)
        acf = np.fft.irfft(spec * np.conj(spec), n)[: len(env)]
        if acf[0] <= 0:
            self.confidence = 0.0
            return
        acf /= acf[0]

        hi = min(self._max_lag, len(acf) - 1)
        if hi <= self._min_lag:
            self.confidence = 0.0
            return

        lags = np.arange(self._min_lag, hi + 1)
        scores = acf[self._min_lag : hi + 1].copy()

        # Prior over tempo. Without this, autocorrelation happily locks onto
        # half or double the real tempo -- it is genuinely periodic there too.
        cand_bpm = 60.0 * self.rate_hz / lags
        log_ratio = np.log2(cand_bpm / self.prior_bpm)
        scores *= np.exp(-0.5 * (log_ratio / self.prior_width) ** 2)

        best = int(np.argmax(scores))
        peak = float(scores[best])
        if peak <= 0:
            self.confidence = 0.0
            return

        bpm = float(cand_bpm[best])
        # Confidence: how much the winning peak stands out from the field.
        mean_score = float(np.mean(scores))
        self.confidence = float(np.clip((peak - mean_score) / (peak + 1e-9), 0.0, 1.0))

        if self.bpm <= 0:
            self.bpm = bpm
            log.info("Tempo locked: %.1f BPM (confidence %.2f)", bpm, self.confidence)
        else:
            # Jump to a clearly different tempo (new track); otherwise glide, so
            # a momentarily ambiguous estimate does not jerk the whole rig.
            if abs(bpm - self.bpm) / self.bpm > 0.12:
                log.info("Tempo changed: %.1f -> %.1f BPM (confidence %.2f)",
                         self.bpm, bpm, self.confidence)
                self.bpm = bpm
                self.phase = 0.0
            else:
                self.bpm += 0.25 * (bpm - self.bpm)

    def reset(self, *, keep_tempo: bool = False) -> None:
        """Clear state. Called on a track change, where the tempo is new."""
        self._env.clear()
        self.phase = 0.0
        self.beat_index = 0
        self._beat_fired = False
        self._last_estimate_t = -1e9
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
