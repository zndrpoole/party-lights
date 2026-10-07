"""Whole-song recording from the loopback, with a clock you can trust.

The live capture keeps twelve seconds in a ring; the pass needs a whole song,
sample-exact, because every time in a map is counted in samples from the
start of the take. So this records into one preallocated buffer, and keeps
two pieces of evidence that no samples went missing:

* overflows reported by the audio driver, and
* a log of (monotonic clock, samples so far) at every callback, so the sample
  count can be checked against the wall clock -- and so a Spotify position,
  stamped on the wall clock, can be placed on the recording to the sample.

A take with an overflow, or whose sample count drifts from the clock, is
thrown away and played again: one lost block shifts everything after it.
"""

from __future__ import annotations

import threading
import time

import numpy as np

from ..audio.capture import find_device

SR = 48000
BLOCK = 512


class Recorder:
    def __init__(self, device: str = "BlackHole", *, sample_rate: int = SR, channels: int = 2):
        self.device_spec = device
        self.sample_rate = sample_rate
        self.channels = channels
        self._stream = None
        self._lock = threading.Lock()
        self._buf: np.ndarray | None = None
        self._n = 0
        self._armed = False
        self._clock: list[tuple[float, int]] = []
        self.overflows = 0
        self._peak = 0.0
        self.device_name = ""

    def start(self) -> None:
        import sounddevice as sd
        idx = find_device(self.device_spec) if isinstance(self.device_spec, str) else self.device_spec
        info = sd.query_devices(idx)
        self.device_name = info["name"]
        self.channels = min(self.channels, info["max_input_channels"])
        self._weights = np.full((self.channels, 1), 1.0 / self.channels, dtype=np.float32)
        self._stream = sd.InputStream(device=idx, samplerate=self.sample_rate, blocksize=BLOCK,
                                      channels=self.channels, dtype="float32",
                                      callback=self._callback, latency="low")
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None

    def _callback(self, indata, frames, time_info, status):
        now = time.monotonic()
        mono = (indata @ self._weights).reshape(-1)
        peak = float(np.abs(mono).max()) if mono.size else 0.0
        with self._lock:
            self._peak = max(self._peak * 0.9, peak)
            if not self._armed or self._buf is None:
                return
            if status:
                self.overflows += 1
            end = self._n + len(mono)
            if end > len(self._buf):
                self._armed = False
                return
            self._buf[self._n:end] = mono
            self._n = end
            # The clock stamp is for the *end* of this block.
            self._clock.append((now, end))

    def peak(self) -> float:
        with self._lock:
            return self._peak

    def arm(self, max_seconds: float) -> None:
        """Start a take, holding up to `max_seconds`."""
        buf = np.zeros(int(max_seconds * self.sample_rate), dtype=np.float32)
        with self._lock:
            self._buf = buf
            self._n = 0
            self._clock = []
            self.overflows = 0
            self._armed = True

    def disarm(self) -> "Take":
        with self._lock:
            self._armed = False
            take = Take(self._buf[:self._n].copy() if self._buf is not None else np.zeros(0),
                        np.array(self._clock, dtype=np.float64).reshape(-1, 2),
                        self.overflows, self.sample_rate)
            self._buf = None
            return take

    @property
    def seconds(self) -> float:
        with self._lock:
            return self._n / self.sample_rate


class Take:
    """One recorded song, plus the evidence its timeline is intact."""

    def __init__(self, samples: np.ndarray, clock: np.ndarray, overflows: int, sample_rate: int):
        self.samples = samples
        self.clock = clock              # (k, 2): monotonic time, samples so far
        self.overflows = overflows
        self.sample_rate = sample_rate
        self._fit = None

    def _clock_fit(self) -> tuple[float, float, float]:
        """Least-squares line through the callback stamps: (samples per
        second, samples at t_ref, t_ref). Callbacks wake with scheduler
        jitter; the line through thousands of them is accurate to well under
        a block."""
        if self._fit is None:
            if len(self.clock) < 10:
                t_ref = float(self.clock[0, 0]) if len(self.clock) else 0.0
                n_ref = float(self.clock[0, 1]) if len(self.clock) else 0.0
                self._fit = (float(self.sample_rate), n_ref, t_ref)
            else:
                t, n = self.clock[:, 0], self.clock[:, 1]
                slope, icpt = np.polyfit(t - t[0], n, 1)
                self._fit = (float(slope), float(icpt), float(t[0]))
        return self._fit

    def sample_at(self, mono: float) -> float:
        """Recording sample index at a moment on the monotonic clock."""
        slope, n_ref, t_ref = self._clock_fit()
        return slope * (mono - t_ref) + n_ref

    def rate_error(self) -> float:
        """How far the measured sample rate is from nominal, as a fraction.
        A dropped block shows up here even when the driver did not report it."""
        if len(self.clock) < 10:
            return 0.0
        slope, _, _ = self._clock_fit()
        return slope / self.sample_rate - 1.0

    def max_clock_residual_s(self) -> float:
        """Largest gap between a callback's stamp and the fitted line, s. A
        lost block is a step of 512 samples (10.7 ms) in the residuals."""
        if len(self.clock) < 10:
            return 0.0
        t, n = self.clock[:, 0], self.clock[:, 1]
        pred = np.array([self.sample_at(x) for x in t])
        return float(np.abs(n - pred).max() / self.sample_rate)
