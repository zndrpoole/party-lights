"""Spectral feature extraction: bands, per-region onsets, and level tracking.

The design constraint that shapes this module: it is fed *hops of samples* and
knows nothing about where they came from. Live capture pushes blocks in; the
offline analyser pushes a decoded file in. Same code, same state machine, same
results — so tuning against a file genuinely tunes the live rig, which is the
only practical way to work on this without a party in progress.

Two choices worth explaining:

**Onsets are detected per frequency region, not globally.** A single broadband
"beat" signal cannot tell a kick from a hi-hat, so every look driven by it ends
up reacting to the busiest thing in the mix rather than to the groove. Splitting
flux into low/mid/high regions lets a look pulse on kicks while a different one
sparkles on hats.

**Everything level-sensitive is normalised by a slow peak follower.** A party
playlist jumps between a 2009 loudness-war master and something quiet off
Bandcamp. Without normalisation the lights would barely move for half the night.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

#: Bands for colour and intensity mapping. Chosen for what is musically useful
#: to light rather than for even spectral coverage: the bottom two octaves get
#: three bands because that is where the groove lives.
DEFAULT_BANDS: tuple[tuple[str, float, float], ...] = (
    ("sub", 20.0, 60.0),
    ("bass", 60.0, 150.0),
    ("lowmid", 150.0, 400.0),
    ("mid", 400.0, 1200.0),
    ("highmid", 1200.0, 3500.0),
    ("high", 3500.0, 9000.0),
    ("air", 9000.0, 16000.0),
)

#: Regions for onset detection, named for what they usually catch.
#:
#: The hat region starts high — 8 kHz, not the 5 kHz you might expect from where
#: hi-hats begin. Real snares are bright enough to put significant energy up to
#: 7 kHz, so a lower edge makes every snare fire the hat detector too. Measured
#: against a track with 32 snares and 64 hats, a 5 kHz edge reported 95 hats and
#: an 8 kHz edge reported exactly 64. The trade is that a dark, closed hi-hat in
#: a muddy mix may be missed; that is the better failure, since a missed hat is
#: invisible whereas a hat detector that fires on every snare makes two looks
#: move identically.
DEFAULT_REGIONS: tuple[tuple[str, float, float], ...] = (
    ("kick", 30.0, 110.0),
    ("snare", 180.0, 2500.0),
    ("hat", 8000.0, 16000.0),
)

#: Minimum gap between onsets in a region. A kick cannot physically repeat every
#: 50 ms in dance music, so anything faster is the same hit detected twice.
DEFAULT_DEBOUNCE_S = {"kick": 0.110, "snare": 0.080, "hat": 0.050}


#: Absolute floor for the band normaliser's denominator, as an RMS level.
#: Roughly -50 dBFS: below this a band is carrying nothing we should react to.
#:
#: This value is load-bearing and was originally far too low (1e-4, -80 dBFS).
#: The failure is subtle and worth remembering: when a band goes genuinely empty
#: -- no hi-hats during an intro, say -- its peak reference decays toward the
#: floor, and once the floor is below the noise level, dividing by it amplifies
#: numerical noise up to mid-scale. The band then *reports activity that is not
#: there*. Observed symptom: an intro containing only kicks showed 0.5 in the
#: air band, and the resulting loss of spectral contrast stopped section
#: detection from firing at all on an obvious drop.
BAND_FLOOR_RMS = 3e-3

#: Same reasoning for the overall level normaliser, which sees full-mix RMS.
ENERGY_FLOOR_RMS = 2e-3


class PeakFollower:
    """Slow-decaying peak tracker, used to normalise anything level-dependent.

    Rises instantly to a new peak and decays with a configurable half-life, so a
    loud track sets the reference and a quiet one gradually earns a lower bar.

    `floor` is the minimum denominator, and it must be set to a real signal
    level rather than an epsilon -- see BAND_FLOOR_RMS for what goes wrong
    otherwise.
    """

    def __init__(self, half_life_s: float = 8.0, rate_hz: float = 90.0, floor: float = 1e-3):
        self.floor = floor
        self._value = floor
        # Per-hop multiplier that halves the value over half_life_s.
        self._decay = 0.5 ** (1.0 / max(1.0, half_life_s * rate_hz))

    def update(self, x: float) -> float:
        self._value = max(x, self._value * self._decay, self.floor)
        return self._value

    def normalise(self, x: float) -> float:
        """Update with x, return x scaled into roughly 0..1."""
        peak = self.update(x)
        return min(1.0, x / peak) if peak > 0 else 0.0

    @property
    def value(self) -> float:
        return self._value


class Envelope:
    """Asymmetric attack/release follower.

    Lighting wants a fast attack so a hit registers, and a slow release so the
    fixture glows down rather than snapping off. Symmetric smoothing gives you
    one or the other and looks wrong either way.
    """

    def __init__(self, attack_s: float = 0.005, release_s: float = 0.120, rate_hz: float = 90.0):
        self._value = 0.0
        self._a_up = 1.0 - np.exp(-1.0 / max(1e-6, attack_s * rate_hz))
        self._a_dn = 1.0 - np.exp(-1.0 / max(1e-6, release_s * rate_hz))

    def update(self, x: float) -> float:
        coeff = self._a_up if x > self._value else self._a_dn
        self._value += coeff * (x - self._value)
        return self._value

    @property
    def value(self) -> float:
        return self._value


class OnsetDetector:
    """Adaptive-threshold onset detection on one region's flux signal.

    The threshold is median plus a multiple of the median absolute deviation
    over a recent window. Robust statistics matter here: a mean-and-standard-
    deviation threshold is dragged upward by the very transients we are trying
    to detect, so it goes deaf exactly during the busiest part of a track.

    `min_flux` is not optional padding — it is load-bearing. On sparse material
    most frames carry almost no flux, so the median and the MAD both collapse
    toward zero and the adaptive threshold follows them down until any nonzero
    flux fires. The symptom is extra onsets trailing each real hit as its decay
    tail wobbles above a threshold of effectively zero. Because flux is already
    peak-normalised to roughly 0..1, a fixed floor is meaningful here.
    """

    def __init__(
        self,
        *,
        sensitivity: float = 2.2,
        history_s: float = 1.5,
        debounce_s: float = 0.08,
        rate_hz: float = 90.0,
        min_flux: float = 0.16,
        eps: float = 1e-5,
    ):
        self.sensitivity = sensitivity
        self.debounce_s = debounce_s
        self.min_flux = min_flux
        self.eps = eps
        self._hist: deque[float] = deque(maxlen=max(8, int(history_s * rate_hz)))
        self._last_onset_t = -1e9
        self.threshold = 0.0
        self.strength = 0.0

    def update(self, flux: float, t: float) -> bool:
        hist = self._hist
        fired = False
        if len(hist) >= 8:
            arr = np.fromiter(hist, dtype=np.float64, count=len(hist))
            med = float(np.median(arr))
            mad = float(np.median(np.abs(arr - med)))
            # 1.4826 * MAD estimates the standard deviation of a normal sample.
            adaptive = med + self.sensitivity * (mad * 1.4826 + self.eps)
            self.threshold = max(adaptive, self.min_flux)
            if flux > self.threshold and t - self._last_onset_t >= self.debounce_s:
                self._last_onset_t = t
                fired = True
            # How far above threshold, roughly 0..1. Looks use this for hit size.
            self.strength = min(1.0, max(0.0, (flux - self.threshold) / (self.threshold + self.eps)))
        hist.append(flux)
        return fired

    def since_onset(self, t: float) -> float:
        return t - self._last_onset_t


@dataclass
class FeatureFrame:
    """Everything the engine knows about the music at one instant."""

    #: Seconds since analysis started.
    t: float
    #: Raw RMS of the window, 0..1.
    rms: float
    #: RMS in dBFS; -inf-ish when silent. Useful for "is anything playing".
    loudness_db: float
    #: Overall level normalised against this track's recent peak, 0..1. This is
    #: the one a look should use for "how hard is the music going right now".
    energy: float
    #: Per-band energy, normalised per band. Keys are DEFAULT_BANDS names.
    #: This is what looks should use: "how loud is the bass *for this track*".
    bands: dict[str, float] = field(default_factory=dict)
    #: Per-band energy, absolute RMS, no normalisation.
    #:
    #: Needed because per-band normalisation destroys spectral *shape*: a band
    #: carrying nothing has a low reference, so its own leakage normalises up,
    #: while a loud band reads low between hits. The result can invert which
    #: band looks dominant. Anything comparing one moment's spectrum against
    #: another's -- structure detection, principally -- must use these.
    bands_raw: dict[str, float] = field(default_factory=dict)
    #: Per-band envelope-followed values — smoother, for washes and fades.
    bands_smooth: dict[str, float] = field(default_factory=dict)
    #: Per-region spectral flux, normalised.
    flux: dict[str, float] = field(default_factory=dict)
    #: Regions that fired an onset on this frame.
    onsets: dict[str, bool] = field(default_factory=dict)
    #: How far above threshold each region's onset was, 0..1.
    onset_strength: dict[str, float] = field(default_factory=dict)
    #: Spectral centroid in Hz — a decent proxy for "brightness" of the sound,
    #: which maps naturally onto colour temperature.
    centroid: float = 0.0
    #: True when the signal is essentially silent, so looks can idle gracefully
    #: instead of chasing noise.
    silent: bool = True

    def onset(self, region: str) -> bool:
        return self.onsets.get(region, False)

    def band(self, name: str, default: float = 0.0) -> float:
        return self.bands.get(name, default)

    def smooth(self, name: str, default: float = 0.0) -> float:
        return self.bands_smooth.get(name, default)


class FeatureExtractor:
    """Turns a stream of samples into a stream of FeatureFrames.

    Push samples with `feed()`; get back zero or more frames, one per hop. State
    persists across calls, so block boundaries do not matter — which is what
    lets the live and offline paths be literally the same object.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 48000,
        window: int = 2048,
        hop: int = 512,
        bands=DEFAULT_BANDS,
        regions=DEFAULT_REGIONS,
        onset_sensitivity: float = 2.2,
        silence_db: float = -55.0,
    ):
        self.sample_rate = sample_rate
        self.window = window
        self.hop = hop
        self.bands = tuple(bands)
        self.regions = tuple(regions)
        self.silence_db = silence_db
        self.rate_hz = sample_rate / hop

        self._win_buf = np.zeros(window, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._hann = np.hanning(window).astype(np.float32)
        # Power-preserving scale for the Hann window, so RMS stays meaningful.
        self._win_gain = 1.0 / (self._hann.sum() / window)

        freqs = np.fft.rfftfreq(window, 1.0 / sample_rate)
        self._freqs = freqs
        self._band_bins = [self._bins_for(freqs, lo, hi) for _, lo, hi in self.bands]
        self._region_bins = [self._bins_for(freqs, lo, hi) for _, lo, hi in self.regions]

        self._prev_log_mag = np.zeros(len(freqs), dtype=np.float32)

        self._frames = 0
        self._t = 0.0

        # Normalisers and smoothers, one per band / region.
        self._band_peak = {
            name: PeakFollower(10.0, self.rate_hz, floor=BAND_FLOOR_RMS)
            for name, _, _ in self.bands
        }
        self._band_env = {name: Envelope(0.008, 0.150, self.rate_hz) for name, _, _ in self.bands}
        self._flux_peak = {name: PeakFollower(6.0, self.rate_hz) for name, _, _ in self.regions}
        self._energy_peak = PeakFollower(10.0, self.rate_hz, floor=ENERGY_FLOOR_RMS)
        self._detectors = {
            name: OnsetDetector(
                sensitivity=onset_sensitivity,
                debounce_s=DEFAULT_DEBOUNCE_S.get(name, 0.08),
                rate_hz=self.rate_hz,
            )
            for name, _, _ in self.regions
        }

    @staticmethod
    def _bins_for(freqs: np.ndarray, lo: float, hi: float) -> np.ndarray:
        idx = np.where((freqs >= lo) & (freqs < hi))[0]
        if idx.size == 0:
            # Degenerate band (window too short for this low a frequency):
            # fall back to the nearest single bin so it reads zero, not crashes.
            idx = np.array([int(np.argmin(np.abs(freqs - lo)))])
        return idx

    @property
    def frames_emitted(self) -> int:
        return self._frames

    def feed(self, samples: np.ndarray) -> list[FeatureFrame]:
        """Push samples; return one FeatureFrame per completed hop."""
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)
        buf = np.concatenate((self._pending, samples)) if self._pending.size else samples

        out: list[FeatureFrame] = []
        pos = 0
        hop, window = self.hop, self.window
        while pos + hop <= len(buf):
            chunk = buf[pos : pos + hop]
            # Slide the analysis window: drop the oldest hop, append the newest.
            self._win_buf[:-hop] = self._win_buf[hop:]
            self._win_buf[-hop:] = chunk
            out.append(self._analyse())
            pos += hop

        self._pending = buf[pos:].copy()
        return out

    def _analyse(self) -> FeatureFrame:
        self._frames += 1
        self._t = self._frames * self.hop / self.sample_rate

        frame = self._win_buf
        rms = float(np.sqrt(np.mean(frame * frame)))
        loudness_db = 20.0 * np.log10(rms) if rms > 1e-9 else -120.0
        silent = loudness_db < self.silence_db

        spectrum = np.abs(np.fft.rfft(frame * self._hann)) * self._win_gain
        # Log magnitude for flux: makes transient detection depend on relative
        # change rather than absolute level, so a quiet passage still has onsets.
        log_mag = np.log1p(spectrum).astype(np.float32)

        bands: dict[str, float] = {}
        bands_raw: dict[str, float] = {}
        bands_smooth: dict[str, float] = {}
        for (name, _, _), bins in zip(self.bands, self._band_bins):
            raw = float(np.sqrt(np.mean(spectrum[bins] ** 2)))
            norm = self._band_peak[name].normalise(raw)
            bands_raw[name] = raw
            bands[name] = norm
            bands_smooth[name] = self._band_env[name].update(norm)

        flux: dict[str, float] = {}
        onsets: dict[str, bool] = {}
        strengths: dict[str, float] = {}
        delta = log_mag - self._prev_log_mag
        np.maximum(delta, 0.0, out=delta)  # half-wave rectify: only rises count
        for (name, _, _), bins in zip(self.regions, self._region_bins):
            raw = float(np.mean(delta[bins]))
            norm = self._flux_peak[name].normalise(raw)
            flux[name] = norm
            det = self._detectors[name]
            fired = det.update(norm, self._t) if not silent else False
            onsets[name] = fired
            strengths[name] = det.strength
        self._prev_log_mag = log_mag

        total = float(spectrum.sum())
        centroid = float((self._freqs * spectrum).sum() / total) if total > 1e-9 else 0.0

        energy = self._energy_peak.normalise(rms)

        return FeatureFrame(
            t=self._t,
            rms=rms,
            loudness_db=loudness_db,
            energy=0.0 if silent else energy,
            bands=bands,
            bands_raw=bands_raw,
            bands_smooth=bands_smooth,
            flux=flux,
            onsets=onsets,
            onset_strength=strengths,
            centroid=centroid,
            silent=silent,
        )

    @property
    def band_names(self) -> list[str]:
        return [n for n, _, _ in self.bands]

    @property
    def region_names(self) -> list[str]:
        return [n for n, _, _ in self.regions]
