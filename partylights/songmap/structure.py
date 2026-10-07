"""Sections: where the song changes, and which parts are the same part.

Works bar by bar on the beat grid, because songs are built from bars and every
light cue should land on one. Each bar gets a description of its harmony
(chroma), its timbre (MFCCs) and its level in each band; bars are compared
with every other bar, and a section boundary is a bar where the past stops
looking like the future (Foote's checkerboard novelty, 2000). Boundaries
prefer the phrase grid -- 4- and 8-bar multiples from the first downbeat --
because that is where songwriters put them.

Sections that repeat are then given the same label by comparing them bar
against bar, in order, so a verse matches a verse by its chord sequence and
not just by its average sound. Roles (intro, verse, chorus, bridge, breakdown,
drop, outro) come from the labels, the order and the energy, the way a
listener names them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .beats import Beats
from .frames import FPS, Frames

#: Half-width, in bars, of the novelty kernel: four bars before against four
#: after, which is the shortest unit a section is built from.
KERNEL_BARS = 4
#: No two boundaries closer than this, in bars.
MIN_SECTION_BARS = 4
#: Boundary score multipliers on the phrase grid.
PHRASE_BONUS = 1.35
DOUBLE_PHRASE_BONUS = 1.15
#: Two sections whose bars match, in order, at least this well share a label.
SAME_LABEL = 0.55
N_MFCC = 13


@dataclass
class Section:
    start: float
    end: float
    start_bar: int
    bars: int
    label: str
    role: str = "section"
    energy: float = 0.0          # 0..1 within the song
    level_db: float = 0.0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3),
                "start_bar": self.start_bar, "bars": self.bars, "label": self.label,
                "role": self.role, "energy": round(self.energy, 3),
                "level_db": round(self.level_db, 1), "tags": self.tags}


def _dct_matrix(n_in: int, n_out: int) -> np.ndarray:
    k = np.arange(n_out)[:, None]
    i = np.arange(n_in)[None, :]
    return np.cos(np.pi * k * (2 * i + 1) / (2 * n_in))


def bar_bounds(beats: Beats, frames: Frames) -> np.ndarray:
    """Bar start times plus the end of the last bar."""
    bars = beats.bar_times
    if len(bars) < 2:
        return bars
    last = bars[-1] + float(np.median(np.diff(bars)))
    end = frames.t0 + len(frames) / FPS
    return np.append(bars, min(last, end))


def _power_mean_db(db: np.ndarray, axis=0) -> np.ndarray:
    return 10.0 * np.log10(np.maximum(np.mean(10.0 ** (db / 10.0), axis=axis), 1e-20))


@dataclass
class BarFeatures:
    starts: np.ndarray       # bar start times (len n) plus end (n+1)
    chroma: np.ndarray       # (n, 12)
    mfcc: np.ndarray         # (n, 12)  c1..c12
    bands: np.ndarray        # (n, 7) dB
    level: np.ndarray        # (n,) dB
    onset: np.ndarray        # (n, 3) mean region onset

    def __len__(self) -> int:
        return len(self.level)


def bar_features(frames: Frames, bounds: np.ndarray) -> BarFeatures:
    dct = _dct_matrix(frames.mel.shape[1], N_MFCC)
    n = len(bounds) - 1
    chroma, mfcc, bands, level, onset = [], [], [], [], []
    for i in range(n):
        a = max(0, frames.frame_at(bounds[i]))
        b = max(a + 1, min(len(frames), frames.frame_at(bounds[i + 1])))
        chroma.append(frames.chroma[a:b].mean(axis=0))
        mfcc.append((dct @ frames.mel[a:b].mean(axis=0))[1:])
        bands.append(_power_mean_db(frames.bands[a:b]))
        level.append(float(_power_mean_db(frames.rms_db[a:b])))
        onset.append(frames.region_onset[a:b].mean(axis=0))
    return BarFeatures(bounds, np.array(chroma), np.array(mfcc), np.array(bands),
                       np.array(level), np.array(onset))


def _z(x: np.ndarray) -> np.ndarray:
    sd = x.std(axis=0)
    sd[sd == 0] = 1.0
    return (x - x.mean(axis=0)) / sd


def similarity(bf: BarFeatures) -> np.ndarray:
    """Bar-by-bar cosine similarity of harmony, timbre and level together."""
    feats = np.hstack((1.0 * _z(bf.chroma), 0.8 * _z(bf.mfcc), 0.6 * _z(bf.bands),
                       0.5 * _z(bf.onset)))
    norm = np.linalg.norm(feats, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    unit = feats / norm
    return unit @ unit.T


def novelty(ssm: np.ndarray, level: np.ndarray) -> np.ndarray:
    """Per bar: how much the music changes at its start."""
    n = len(ssm)
    k = KERNEL_BARS
    g = np.exp(-0.5 * (np.linspace(-1.5, 1.5, 2 * k)) ** 2)
    taper = np.outer(g, g)
    sign = np.ones((2 * k, 2 * k))
    sign[:k, k:] = -1
    sign[k:, :k] = -1
    kernel = taper * sign
    padded = np.pad(ssm, k, mode="edge")
    foote = np.array([(padded[i:i + 2 * k, i:i + 2 * k] * kernel).sum() for i in range(n)])
    foote = np.maximum(foote, 0.0)
    # A jump in level counts too: it is most of what makes an EDM section.
    lv = np.pad(level, 2, mode="edge")
    jump = np.array([abs(lv[i + 2:i + 4].mean() - lv[i:i + 2].mean()) for i in range(n)])
    def unit(x):
        m = x.max()
        return x / m if m > 0 else x
    return unit(foote) + 0.6 * unit(np.minimum(jump, 12.0))


def pick_boundaries(nov: np.ndarray) -> list[int]:
    """Bar indices where sections start, always including 0."""
    n = len(nov)
    if n < 2 * MIN_SECTION_BARS:
        return [0]
    # Which offset of the 4-bar grid the song's changes favour.
    phase = int(np.argmax([nov[p::4].sum() for p in range(4)]))
    score = nov.copy()
    for i in range(n):
        if (i - phase) % 4 == 0:
            score[i] *= PHRASE_BONUS
            if (i - phase) % 8 == 0:
                score[i] *= DOUBLE_PHRASE_BONUS
    thresh = score.mean() + 0.5 * score.std()
    peaks = [i for i in range(1, n) if score[i] >= thresh
             and score[i] == score[max(0, i - 2):i + 3].max()]
    chosen: list[int] = [0]
    for i in sorted(peaks, key=lambda i: -score[i]):
        if all(abs(i - c) >= MIN_SECTION_BARS for c in chosen) and n - i >= 2:
            chosen.append(i)
    return sorted(chosen)


def _sequence_similarity(ssm: np.ndarray, a: tuple[int, int], b: tuple[int, int]) -> float:
    """How well two bar ranges match bar against bar, allowing the shorter to
    sit anywhere inside the longer (a chorus repeated with an extra bar)."""
    (a0, a1), (b0, b1) = a, b
    la, lb = a1 - a0, b1 - b0
    if la > lb:
        (a0, a1), (b0, b1), la, lb = (b0, b1), (a0, a1), lb, la
    best = -1.0
    for off in range(0, lb - la + 1):
        diag = [ssm[a0 + k, b0 + off + k] for k in range(la)]
        best = max(best, float(np.mean(diag)))
    # A much shorter section matching part of a longer one is weaker evidence.
    return best * (0.75 + 0.25 * la / lb)


def label_sections(ssm: np.ndarray, bounds: list[int], n_bars: int) -> list[str]:
    spans = [(b, bounds[i + 1] if i + 1 < len(bounds) else n_bars) for i, b in enumerate(bounds)]
    parent = list(range(len(spans)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            if _sequence_similarity(ssm, spans[i], spans[j]) >= SAME_LABEL:
                parent[find(j)] = find(i)
    names: dict[int, str] = {}
    out = []
    for i in range(len(spans)):
        root = find(i)
        if root not in names:
            names[root] = "ABCDEFGHIJKLMNOP"[min(len(names), 15)]
        out.append(names[root])
    return out


#: Roles that come from hearing a specific event, not from comparing sections.
FIXED_ROLES = ("build", "drop")


def assign_roles(sections: list[Section]) -> None:
    """Name each section the way a listener would.

    Position and energy first, because they are unambiguous: a quiet opening
    is the intro, a quiet close the outro, a dip between louder parts a
    breakdown -- even when they share chords, as intros and outros often do.
    Then repetition among what is left: the loudest repeated part is the
    chorus, the most repeated other part the verse, a one-off the bridge.
    Builds and drops are heard by moments.py, which tags them "build" and
    "drop" and calls this again; those roles are kept as they are.
    """
    if not sections:
        return
    peak = max(s.energy for s in sections) or 1.0
    n = len(sections)
    for i, s in enumerate(sections):
        if s.role in FIXED_ROLES:
            continue
        s.role = ""
        if i == 0 and n > 1 and s.energy < 0.6 * peak:
            s.role = "intro"
        elif i == n - 1 and n > 1 and s.energy < 0.6 * peak:
            s.role = "outro"
        elif 0 < i < n - 1 and s.energy < 0.45 * peak and (
                s.energy < sections[i - 1].energy and s.energy < sections[i + 1].energy):
            s.role = "breakdown"

    middle = [s for s in sections if not s.role]
    counts: dict[str, int] = {}
    for s in middle:
        counts[s.label] = counts.get(s.label, 0) + 1
    repeated = [lab for lab, c in counts.items() if c >= 2]
    energy_of = {lab: float(np.mean([s.energy for s in middle if s.label == lab]))
                 for lab in counts}
    chorus = max(repeated, key=lambda l: energy_of[l]) if repeated else None
    # In a song with drops, the drops are the peak; a repeated groove that
    # does not get near them is the verse, not a chorus.
    drops = [s.energy for s in sections if s.role == "drop"]
    if chorus is not None and drops and energy_of[chorus] < 0.9 * float(np.mean(drops)):
        chorus = None
    others = [l for l in repeated if l != chorus]
    verse = max(others, key=lambda l: (counts[l], -energy_of[l])) if others else None
    for s in middle:
        if s.label == chorus:
            s.role = "chorus"
        elif s.label == verse:
            s.role = "verse"
        elif counts[s.label] == 1:
            s.role = "bridge"
        else:
            s.role = "section"
    # A song with no repeated part has no chorus to name; its loudest
    # section is still the one to save the big look for.
    if chorus is None and not repeated and not drops and middle:
        max(middle, key=lambda s: s.energy).role = "peak"


def analyse_sections(frames: Frames, beats: Beats) -> tuple[list[Section], BarFeatures]:
    bounds_t = bar_bounds(beats, frames)
    if len(bounds_t) < 2 * MIN_SECTION_BARS + 1:
        return [], bar_features(frames, bounds_t) if len(bounds_t) > 1 else None
    bf = bar_features(frames, bounds_t)
    ssm = similarity(bf)
    nov = novelty(ssm, bf.level)
    starts = pick_boundaries(nov)
    labels = label_sections(ssm, starts, len(bf))

    loud = float(np.percentile(bf.level, 95))
    quiet = float(np.percentile(bf.level, 5))
    span = max(loud - quiet, 6.0)
    sections = []
    for i, b0 in enumerate(starts):
        b1 = starts[i + 1] if i + 1 < len(starts) else len(bf)
        level = float(_power_mean_db(bf.level[b0:b1]))
        sections.append(Section(
            start=float(bounds_t[b0]), end=float(bounds_t[b1]), start_bar=b0, bars=b1 - b0,
            label=labels[i], energy=float(np.clip((level - quiet) / span, 0.0, 1.0)),
            level_db=level))
    assign_roles(sections)
    return sections, bf
