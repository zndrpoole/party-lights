"""Frame features: the one spectral front end every song map is built from.

A song map is only as accurate as the features underneath it, and the live
rig has to compute the *same* features to find its place in a map. So this is
a streaming extractor -- it is fed blocks of samples and emits frames -- and
the offline pass feeds it a whole song through the same `feed()` the live
aligner will use. Same code, same numbers, which is what makes a map's
timeline trustworthy at the party.

100 frames a second (hop 480 at 48 kHz), each looked at through two windows
centred on the same instant:

* 4096 samples (85 ms) for levels and pitch: it resolves the low end into
  ~12 Hz bins, which the bass bands and the chroma need.
* 1024 samples (21 ms) for onsets. In the long window a hit shows up as soon
  as it touches the window's edge, which on a log scale is up to 45 ms early
  -- and by an amount that depends on how loud the section is, so no constant
  can correct it. Measured on test songs: beats landed 15 ms early in a quiet
  intro and 50 ms early in a chorus. The short window bounds that to ~10 ms.

Per frame:

    mel     64 log-mel bands, 30 Hz .. 16 kHz, dB
    bands   the engine's seven named bands (sub .. air), dB
    chroma  12 pitch classes from 55 Hz to 2 kHz, sum-normalised
    onset   spectral flux on the mel bands, overall and per region
    rms_db  frame level, dBFS
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..audio.features import DEFAULT_BANDS

SR = 48000
N_FFT = 4096
HOP = 480
FPS = SR / HOP              # 100.0
N_SHORT = 1024
#: Frames between the two spectra compared for an onset. 2 (20 ms) lets a hit
#: build over two frames instead of being split across them.
FLUX_LAG = 2
N_MELS = 64
MEL_LO, MEL_HI = 30.0, 16000.0
CHROMA_LO, CHROMA_HI = 55.0, 2000.0
#: dB floor for log features, relative to full scale. Quieter than this is
#: silence for every purpose here.
FLOOR_DB = -100.0

#: Onset regions on the mel bands: (name, lo Hz, hi Hz). Low catches the kick
#: and bass, mid the snare and vocals, high the hats and cymbals.
ONSET_REGIONS = (("low", 30.0, 150.0), ("mid", 150.0, 3000.0), ("high", 6000.0, 16000.0))
BAND_NAMES = tuple(name for name, _, _ in DEFAULT_BANDS)


def _hz_to_mel(f):
    return 2595.0 * np.log10(1.0 + np.asarray(f) / 700.0)


def _mel_to_hz(m):
    return 700.0 * (10.0 ** (np.asarray(m) / 2595.0) - 1.0)


def _mel_filterbank(n_fft: int = N_FFT) -> np.ndarray:
    """Triangular mel filters, (N_MELS, bins), each normalised to unit area so
    a band's level does not depend on how many FFT bins it spans. A filter
    narrower than one bin (the bottom bands of the short window) takes the
    nearest bin, so no band is ever empty."""
    freqs = np.fft.rfftfreq(n_fft, 1.0 / SR)
    edges = _mel_to_hz(np.linspace(_hz_to_mel(MEL_LO), _hz_to_mel(MEL_HI), N_MELS + 2))
    fb = np.zeros((N_MELS, len(freqs)), dtype=np.float32)
    for i in range(N_MELS):
        lo, mid, hi = edges[i], edges[i + 1], edges[i + 2]
        up = (freqs - lo) / max(mid - lo, 1e-9)
        down = (hi - freqs) / max(hi - mid, 1e-9)
        fb[i] = np.clip(np.minimum(up, down), 0.0, None)
        if fb[i].sum() == 0:
            fb[i, int(np.argmin(np.abs(freqs - mid)))] = 1.0
        area = fb[i].sum()
        if area > 0:
            fb[i] /= area
    return fb


def _chroma_map() -> np.ndarray:
    """(12, bins) 0/1 map from FFT bin to pitch class, C = 0."""
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SR)
    out = np.zeros((12, len(freqs)), dtype=np.float32)
    ok = (freqs >= CHROMA_LO) & (freqs <= CHROMA_HI)
    midi = 69.0 + 12.0 * np.log2(np.where(ok, freqs, 1.0) / 440.0)
    pc = np.round(midi).astype(int) % 12
    out[pc[ok], np.nonzero(ok)[0]] = 1.0
    return out


def _band_masks() -> np.ndarray:
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SR)
    return np.array([(freqs >= lo) & (freqs < hi) for _, lo, hi in DEFAULT_BANDS],
                    dtype=np.float32)


MEL_FB = _mel_filterbank()
MEL_FB_SHORT = _mel_filterbank(N_SHORT)
MEL_CENTRES = _mel_to_hz(np.linspace(_hz_to_mel(MEL_LO), _hz_to_mel(MEL_HI), N_MELS + 2))[1:-1]
CHROMA = _chroma_map()
BANDS = _band_masks()
WINDOW = np.hanning(N_FFT).astype(np.float32)
WINDOW_SHORT = np.hanning(N_SHORT).astype(np.float32)
SHORT_OFFSET = (N_FFT - N_SHORT) // 2
REGION_MASKS = {name: (MEL_CENTRES >= lo) & (MEL_CENTRES < hi)
                for name, lo, hi in ONSET_REGIONS}


@dataclass
class Frames:
    """Features for a run of frames. Frame i is centred at t0 + i / FPS."""

    t0: float
    mel: np.ndarray          # (n, 64) dB
    bands: np.ndarray        # (n, 7) dB
    chroma: np.ndarray       # (n, 12)
    onset: np.ndarray        # (n,)
    region_onset: np.ndarray  # (n, 3): low, mid, high
    rms_db: np.ndarray       # (n,)

    def __len__(self) -> int:
        return len(self.onset)

    @property
    def times(self) -> np.ndarray:
        return self.t0 + np.arange(len(self)) / FPS

    def frame_at(self, t: float) -> int:
        return int(round((t - self.t0) * FPS))

    @staticmethod
    def concat(parts: list["Frames"]) -> "Frames":
        if not parts:
            return Frames.empty()
        return Frames(
            t0=parts[0].t0,
            mel=np.concatenate([p.mel for p in parts]),
            bands=np.concatenate([p.bands for p in parts]),
            chroma=np.concatenate([p.chroma for p in parts]),
            onset=np.concatenate([p.onset for p in parts]),
            region_onset=np.concatenate([p.region_onset for p in parts]),
            rms_db=np.concatenate([p.rms_db for p in parts]),
        )

    @staticmethod
    def empty(t0: float = 0.0) -> "Frames":
        return Frames(t0, np.zeros((0, N_MELS), np.float32), np.zeros((0, 7), np.float32),
                      np.zeros((0, 12), np.float32), np.zeros(0, np.float32),
                      np.zeros((0, 3), np.float32), np.zeros(0, np.float32))


def _db(power: np.ndarray) -> np.ndarray:
    return np.maximum(10.0 * np.log10(np.maximum(power, 1e-20)), FLOOR_DB).astype(np.float32)


class FrameExtractor:
    """Streaming: feed() any number of samples, get back the frames completed.

    `t0` is the time of the first sample fed, in whatever clock the caller
    uses -- song time for the pass, capture time live.
    """

    def __init__(self, t0: float = 0.0):
        self._pending = np.zeros(0, dtype=np.float32)
        self._consumed = 0          # samples dropped off the front so far
        self._t0 = t0
        self._prev_mel: np.ndarray | None = None

    def feed(self, samples: np.ndarray, batch: int = 2048) -> Frames:
        buf = np.concatenate((self._pending, np.asarray(samples, dtype=np.float32)))
        n = 0 if len(buf) < N_FFT else 1 + (len(buf) - N_FFT) // HOP
        if n == 0:
            self._pending = buf
            return Frames.empty(self._frame_time(0))
        t_first = self._frame_time(0)
        parts = []
        for start in range(0, n, batch):
            count = min(batch, n - start)
            idx = (start + np.arange(count))[:, None] * HOP + np.arange(N_FFT)[None, :]
            block = buf[idx]
            spec = np.fft.rfft(block * WINDOW, axis=1)
            power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
            short = np.fft.rfft(block[:, SHORT_OFFSET:SHORT_OFFSET + N_SHORT] * WINDOW_SHORT,
                                axis=1)
            short_power = (short.real ** 2 + short.imag ** 2).astype(np.float32)
            parts.append(self._features(power, np.sqrt(power), short_power))
        frames = Frames.concat(parts)
        frames.t0 = t_first
        drop = n * HOP
        self._pending = buf[drop:]
        self._consumed += drop
        return frames

    def _frame_time(self, k: int) -> float:
        """Centre of the k-th frame from the current read position."""
        return self._t0 + (self._consumed + k * HOP + N_FFT / 2) / SR

    def _features(self, power: np.ndarray, mag: np.ndarray,
                  short_power: np.ndarray) -> Frames:
        # Window power gain, so a full-scale sine reads near 0 dBFS.
        norm = (WINDOW.sum() ** 2) / 2.0
        mel = _db(power @ MEL_FB.T * (N_FFT / 2) / norm)
        bands = _db(power @ BANDS.T / norm)
        rms_db = _db(power.sum(axis=1) / norm)

        chroma = mag @ CHROMA.T
        chroma /= np.maximum(chroma.sum(axis=1, keepdims=True), 1e-9)

        # Onsets: SuperFlux (Boeck & Widmer, 2013) on the short window's
        # log-mel. Each band is compared with the frame FLUX_LAG back, maximum-
        # filtered across neighbouring bands, and only rises count. Plain flux
        # fires on every wobble of a held bass note -- on a real capture the
        # kick region spiked several times a beat -- while a hit rises across
        # neighbouring bands at once and survives the filter. Bands below
        # -80 dB are held at the floor so noise in near-silence is not hits.
        short_norm = (WINDOW_SHORT.sum() ** 2) / 2.0
        short_mel = _db(short_power @ MEL_FB_SHORT.T * (N_SHORT / 2) / short_norm)
        floored = np.maximum(short_mel, -80.0)
        spread = floored.copy()
        spread[:, 1:] = np.maximum(spread[:, 1:], floored[:, :-1])
        spread[:, :-1] = np.maximum(spread[:, :-1], floored[:, 1:])
        prev = (self._prev_mel if self._prev_mel is not None
                else np.repeat(spread[:1], FLUX_LAG, axis=0))
        history = np.vstack((prev, spread))
        rise = np.maximum(floored - history[:len(floored)], 0.0)
        self._prev_mel = history[-FLUX_LAG:]
        onset = rise.mean(axis=1)
        region = np.stack([rise[:, m].mean(axis=1) for m in REGION_MASKS.values()], axis=1)
        return Frames(0.0, mel, bands, chroma.astype(np.float32), onset.astype(np.float32),
                      region.astype(np.float32), rms_db)


def extract(samples: np.ndarray, t0: float = 0.0) -> Frames:
    """Frames for a whole signal, through the same path as streaming."""
    return FrameExtractor(t0).feed(samples)
