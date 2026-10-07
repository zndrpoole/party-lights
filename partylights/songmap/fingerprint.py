"""Fingerprints: how the live rig finds its place in a song map.

A map is only useful if the lights know where in the song they are, to the
beat. Spotify's reported position is good to about half a second at best, and
the playback-to-speaker path adds its own delay on top. So instead the rig
listens: it keeps the last few seconds of what it hears, describes them the
way the pass described the whole song, and slides that description along the
stored one until they line up. The pass heard the song through the very same
path -- Spotify, BlackHole, this extractor -- so the two match closely, and the
match lands within a few milliseconds.

Eight channels at 50 frames a second, stored as bytes (about 30 KB a song):

    onset low / mid / high / all   when things hit, per region (log scale)
    level low / mid / highmid / high   the band levels, dB

Onsets pin the timing; levels tell a verse from a chorus that has the same
drum pattern. Every channel is normalised within the window being compared,
so a different volume on the night changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .frames import FPS, Frames

FP_FPS = FPS / 2            # 50
CHANNELS = ("on_low", "on_mid", "on_high", "on_all",
            "lv_low", "lv_mid", "lv_highmid", "lv_high")
N_ONSET = 4
DB_LO = -100.0              # level quantisation: -100..0 dB -> 0..255


def _raw_channels(frames: Frames) -> np.ndarray:
    """(n, 8) float channels at the frame rate."""
    b = frames.bands
    def pmean(cols):
        return 10.0 * np.log10(np.maximum((10.0 ** (b[:, cols] / 10.0)).sum(axis=1), 1e-20))
    onsets = np.log1p(np.column_stack((frames.region_onset, frames.onset)))
    levels = np.column_stack((pmean([0, 1]), pmean([2, 3]), b[:, 4], pmean([5, 6])))
    return np.hstack((onsets, levels)).astype(np.float32)


def _reduce(raw: np.ndarray) -> np.ndarray:
    """Halve the rate: onsets keep the larger of each pair (a hit must not be
    averaged away), levels the mean."""
    n = len(raw) // 2 * 2
    pairs = raw[:n].reshape(-1, 2, raw.shape[1])
    return np.hstack((pairs[:, :, :N_ONSET].max(axis=1), pairs[:, :, N_ONSET:].mean(axis=1)))


@dataclass
class Fingerprint:
    t0: float                  # song time of fingerprint frame 0
    data: np.ndarray           # (n, 8) uint8
    onset_scale: np.ndarray    # (4,) per onset channel: value that maps to 255

    def __len__(self) -> int:
        return len(self.data)

    def decode(self) -> np.ndarray:
        """Back to float channels in the same units as live features."""
        d = self.data.astype(np.float32)
        on = d[:, :N_ONSET] / 255.0 * self.onset_scale
        lv = d[:, N_ONSET:] / 255.0 * (-DB_LO) + DB_LO
        return np.hstack((on, lv))

    def save(self, path: Path) -> None:
        tmp = path.with_name(path.name + ".tmp.npz")
        np.savez_compressed(tmp, t0=np.float64(self.t0), data=self.data,
                            onset_scale=self.onset_scale, fps=np.float64(FP_FPS))
        tmp.replace(path)

    @staticmethod
    def load(path: Path) -> "Fingerprint":
        with np.load(path) as z:
            return Fingerprint(float(z["t0"]), z["data"], z["onset_scale"])


def build(frames: Frames) -> Fingerprint:
    raw = _reduce(_raw_channels(frames))
    on = raw[:, :N_ONSET]
    scale = np.maximum(np.percentile(on, 99.9, axis=0), 1e-6).astype(np.float32)
    q_on = np.clip(np.round(on / scale * 255.0), 0, 255)
    q_lv = np.clip(np.round((raw[:, N_ONSET:] - DB_LO) / (-DB_LO) * 255.0), 0, 255)
    # Frame j of the reduced rate is the mean of frames 2j and 2j+1.
    t0 = frames.t0 + 0.5 / FPS
    return Fingerprint(t0, np.hstack((q_on, q_lv)).astype(np.uint8), scale)


@dataclass
class Match:
    offset: float      # song time = live time + offset
    score: float       # mean correlation at the match, -1..1
    margin: float      # best score minus the best elsewhere: how unambiguous

    @property
    def confident(self) -> bool:
        return self.score >= 0.5 and self.margin >= 0.1


def _ncc(map_ch: np.ndarray, live_ch: np.ndarray) -> np.ndarray | None:
    """Pearson correlation of the live channel against every position in the
    map channel. None if the live channel is flat (nothing to match on)."""
    n = len(live_ch)
    x = live_ch - live_ch.mean()
    nx = np.linalg.norm(x)
    if nx < 1e-6 or len(map_ch) < n:
        return None
    x /= nx
    num = np.correlate(map_ch, x, mode="valid")
    c1 = np.concatenate(([0.0], np.cumsum(map_ch, dtype=np.float64)))
    c2 = np.concatenate(([0.0], np.cumsum(map_ch.astype(np.float64) ** 2)))
    s1 = c1[n:] - c1[:-n]
    s2 = c2[n:] - c2[:-n]
    var = np.maximum(s2 - s1 * s1 / n, 1e-9)
    return num / np.sqrt(var)


#: How much a candidate is marked down for sitting away from the guess, per
#: (distance / sigma) squared. Small: a clear acoustic match a second away
#: still wins over a poor one on the guess. But loop-built tracks repeat bars
#: exactly, and between identical bars the guess is the only evidence, so at
#: two bars off (4 s at 120 BPM, sigma 0.5 s) the penalty is decisive.
PRIOR_WEIGHT = 0.05


def align(fp: Fingerprint, live: Frames, *, around: float | None = None,
          search_s: float = 4.0, sigma: float | None = None) -> Match | None:
    """Where the live window sits in the song.

    `around` is a guess at the song time of the window's first frame: from
    Spotify's position when a song starts, from the last match once locked.
    The search is limited to `search_s` either side of it, which rules out
    the chorus that sounds the same three minutes later. `sigma`, how far off
    the guess may be, lets the guess break ties between identical bars. With
    no guess, the whole song is searched.
    """
    live_raw = _reduce(_raw_channels(live))
    n = len(live_raw)
    if n < FP_FPS * 2:
        return None
    ref = fp.decode()
    live_t0 = live.t0 + 0.5 / FPS
    if around is None:
        lo, hi = 0, len(ref)
    else:
        centre = int(round((around - fp.t0) * FP_FPS))
        span = int(search_s * FP_FPS)
        lo, hi = max(0, centre - span), min(len(ref), centre + span + n)
    seg = ref[lo:hi]
    if len(seg) < n:
        return None
    scores, used = None, 0
    for c in range(len(CHANNELS)):
        r = _ncc(seg[:, c].astype(np.float64), live_raw[:, c].astype(np.float64))
        if r is None:
            continue
        scores = r if scores is None else scores + r
        used += 1
    if scores is None:
        return None
    scores /= used
    if around is not None and sigma:
        cand_t = fp.t0 + (lo + np.arange(len(scores))) / FP_FPS
        scores = scores - PRIOR_WEIGHT * ((cand_t - around) / sigma) ** 2
    best = int(np.argmax(scores))
    frac = 0.0
    if 0 < best < len(scores) - 1:
        a, b, c = scores[best - 1], scores[best], scores[best + 1]
        d = a - 2 * b + c
        if d < 0:
            frac = float(np.clip(0.5 * (a - c) / d, -0.5, 0.5))
    away = np.ones(len(scores), dtype=bool)
    away[max(0, best - 5):best + 6] = False
    rival = float(scores[away].max()) if away.any() else -1.0
    song_t = fp.t0 + (lo + best + frac) / FP_FPS
    return Match(offset=song_t - live_t0, score=float(scores[best]),
                 margin=float(scores[best]) - rival)
