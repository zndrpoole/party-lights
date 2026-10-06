#!/usr/bin/env python3
"""Generate synthetic test tracks with known tempo and known hit counts.

Having ground truth matters: without it, "the onset detector looks about right"
is the best claim you can make about a real song, and that is not enough to tune
against. Here we know exactly how many kicks, snares and hats exist and exactly
when they happen.

The drums are deliberately band-limited to roughly where real drums sit. An
earlier version of this generator used raw white noise for snares and hats,
which put broadband energy in every analysis region and made the detector look
like it was cross-triggering when actually the test signal was at fault.

    python tools/make_test_audio.py out/
    python tools/make_test_audio.py out/ --bpm 128 140
"""

from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

SR = 48000


def _noise(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 1.0, n)


def _band(sig: np.ndarray, lo: float | None, hi: float | None) -> np.ndarray:
    if lo and hi:
        sos = butter(4, [lo / (SR / 2), hi / (SR / 2)], btype="bandpass", output="sos")
    elif lo:
        sos = butter(4, lo / (SR / 2), btype="highpass", output="sos")
    else:
        sos = butter(4, hi / (SR / 2), btype="lowpass", output="sos")
    return sosfilt(sos, sig)


def kick(dur: float = 0.26) -> np.ndarray:
    """Pitched sine sweep, low-passed. Energy essentially all below 150 Hz."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    freq = 45.0 + 60.0 * np.exp(-55.0 * t)
    sig = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-22.0 * t)
    return _band(sig, None, 180.0).astype(np.float32)


def snare(dur: float = 0.16) -> np.ndarray:
    """Band-passed noise plus a 190 Hz body. Nothing below 150 or above 5k."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    body = np.sin(2 * np.pi * 190.0 * t) * 0.35
    sig = (_noise(n, 11) * 0.8 + body) * np.exp(-26.0 * t)
    return _band(sig, 200.0, 5000.0).astype(np.float32)


def hat(dur: float = 0.06) -> np.ndarray:
    """High-passed noise. Nothing below 6 kHz, where a real hi-hat lives."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = _noise(n, 22) * np.exp(-95.0 * t)
    return _band(sig, 6000.0, None).astype(np.float32)


def render(bpm: float, bars: int = 16, *, hats: bool = True, snares: bool = True,
           level: float = 1.0) -> tuple[np.ndarray, dict]:
    """A 4/4 pattern: kick on 1 and 3, snare on 2 and 4, hat on every offbeat."""
    beat = 60.0 / bpm
    total = int(bars * 4 * beat * SR) + SR
    out = np.zeros(total, dtype=np.float32)
    truth: dict[str, list[float]] = {"kick": [], "snare": [], "hat": []}

    k, s, h = kick(), snare(), hat()

    def place(sig: np.ndarray, at: float, gain: float, name: str) -> None:
        i = int(at * SR)
        j = min(total, i + len(sig))
        if j <= i:
            return
        out[i:j] += sig[: j - i] * gain
        truth[name].append(round(at, 4))

    for b in range(bars * 4):
        at = b * beat
        if b % 4 in (0, 2):
            place(k, at, 0.95 * level, "kick")
        if snares and b % 4 in (1, 3):
            place(s, at, 0.55 * level, "snare")
        if hats:
            place(h, at + beat / 2, 0.40 * level, "hat")

    return np.clip(out, -1.0, 1.0), truth


def bass_note(dur: float, freq: float = 55.0) -> np.ndarray:
    """A sub-bass note with a soft attack -- the low end a drop brings back."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    env = np.minimum(1.0, t / 0.01) * np.exp(-3.0 * t)
    return (np.sin(2 * np.pi * freq * t) * env).astype(np.float32)


def riser(dur: float) -> np.ndarray:
    """Noise plus an upward sine sweep, getting louder: the build's tension."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    ramp = (t / dur) ** 2
    sweep = np.sin(2 * np.pi * np.cumsum(300.0 + 2700.0 * t / dur) / SR)
    sig = _band(_noise(n, 33), 800.0, 9000.0) * 0.6 + sweep * 0.25
    return (sig * ramp).astype(np.float32)


#: The EDM arc track, in bars: (section, bars). Builds end with a bar with no
#: kick and no bass -- the gap before the drop.
ARC_SECTIONS = (("intro", 8), ("groove", 16), ("build", 8), ("drop", 16),
                ("breakdown", 8), ("build", 8), ("drop", 16))


def render_arc(bpm: float = 128.0) -> tuple[np.ndarray, dict]:
    """A dance track with a known arc, for checking the song-arc director.

    intro       kicks and hats, quiet, no bass
    groove      four-on-the-floor, snare on 2 and 4, offbeat hats, bass
    build       kicks on, bass out, snare roll doubling every 2 bars, riser;
                the last bar drops the kick (the gap)
    drop        everything, louder, bass on every beat
    breakdown   no drums; a soft pad
    """
    beat = 60.0 / bpm
    bars = sum(n for _, n in ARC_SECTIONS)
    total = int(bars * 4 * beat * SR) + SR
    out = np.zeros(total, dtype=np.float32)
    k, s, h = kick(), snare(), hat()
    b = bass_note(beat * 0.9)
    truth: dict = {"bpm": bpm, "sections": [], "drops": [], "gaps": []}

    def place(sig, at, gain):
        i = int(at * SR)
        j = min(total, i + len(sig))
        if j > i:
            out[i:j] += sig[: j - i] * gain

    bar0 = 0
    for name, n in ARC_SECTIONS:
        start = bar0 * 4 * beat
        truth["sections"].append({"name": name, "at": round(start, 3), "bars": n})
        if name == "drop":
            truth["drops"].append(round(start, 3))
        for bar in range(n):
            for q in range(4):
                at = (bar0 + bar) * 4 * beat + q * beat
                last_bar = bar == n - 1
                if name == "intro":
                    place(k, at, 0.4)
                    place(h, at + beat / 2, 0.2)
                elif name in ("groove", "drop"):
                    lvl = 1.0 if name == "drop" else 0.7
                    place(k, at, 0.95 * lvl)
                    place(b, at, (0.7 if name == "drop" else 0.45))
                    if q in (1, 3):
                        place(s, at, 0.55 * lvl)
                    place(h, at + beat / 2, 0.4 * lvl)
                elif name == "build":
                    if not last_bar:
                        place(k, at, 0.8)
                    # Snare roll: quarters, then eighths, then sixteenths.
                    div = 1 if bar < n // 4 else 2 if bar < n // 2 else 4
                    for d in range(div):
                        place(s, at + d * beat / div, 0.25 + 0.35 * (bar + 1) / n)
                elif name == "breakdown":
                    if q == 0:
                        pad = np.sin(2 * np.pi * 330.0 * np.arange(int(4 * beat * SR)) / SR)
                        place((pad * 0.15).astype(np.float32), at, 1.0)
        if name == "build":
            place(riser(n * 4 * beat), start, 0.5)
            truth["gaps"].append(round(start + (n - 1) * 4 * beat, 3))
        bar0 += n
    truth["length"] = round(total / SR, 2)
    return np.clip(out, -1.0, 1.0), truth


def save(path: Path, sig: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((sig * 32767).astype("<i2").tobytes())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--bpm", type=float, nargs="*",
                    default=[90, 110, 128, 140, 174])
    ap.add_argument("--bars", type=int, default=16)
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)

    manifest = {}
    for bpm in args.bpm:
        sig, truth = render(bpm, args.bars)
        name = f"click_{int(bpm)}.wav"
        save(args.outdir / name, sig)
        manifest[name] = {"bpm": bpm, "counts": {k: len(v) for k, v in truth.items()},
                          "times": truth}
        print(f"{name}: {len(sig)/SR:.1f}s  "
              + "  ".join(f"{k}={len(v)}" for k, v in truth.items()))

    # Quiet intro then full drop, for structure detection.
    intro, _ = render(128, args.bars // 2, hats=False, snares=False, level=0.25)
    full, _ = render(128, args.bars // 2)
    sig = np.concatenate([intro, full])
    save(args.outdir / "drop_128.wav", sig)
    manifest["drop_128.wav"] = {"bpm": 128, "drop_at": round(len(intro) / SR, 2)}
    print(f"drop_128.wav: {len(sig)/SR:.1f}s  drop at {len(intro)/SR:.1f}s")

    # A dance track with builds, gaps and drops, for the song-arc director.
    sig, truth = render_arc(128)
    save(args.outdir / "edm_arc_128.wav", sig)
    manifest["edm_arc_128.wav"] = truth
    print(f"edm_arc_128.wav: {len(sig)/SR:.1f}s  drops at {truth['drops']}")

    (args.outdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nground truth -> {args.outdir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
