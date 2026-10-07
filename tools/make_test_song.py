#!/usr/bin/env python3
"""Synthetic *songs* with known structure, for grading the listening pass.

make_test_audio.py proves the drum detectors; this proves the song maps. Each
song has verses and choruses with their own chords, a breakdown, a build with
a riser and a silent gap, a drop, stabs, and a hard or faded ending -- and a
truth file saying exactly when every beat, bar, section and moment is. A map
of a real song cannot be graded; a map of one of these can, to the frame.

    python tools/make_test_song.py out/                  # pop and edm, 2 endings
    python tools/make_test_song.py out/ --bpm 96 --lead 0.7
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.make_test_audio import SR, _band, _noise, hat, kick, riser, save, snare  # noqa: E402

#: Chords as semitones above A2 (110 Hz): root position triads.
CHORDS = {
    "Am": (0, 3, 7), "F": (-4, 0, 3), "C": (3, 7, 10), "G": (-2, 2, 5),
    "Em": (-5, -2, 2), "Dm": (-7, -4, 0), "E": (-5, -1, 2),
}
#: Progressions, one chord per bar, cycled through a section.
PROGRESSIONS = {
    "intro": ("Am", "Am", "F", "F"),
    "verse": ("Am", "Em", "Am", "Em"),
    "chorus": ("F", "G", "C", "C"),
    "bridge": ("Dm", "E", "Dm", "E"),
    "breakdown": ("F", "F", "Dm", "Dm"),
    "build": ("G", "G", "G", "G"),
    "drop": ("F", "G", "C", "C"),
    "outro": ("Am", "Am", "F", "F"),
}

#: (role, bars). Labels in the truth: sections sharing a role share a label.
POP = (("intro", 8), ("verse", 16), ("chorus", 16), ("verse", 16), ("chorus", 16),
       ("bridge", 8), ("chorus", 16), ("outro", 8))
EDM = (("intro", 16), ("verse", 16), ("breakdown", 8), ("build", 8), ("drop", 16),
       ("verse", 16), ("build", 8), ("drop", 16), ("outro", 8))


def _tone(freq: float, n: int, kind: str = "pad") -> np.ndarray:
    t = np.arange(n) / SR
    if kind == "pad":
        sig = (np.sin(2 * np.pi * freq * t) + 0.5 * np.sin(4 * np.pi * freq * t)
               + 0.25 * np.sin(6 * np.pi * freq * t))
        env = np.minimum(1.0, t / 0.05) * np.minimum(1.0, (n / SR - t) / 0.05)
    else:  # pluck
        sig = np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(4 * np.pi * freq * t)
        env = np.minimum(1.0, t / 0.005) * np.exp(-4.0 * t)
    return (sig * env).astype(np.float32)


def chord(name: str, dur: float, kind: str = "pad") -> np.ndarray:
    n = int(dur * SR)
    out = np.zeros(n, dtype=np.float32)
    for semi in CHORDS[name]:
        out += _tone(220.0 * 2 ** (semi / 12), n, kind)
    return out / 3.0


def bass(name: str, dur: float) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 55.0 * 2 ** (CHORDS[name][0] / 12)
    env = np.minimum(1.0, t / 0.008) * np.exp(-2.5 * t)
    return (np.sin(2 * np.pi * f * t) * env).astype(np.float32)


def stab(dur: float = 0.4) -> np.ndarray:
    """A brass-like hit: loud, broadband, short."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = sum(np.sign(np.sin(2 * np.pi * f * t)) for f in (220.0, 277.0, 330.0))
    sig = _band(sig + 0.3 * _noise(n, 5), 150.0, 8000.0)
    return (sig * np.exp(-7.0 * t)).astype(np.float32)


def render_song(form=POP, bpm: float = 120.0, *, lead: float = 0.5, ending: str = "hard",
                seed: int = 1) -> tuple[np.ndarray, dict]:
    """Render a song. `lead` seconds of silence before the first beat."""
    beat = 60.0 / bpm
    bar_s = 4 * beat
    bars = sum(n for _, n in form)
    total = int((lead + bars * bar_s + 3.0) * SR)
    out = np.zeros(total, dtype=np.float32)
    k, s, h = kick(), snare(), hat()
    rng = np.random.default_rng(seed)

    def place(sig, at, gain):
        i = int(round(at * SR))
        j = min(total, i + len(sig))
        if j > i >= 0:
            out[i:j] += sig[: j - i] * gain

    truth = {"bpm": bpm, "lead": lead, "beats": [], "downbeats": [], "sections": [],
             "drops": [], "gaps": [], "stabs": [], "ending": ending}
    labels: dict[str, str] = {}
    bar0 = 0
    for si, (role, n) in enumerate(form):
        start = lead + bar0 * bar_s
        label = labels.setdefault(role, "ABCDEFGH"[len(labels)])
        truth["sections"].append({"role": role, "label": label, "start": round(start, 4),
                                  "bars": n})
        prog = PROGRESSIONS[role]
        loud = {"intro": 0.35, "verse": 0.6, "chorus": 0.9, "bridge": 0.55,
                "breakdown": 0.3, "build": 0.7, "drop": 1.0, "outro": 0.4}[role]
        for bar in range(n):
            at_bar = start + bar * bar_s
            name = prog[bar % len(prog)]
            last = bar == n - 1
            gap_bar = role == "build" and last
            truth["downbeats"].append(round(at_bar, 4))
            if not gap_bar:
                place(chord(name, bar_s, "pad"), at_bar, 0.18 * loud + 0.05)
            for q in range(4):
                at = at_bar + q * beat
                truth["beats"].append(round(at, 4))
                if gap_bar:
                    continue
                if role in ("verse", "chorus", "drop", "bridge"):
                    kick_beats = (0, 1, 2, 3) if role == "drop" else (0, 2)
                    if q in kick_beats:
                        place(k, at, 0.95 * loud)
                    if q in (1, 3):
                        place(s, at, 0.55 * loud)
                    place(h, at + beat / 2, 0.35 * loud)
                    place(bass(name, beat * 0.95), at, 0.55 * loud)
                    if role in ("chorus", "drop"):
                        place(chord(name, beat * 0.5, "pluck"), at + beat / 2, 0.2)
                elif role == "intro":
                    if bar >= n // 2:
                        place(h, at + beat / 2, 0.15)
                elif role == "outro":
                    if q == 0:
                        place(k, at, 0.4)
                elif role == "build":
                    place(k, at, 0.7)
                    div = 1 if bar < n // 4 else 2 if bar < n // 2 else 4
                    for d in range(div):
                        place(s, at + d * beat / div, 0.2 + 0.4 * (bar + 1) / n)
        if role == "build":
            place(riser((n - 1) * bar_s), start, 0.45)
            truth["gaps"].append({"start": round(start + (n - 1) * bar_s, 4),
                                  "end": round(start + n * bar_s, 4)})
        if role == "drop":
            truth["drops"].append(round(start, 4))
        if role == "chorus" and si == len(form) - 2:
            # A stab out of a stop: the bar before is cut to nothing but this.
            at = start + (n - 2) * bar_s
            truth["stabs"].append(round(at, 4))
            i0 = int(round((at - bar_s) * SR))
            out[i0:int(round(at * SR))] *= 0.02
            place(stab(), at, 1.4)
        bar0 += n

    end = lead + bars * bar_s
    if ending == "hard":
        place(k, end, 1.0)
        place(chord("Am", 1.5, "pluck"), end, 0.6)
        truth["final_hit"] = round(end, 4)
        truth["downbeats"].append(round(end, 4))
        truth["beats"].append(round(end, 4))
    else:
        fade_bars = 8
        f0 = end - fade_bars * bar_s
        i0, i1 = int(f0 * SR), int(end * SR)
        out[i0:i1] *= np.linspace(1.0, 0.0, i1 - i0) ** 2
        out[i1:] = 0.0
        truth["fade_start"] = round(f0, 4)
    out += rng.normal(0, 1e-4, total).astype(np.float32)   # a noise floor, like a real file
    peak = np.abs(out).max()
    out = out / peak * 0.9 if peak > 0 else out
    truth["length"] = round(total / SR, 3)
    return out, truth


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--bpm", type=float, default=None)
    ap.add_argument("--lead", type=float, default=0.5)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    cases = [("pop_hard", POP, 118.0, "hard"), ("pop_fade", POP, 96.0, "fade"),
             ("edm_hard", EDM, 128.0, "hard")]
    for name, form, bpm, ending in cases:
        sig, truth = render_song(form, args.bpm or bpm, lead=args.lead, ending=ending)
        save(args.outdir / f"{name}.wav", sig)
        (args.outdir / f"{name}.json").write_text(json.dumps(truth, indent=1))
        print(f"{name}: {truth['length']:.0f} s, {len(truth['sections'])} sections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
