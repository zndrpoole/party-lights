"""Tempo, beats and bars for a whole song, offline.

The live tempo tracker has to guess from the last few seconds and commit as it
goes. Here the whole song is in hand, so this does what it cannot: estimates
the tempo from every beat in the song at once, then finds the best-fitting beat
sequence end to end by dynamic programming (Ellis, "Beat Tracking by Dynamic
Programming", 2007) -- each beat chosen knowing where the next one lands.

Bars come from a small Viterbi pass over the beats: each beat is in position
1..4 of its bar, positions advance one per beat, and the downbeat is the
position where the bass hits and the harmony changes. A song that drops or
adds a beat somewhere (a 2/4 bar before a chorus is common) can change phase,
but only where the evidence clearly says so.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .frames import FPS, Frames

#: Tempo search range, BPM. Below 60 is a half-time reading of something
#: faster; above 200 is double time.
MIN_BPM, MAX_BPM = 60.0, 200.0
#: Log-normal prior on tempo: centred at 120 BPM, one octave wide. Breaks the
#: half/double ambiguity the way a listener does, towards a danceable pulse.
PRIOR_BPM, PRIOR_OCTAVES = 120.0, 0.9
#: How strongly the beat tracker holds the tempo. Higher is stiffer; 100 is
#: the usual value for pop, and party music is mostly on a grid.
TIGHTNESS = 120.0
#: SuperFlux compares frames two apart (frames.FLUX_LAG), so a hit's onset
#: peaks a little after the attack. Measured on the synthetic test songs, whose
#: hit times are known exactly: +7 to +10 ms across 96-174 BPM. Taken off so
#: beat times are when the drum is heard. tests/test_songmap.py holds it there.
ONSET_BIAS_S = -0.008
#: Bar positions. Party music is overwhelmingly in four.
METER = 4
#: Viterbi cost of a beat breaking the bar sequence, in units of downbeat
#: evidence (z-scores). High: a phase change needs several bars of agreement.
PHASE_JUMP_COST = 6.0


@dataclass
class Beats:
    bpm: float
    times: np.ndarray            # seconds, every beat
    downbeats: np.ndarray        # indices into times
    confidence: float            # 0..1, how clearly the tempo stands out
    downbeat_confidence: float   # 0..1
    stability: float             # 0..1, 1 = metronomic
    warnings: list[str]

    @property
    def bar_times(self) -> np.ndarray:
        return self.times[self.downbeats]


#: How much each onset region counts towards the beat. The kick and snare
#: carry the pulse; hats and shakers mostly sit *between* beats, and since they
#: light up a wide stretch of the spectrum at once, an unweighted flux lets
#: them pull the tracker onto the offbeat. Measured on test songs with hats on
#: every offbeat: unweighted, 7% of beats landed within 30 ms; see the tests.
REGION_WEIGHTS = (0.5, 0.35, 0.15)   # low, mid, high


def _envelope(frames: Frames) -> np.ndarray:
    """Onset strength for beat tracking: the regions weighted towards the
    pulse, each de-trended and scaled to unit spread."""
    if not len(frames):
        return np.zeros(0)
    # Remove the slow level (a loud chorus has more flux everywhere) so the
    # tracker sees hits rather than sections.
    k = int(FPS)  # 1 s
    kernel = np.ones(k) / k
    out = np.zeros(len(frames))
    for w, col in zip(REGION_WEIGHTS, frames.region_onset.T):
        o = col.astype(np.float64)
        e = np.maximum(o - np.convolve(o, kernel, mode="same"), 0.0)
        sd = e.std()
        if sd > 0:
            out += w * e / sd
    sd = out.std()
    return out / sd if sd > 0 else out


def estimate_tempo(env: np.ndarray) -> tuple[float, float]:
    """(bpm, confidence) from the autocorrelation of the onset envelope.

    Each candidate lag also collects its double, half and two-thirds, so a
    tempo is scored by how well the whole metrical grid fits, not one lag.
    """
    if len(env) < 4 * FPS:
        return 0.0, 0.0
    x = env - env.mean()
    n = len(x)
    spec = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(spec * np.conj(spec))[:n]
    ac /= ac[0] if ac[0] > 0 else 1.0

    def at(lag: float) -> float:
        i = int(lag)
        if i + 1 >= n:
            return 0.0
        f = lag - i
        return ac[i] * (1 - f) + ac[i + 1] * f

    bpms = np.arange(MIN_BPM, MAX_BPM + 0.01, 0.25)
    scores = np.empty(len(bpms))
    for j, bpm in enumerate(bpms):
        lag = 60.0 * FPS / bpm
        grid = at(lag) + 0.5 * at(2 * lag) + 0.25 * at(4 * lag) + 0.35 * at(lag / 2)
        prior = np.exp(-0.5 * (np.log2(bpm / PRIOR_BPM) / PRIOR_OCTAVES) ** 2)
        scores[j] = max(grid, 0.0) * prior
    best = int(np.argmax(scores))
    if scores[best] <= 0:
        return 0.0, 0.0
    # Parabolic refinement between candidates.
    bpm = bpms[best]
    if 0 < best < len(bpms) - 1:
        a, b, c = scores[best - 1], scores[best], scores[best + 1]
        d = a - 2 * b + c
        if d < 0:
            bpm += 0.25 * 0.5 * (a - c) / d
    # Confidence: the winner against the best candidate that is not a
    # neighbour or a simple multiple of it.
    rivals = [s for b2, s in zip(bpms, scores)
              if min(abs(np.log2(b2 / bpm) - k) for k in (-1, 0, 1, np.log2(1.5), -np.log2(1.5)))
              > 0.05]
    rival = max(rivals) if rivals else 0.0
    conf = float(np.clip(1.0 - rival / scores[best], 0.0, 1.0))
    return float(bpm), conf


def track(env: np.ndarray, bpm: float) -> np.ndarray:
    """Beat frames by dynamic programming. Returns float frame positions."""
    n = len(env)
    period = 60.0 * FPS / bpm
    lo, hi = int(round(period / 2)), int(round(period * 2))
    lags = np.arange(lo, hi + 1)
    penalty = -TIGHTNESS * np.log(lags / period) ** 2
    score = env.copy()
    back = np.full(n, -1, dtype=np.int64)
    for t in range(lo, n):
        prev = t - lags
        ok = prev >= 0
        cand = score[prev[ok]] + penalty[ok]
        j = int(np.argmax(cand))
        if cand[j] > 0:
            score[t] = env[t] + cand[j]
            back[t] = prev[ok][j]
    # The last beat: the latest strong local maximum of the cumulative score
    # in the final period, so the sequence does not end on noise.
    tail = slice(max(0, n - int(period * 1.5)), n)
    last = tail.start + int(np.argmax(score[tail]))
    beats = []
    t = last
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats = np.array(beats[::-1], dtype=np.float64)

    # Sub-frame refinement: centre each beat on the onset peak's parabola.
    out = beats.copy()
    for i, b in enumerate(beats.astype(int)):
        if 0 < b < n - 1:
            a, c, d = env[b - 1], env[b], env[b + 1]
            curv = a - 2 * c + d
            if curv < 0:
                out[i] = b + float(np.clip(0.5 * (a - d) / curv, -0.5, 0.5))
    return out


#: Beats either side used to fit the local grid. 16 is four bars: long enough
#: to average out where in each hit the detector lands, short enough to follow
#: a live drummer who speeds up into a chorus.
GRID_HALF_WINDOW = 16
#: A beat further than this from its local grid line is treated as a wobble
#: of the detector, not of the drummer, and refit without it.
GRID_OUTLIER_S = 0.035


def fit_grid(times: np.ndarray) -> np.ndarray:
    """Replace each beat with its place on the local tempo grid.

    The tracker places each beat on an onset peak, so its timing inherits
    every quirk of the hit under it -- a flam, a sidechained bass, a vocal
    landing early. Almost every party track is cut to a fixed grid, and even
    a live band holds tempo over a few bars, so a robust straight line through
    the beats around each one is closer to the truth than the beat itself.
    """
    n = len(times)
    if n < 8:
        return times
    idx = np.arange(n, dtype=np.float64)
    out = times.copy()
    for i in range(n):
        lo, hi = max(0, i - GRID_HALF_WINDOW), min(n, i + GRID_HALF_WINDOW + 1)
        x, y = idx[lo:hi], times[lo:hi]
        keep = np.ones(len(x), dtype=bool)
        for _ in range(3):
            if keep.sum() < 4:
                break
            slope, icpt = np.polyfit(x[keep], y[keep], 1)
            resid = np.abs(y - (slope * x + icpt))
            new = resid < GRID_OUTLIER_S
            if (new == keep).all():
                break
            keep = new
        out[i] = slope * i + icpt
    return out


#: A run of at least this many beats with almost no kick or snare under them
#: is not trusted to find the beat itself. See bridge_weak_runs().
WEAK_RUN_BEATS = 8
WEAK_PULSE = 0.3
#: Beats of well-marked grid used to carry the tempo into a weak run. Long,
#: because a tempo error accumulates across the run: 0.1% over a 64-beat
#: intro is 30 ms at the first beat.
BRIDGE_FIT_BEATS = 128


def pulse_strength(frames: Frames, times: np.ndarray) -> np.ndarray:
    """Per beat: the kick and snare onset right at it, relative to the song's
    typical beat. Near 0 means nothing marks the beat there."""
    if not len(times):
        return times
    pulse = frames.region_onset[:, 0] + frames.region_onset[:, 1]
    idx = np.round((times - frames.t0) * FPS).astype(int)
    n = len(pulse)
    at = np.array([pulse[max(0, i - 2):min(n, i + 3)].max() if 0 <= i < n else 0.0
                   for i in idx])
    typical = np.median(at[at > np.percentile(at, 50)]) if (at > 0).any() else 0.0
    return at / typical if typical > 0 else at


def bridge_weak_runs(times: np.ndarray, strength: np.ndarray) -> tuple[np.ndarray, int]:
    """Re-place beats where the music gives no pulse, from the grid around them.

    An intro of pads and offbeat hats, or a breakdown, has nothing on the beat
    for the tracker to find, and it will happily lock to the offbeat -- half a
    beat out, which on the lights reads as exactly wrong. A listener carries
    the beat through from where it was clear; so does this. A weak run is
    refilled at the tempo of the nearest well-marked beats, anchored to them,
    from one side (an intro or outro) or from both (a breakdown), in which case
    the beats are spread evenly so they meet the grid on the far side.
    Returns the new beat times and how many beats were re-placed.
    """
    n = len(times)
    weak = strength < WEAK_PULSE
    runs, i = [], 0
    while i < n:
        if weak[i]:
            j = i
            while j < n and weak[j]:
                j += 1
            if j - i >= WEAK_RUN_BEATS:
                # A couple of stray beats at either end of the song (a
                # count-in, a pickup) are part of the intro or outro, not a
                # grid to anchor to.
                runs.append((0 if i < 4 else i, n if n - j < 4 else j))
            i = j
        else:
            i += 1
    if not runs or weak.all():
        return times, 0

    def fit(lo: int, hi: int) -> tuple[float, float]:
        idx = np.arange(lo, hi)
        good = idx[~weak[lo:hi]]
        if len(good) < 4:
            good = idx
        keep = np.ones(len(good), dtype=bool)
        for _ in range(3):
            slope, icpt = np.polyfit(good[keep], times[good][keep], 1)
            new = np.abs(times[good] - (slope * good + icpt)) < GRID_OUTLIER_S
            if new.sum() < 4 or (new == keep).all():
                break
            keep = new
        return slope, icpt

    out = times.copy()
    replaced = 0
    pieces = []
    last = 0
    for lo, hi in runs:
        pieces.append(out[last:lo])
        span_start, span_end = times[lo], times[hi - 1]
        if lo == 0 and hi == n:
            continue
        if lo == 0:
            period, icpt = fit(hi, min(n, hi + BRIDGE_FIT_BEATS))
            anchor = period * hi + icpt
            k = int(np.floor((anchor - span_start) / period + 0.5))
            new = anchor - period * np.arange(k, 0, -1)
        elif hi == n:
            period, icpt = fit(max(0, lo - BRIDGE_FIT_BEATS), lo)
            anchor = period * (lo - 1) + icpt
            k = int(np.floor((span_end - anchor) / period + 0.5))
            new = anchor + period * np.arange(1, k + 1)
        else:
            p1, i1 = fit(max(0, lo - BRIDGE_FIT_BEATS), lo)
            p2, i2 = fit(hi, min(n, hi + BRIDGE_FIT_BEATS))
            a, b = p1 * (lo - 1) + i1, p2 * hi + i2
            period = 0.5 * (p1 + p2)
            k = max(1, int(round((b - a) / period)))
            new = a + (b - a) * np.arange(1, k) / k
        pieces.append(new)
        replaced += hi - lo
        last = hi
    pieces.append(out[last:])
    merged = np.concatenate(pieces)
    return np.sort(merged), replaced


def _trim(beats: np.ndarray, frames: Frames) -> np.ndarray:
    """Drop beats in leading and trailing silence, which the tracker fills in
    at the tempo but nobody can hear."""
    if not len(beats):
        return beats
    loud = frames.rms_db.max()
    audible = np.nonzero(frames.rms_db > loud - 35.0)[0]
    if not len(audible):
        return beats[:0]
    first, last = audible[0] - FPS * 0.1, audible[-1] + FPS * 0.1
    return beats[(beats >= first) & (beats <= last)]


def _zscore(x: np.ndarray) -> np.ndarray:
    sd = x.std()
    return (x - x.mean()) / sd if sd > 0 else np.zeros_like(x)


def downbeat_evidence(frames: Frames, beat_frames: np.ndarray) -> np.ndarray:
    """Per beat: how much it sounds like the one. Low-end hit + harmonic change
    + bass level, each as a z-score, so no one feature dominates by scale."""
    n = len(frames)
    idx = np.clip(np.round(beat_frames).astype(int), 0, n - 1)
    low = frames.region_onset[:, 0]
    low_hit = np.array([low[max(0, i - 3):i + 4].max() for i in idx])
    bass = frames.bands[:, 1]
    bass_at = np.array([bass[i:min(n, i + 10)].mean() if i < n else 0.0 for i in idx])

    # Chroma change into each beat: the harmony over the beat before against
    # the beat after. Chords change on the one far more than anywhere else.
    change = np.zeros(len(idx))
    for k in range(1, len(idx) - 1):
        a = frames.chroma[idx[k - 1]:idx[k]].mean(axis=0)
        b = frames.chroma[idx[k]:idx[k + 1]].mean(axis=0)
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na > 0 and nb > 0:
            change[k] = 1.0 - float(a @ b) / (na * nb)
    return 0.35 * _zscore(low_hit) + 0.45 * _zscore(change) + 0.20 * _zscore(bass_at)


def bar_positions(evidence: np.ndarray) -> tuple[np.ndarray, float]:
    """Viterbi over bar position per beat. Returns (positions, confidence)."""
    n = len(evidence)
    if n == 0:
        return np.zeros(0, dtype=int), 0.0
    # Emission: position 0 earns the evidence; the third beat earns a little
    # of it too, since half-bar phrasing is common, but less than the one.
    emit = np.zeros((n, METER))
    emit[:, 0] = evidence
    if METER == 4:
        emit[:, 2] = 0.25 * evidence
    score = emit[0].copy()
    back = np.zeros((n, METER), dtype=int)
    for i in range(1, n):
        new = np.empty(METER)
        for p in range(METER):
            stay = score[(p - 1) % METER]
            jump = score.max() - PHASE_JUMP_COST
            if stay >= jump:
                new[p], back[i, p] = stay, (p - 1) % METER
            else:
                new[p], back[i, p] = jump, int(np.argmax(score))
            new[p] += emit[i, p]
        score = new
    pos = np.zeros(n, dtype=int)
    pos[-1] = int(np.argmax(score))
    for i in range(n - 1, 0, -1):
        pos[i - 1] = back[i, pos[i]]

    # Confidence: mean evidence on chosen downbeats against the best of the
    # other phases, measured on the global phase.
    phase_means = [evidence[(np.arange(n) - k) % METER == 0].mean() for k in range(METER)]
    ordered = sorted(phase_means, reverse=True)
    spread = ordered[0] - ordered[1]
    conf = float(np.clip(spread / 0.8, 0.0, 1.0))
    return pos, conf


#: If the kick lands between the tracked beats nearly as hard as on them, the
#: tracker has found half the real tempo. Kick only: offbeat hats, guitar
#: strums and chord stabs all sit between beats in ordinary pop, and counting
#: them doubled a 96 BPM test song. Measured, kick between/on: 1.02 for a
#: 174 BPM track read as 87, 0.06 for every song read correctly.
DOUBLE_TIME_RATIO = 0.6


def _kick_at(frames: Frames, times: np.ndarray) -> float:
    low = frames.region_onset[:, 0]
    idx = np.round((times - frames.t0) * FPS).astype(int)
    vals = [low[max(0, i - 2):i + 3].max() for i in idx if 0 <= i < len(low)]
    return float(np.median(vals)) if vals else 0.0


def _is_half_time(frames: Frames, times: np.ndarray) -> bool:
    if len(times) < 16:
        return False
    on = _kick_at(frames, times)
    between = _kick_at(frames, 0.5 * (times[:-1] + times[1:]))
    return on > 0 and between / on >= DOUBLE_TIME_RATIO


def analyse_beats(frames: Frames) -> Beats:
    warnings: list[str] = []
    env = _envelope(frames)
    bpm, conf = estimate_tempo(env)
    if bpm <= 0:
        return Beats(0.0, np.zeros(0), np.zeros(0, dtype=int), 0.0, 0.0, 0.0,
                     ["no tempo found"])
    beat_frames = _trim(track(env, bpm), frames)
    if bpm * 2 <= MAX_BPM and _is_half_time(frames, frames.t0 + beat_frames / FPS):
        bpm *= 2
        beat_frames = _trim(track(env, bpm), frames)
        warnings.append("tempo doubled: the pulse runs between the beats too")
    raw = frames.t0 + beat_frames / FPS
    times = fit_grid(raw)
    # How much the grid had to correct, measured before any bridging (which
    # replaces beats with perfect ones and would flatter the number).
    wobble = float(np.median(np.abs(raw - times))) if len(times) else 0.0
    times, bridged = bridge_weak_runs(times, pulse_strength(frames, times))
    times = times + ONSET_BIAS_S
    beat_frames = (times - frames.t0) * FPS

    ibi = np.diff(times)
    if len(ibi) > 8:
        # Stability: how far the tracked beats sat from the fitted grid, as a
        # share of a beat. Measured: ~0.2% on a quantised test song, ~2.5% on
        # real captures (a 12-14 ms median wobble), so 5% reads as zero.
        cv = wobble / float(np.median(ibi))
        stability = float(np.clip(1.0 - cv / 0.05, 0.0, 1.0))
        # Report the tempo the beats actually keep, not the estimate.
        bpm = float(60.0 / np.median(ibi))
        if cv > 0.04:
            warnings.append(f"loose timing (beats wander {cv:.0%} of a beat off the grid)")
    else:
        stability = 0.0

    pos, dconf = bar_positions(downbeat_evidence(frames, beat_frames))
    downbeats = np.nonzero(pos == 0)[0]
    if len(np.unique((np.arange(len(pos)) - pos) % METER)) > 1:
        warnings.append("bar phase changes mid-song")
    if dconf < 0.3:
        warnings.append("downbeats uncertain")
    if conf < 0.3:
        warnings.append("tempo uncertain")
    return Beats(bpm, times, downbeats, conf, dconf, stability, warnings)
