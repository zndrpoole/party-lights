"""Moments: the specific instants a light show can make memorable.

Sections say what part of the song this is; moments say *when* something
happens that a room feels: the drop, the build into it, the silence just
before, a stab out of nowhere, the last hit of the song. These are what the
song designs hang their biggest cues on, so each is placed to the frame and
then snapped to the hit that makes it, and each carries a strength so a
design can tell a real event from a ripple.

Everything is relative to the song itself -- its own loud level, its own low
end -- because a quiet acoustic track and a brickwalled EDM master both have
drops, at very different absolute levels.
"""

from __future__ import annotations

import numpy as np

from .beats import ONSET_BIAS_S, Beats
from .frames import FPS, Frames
from .structure import BarFeatures, Section, assign_roles

#: Below the song's loud level by this much is inaudible for our purposes.
AUDIBLE_DB = 40.0
#: A gap: the level falls this far below the last few seconds' loud level...
GAP_DB = 18.0
#: ...for at least this long.
GAP_MIN_S = 0.12
#: A drop: the low end at least this much louder just after a downbeat than
#: just before it, landing within DROP_LANDS_DB of the song's loud level.
DROP_LOW_DB = 9.0
DROP_LANDS_DB = 5.0
#: A build: bars before a drop whose low end sits at least this far under it.
BUILD_LOW_DB = 5.0
BUILD_MAX_BARS = 16
#: A stab: a jump of at least this many dB from the half-second before, landing
#: near the song's loud level. Rare by design: these are the hits people
#: notice, not every snare.
HIT_JUMP_DB = 15.0
MAX_HITS = 8
#: Fade-out: the last stretch losing at least this much, gradually.
FADE_DB = 15.0


def _smooth(x: np.ndarray, frames_: int) -> np.ndarray:
    if frames_ <= 1 or len(x) < frames_:
        return x
    k = np.ones(frames_) / frames_
    return np.convolve(x, k, mode="same")


def _t(frames: Frames, i: int) -> float:
    return float(frames.t0 + i / FPS)


def _snap_to_onset(frames: Frames, t: float, within_s: float = 0.04) -> float:
    """Move a moment onto the onset peak that makes it, so a cue lands on the
    hit rather than on the analysis frame that first noticed it."""
    i = frames.frame_at(t)
    w = int(within_s * FPS)
    lo, hi = max(0, i - w), min(len(frames), i + w + 1)
    if hi <= lo:
        return t
    j = lo + int(np.argmax(frames.onset[lo:hi]))
    return _t(frames, j) + ONSET_BIAS_S


def _band_db(frames: Frames, cols, a: int, b: int) -> float:
    a, b = max(0, a), min(len(frames), b)
    if b <= a:
        return -100.0
    p = 10.0 ** (frames.bands[a:b][:, cols] / 10.0)
    return float(10.0 * np.log10(max(p.sum(axis=1).mean(), 1e-20)))


def _level(frames: Frames, a: int, b: int) -> float:
    a, b = max(0, a), min(len(frames), b)
    if b <= a:
        return -100.0
    return float(10.0 * np.log10(max(np.mean(10.0 ** (frames.rms_db[a:b] / 10.0)), 1e-20)))


LOW = [0, 1]           # sub, bass
HIGH = [4, 5, 6]       # highmid, high, air


def analyse_moments(frames: Frames, beats: Beats, sections: list[Section],
                    bars: BarFeatures | None) -> dict:
    n = len(frames)
    if n == 0:
        return {}
    level = frames.rms_db
    loud = float(np.percentile(level, 95))
    audible = np.nonzero(level > loud - AUDIBLE_DB)[0]
    first_i, last_i = (int(audible[0]), int(audible[-1])) if len(audible) else (0, n - 1)
    first_sound, last_sound = _t(frames, first_i), _t(frames, last_i)
    beat_s = 60.0 / beats.bpm if beats.bpm > 0 else 0.5

    out: dict = {"loud_db": round(loud, 1), "first_sound": round(first_sound, 3),
                 "last_sound": round(last_sound, 3)}

    # -- gaps: the music stops -------------------------------------------
    recent = np.array([np.percentile(level[max(0, i - 800):i + 1], 90) for i in
                       range(0, n, 10)])
    recent = np.repeat(recent, 10)[:n]
    quiet = level < recent - GAP_DB
    gaps = []
    i = first_i + int(FPS)
    stop = last_i - int(FPS)
    while i < stop:
        if quiet[i]:
            j = i
            while j < stop and quiet[j]:
                j += 1
            # A gap is a stop the music comes back from, at its own level:
            # a song that simply gets quieter (into an outro) is not one.
            back = _level(frames, j, j + int(FPS))
            if (j - i) / FPS >= GAP_MIN_S and back >= recent[i] - 10.0:
                gaps.append({"start": round(_t(frames, i), 3),
                             "end": round(_snap_to_onset(frames, _t(frames, j)), 3),
                             "beats": round((j - i) / FPS / beat_s, 2)})
            i = j
        else:
            i += 1
    out["gaps"] = gaps

    # -- drops: the low end slams back in on a downbeat ---------------------
    drops = []
    bar_times = beats.bar_times
    for k in range(2, len(bar_times)):
        t = bar_times[k]
        f = frames.frame_at(t)
        pre = _band_db(frames, LOW, f - int(beat_s * FPS), f - 2)
        post = _band_db(frames, LOW, f + 2, f + int(2 * beat_s * FPS))
        lands = _level(frames, f, f + int(2 * beat_s * FPS))
        if post - pre >= DROP_LOW_DB and lands >= loud - DROP_LANDS_DB:
            if drops and k - drops[-1]["bar"] < 8:
                if post - pre <= drops[-1]["strength_db"]:
                    continue
                drops.pop()
            drops.append({"time": round(_snap_to_onset(frames, t), 3), "bar": k,
                          "strength_db": round(post - pre, 1),
                          "after_gap": any(abs(g["end"] - t) < beat_s for g in gaps)})

    # -- builds: what leads into each drop -----------------------------------
    builds = []
    for d in drops:
        k = d["bar"]
        f = frames.frame_at(bar_times[k])
        post_low = _band_db(frames, LOW, f, f + int(4 * beat_s * FPS))
        start = k
        while start - 1 >= 0 and k - (start - 1) <= BUILD_MAX_BARS:
            a = frames.frame_at(bar_times[start - 1])
            b = frames.frame_at(bar_times[start])
            if _band_db(frames, LOW, a, b) <= post_low - BUILD_LOW_DB:
                start -= 1
            else:
                break
        # A build is the part that rises into the drop. A breakdown before it
        # also lacks the low end; where the sections say one became the
        # other, the build starts there.
        inside = [sec.start_bar for sec in sections if start < sec.start_bar <= k - 2]
        if inside:
            start = max(inside)
        if k - start >= 2:
            a = frames.frame_at(bar_times[start])
            b = frames.frame_at(bar_times[k])
            half = (a + b) // 2
            rising = (_band_db(frames, HIGH, half, b) - _band_db(frames, HIGH, a, half) > 1.5
                      or frames.region_onset[half:b, 1].mean()
                      > 1.3 * frames.region_onset[a:half, 1].mean())
            builds.append({"start": round(float(bar_times[start]), 3),
                           "end": round(float(bar_times[k]), 3), "bars": k - start,
                           "drop": d["time"], "rising": bool(rising)})
            d["build_start"] = round(float(bar_times[start]), 3)
    # A drop with nothing built before it is the band stopping and slamming
    # back in: as big a moment, but with no tension to light on the way up.
    for d in drops:
        d["kind"] = "drop" if "build_start" in d else "slam"
    out["drops"] = drops
    out["builds"] = builds

    # -- lifts: a section arriving noticeably bigger (a chorus kicking in) --
    lifts = []
    for a, b in zip(sections, sections[1:]):
        if b.level_db - a.level_db >= 3.0 and not any(abs(d["time"] - b.start) < beat_s
                                                       for d in drops):
            lifts.append({"time": round(_snap_to_onset(frames, b.start), 3),
                          "gain_db": round(b.level_db - a.level_db, 1), "into": b.role})
    out["lifts"] = lifts

    # -- hits: stabs out of quieter moments ------------------------------------
    cand = []
    lv = level
    peak_next = np.maximum.reduce([np.roll(lv, -k) for k in range(4)])
    csum = np.concatenate(([0.0], np.cumsum(10.0 ** (lv / 10.0))))
    for i in range(first_i + 50, last_i):
        before = 10.0 * np.log10(max((csum[i - 3] - csum[i - 50]) / 47.0, 1e-20))
        jump = peak_next[i] - before
        if jump >= HIT_JUMP_DB and peak_next[i] >= loud - 6.0:
            cand.append((jump, i))
    hits = []
    # Moments already named elsewhere are not also hits.
    taken: list[float] = ([d["time"] for d in drops] + [x["time"] for x in lifts]
                          + [first_sound, last_sound])
    for jump, i in sorted(cand, reverse=True):
        t = _snap_to_onset(frames, _t(frames, i))
        if all(abs(t - u) > 2.0 for u in taken):
            on_beat = bool(len(beats.times) and np.min(np.abs(beats.times - t)) < 0.04)
            hits.append({"time": round(t, 3), "jump_db": round(float(jump), 1),
                         "on_beat": on_beat})
            taken.append(t)
        if len(hits) >= MAX_HITS:
            break
    out["hits"] = sorted(hits, key=lambda h: h["time"])

    # -- the ending ---------------------------------------------------------------
    sm = _smooth(level, int(FPS))
    tail_from = max(first_i, last_i - int(30 * FPS))
    tail = sm[tail_from:last_i + 1]
    ending: dict = {}
    if len(tail) > FPS * 4:
        top = float(np.percentile(sm[first_i:last_i + 1], 75))
        # The fade begins at the last point the level was still at its body.
        body = np.nonzero(tail >= top - 3.0)[0]
        fade_start = tail_from + (int(body[-1]) if len(body) else 0)
        drop_total = sm[fade_start] - sm[max(fade_start, last_i - int(FPS))]
        steepest = max((sm[i] - sm[min(last_i, i + int(0.5 * FPS))]
                        for i in range(fade_start, last_i, 5)), default=0.0)
        long_enough = (last_i - fade_start) / FPS >= 4.0
        if drop_total >= FADE_DB and long_enough and steepest < 10.0:
            ending = {"type": "fade", "fade_start": round(_t(frames, fade_start), 3),
                      "end": round(last_sound, 3)}
        else:
            # A hard ending: the last real hit, and how long it rings.
            lo = max(first_i, last_i - int(4 * FPS))
            strong = frames.onset[lo:last_i + 1]
            j = lo + int(np.argmax(strong)) if len(strong) else last_i
            final = _snap_to_onset(frames, _t(frames, j))
            ending = {"type": "hard", "final_hit": round(final, 3),
                      "ring_s": round(last_sound - final, 2), "end": round(last_sound, 3)}
    out["ending"] = ending
    end_at = ending.get("fade_start", ending.get("final_hit"))
    if end_at is not None:
        out["gaps"] = gaps = [g for g in gaps if g["start"] < end_at]
        out["hits"] = [h for h in out["hits"] if abs(h["time"] - end_at) > 0.5]

    # -- per-bar energy, and roles from what was heard ------------------------
    if bars is not None and len(bars):
        q = float(np.percentile(bars.level, 5))
        span = max(float(np.percentile(bars.level, 95)) - q, 6.0)
        out["bar_energy"] = [round(float(np.clip((v - q) / span, 0, 1)), 3) for v in bars.level]
    for s in sections:
        if any(abs(d["time"] - s.start) < beat_s for d in drops):
            s.role = "drop"
            s.tags.append("drop")
        elif any(abs(b["end"] - s.end) < 2 * beat_s and b["start"] < s.end - beat_s
                 and b["bars"] >= s.bars // 2 for b in builds):
            s.role = "build"
    assign_roles(sections)
    for lift in lifts:
        into = [sec for sec in sections if abs(sec.start - lift["time"]) < beat_s]
        if into:
            lift["into"] = into[0].role
    for g in gaps:
        for s in sections:
            if s.start <= g["start"] < s.end:
                s.tags.append("stop")
    return out
