#!/usr/bin/env python3
"""Make a sync-test track: single hard kicks at uneven gaps, for measuring how
far the lights trail the sound.

Filming the rig against real music cannot give that number. The lights flash on
every beat, so a flash lines up with the previous or next beat almost as well as
with its own, and the measured lag comes out anywhere within a beat. Isolated
hits 1.1-1.9 s apart, at no fixed rhythm, leave only one way to pair each flash
with its kick.

    python tools/sync_test.py ~/Desktop/sync_test.wav

Play it through the normal speakers with the rig in manual mode on `pulse`,
film a speaker and a lit PAR in slow motion, and measure the gap with
tools/measure_sync.py.
"""

from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

import numpy as np

SR = 48000
LEAD_S = 3.0
GAP_RANGE_S = (1.1, 1.9)


def kick() -> np.ndarray:
    """A loud, short kick: a falling-pitch thump plus a click for the attack."""
    t = np.arange(int(0.25 * SR)) / SR
    freq = 50.0 + 90.0 * np.exp(-t / 0.03)
    body = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t / 0.08)
    click = np.random.default_rng(1).normal(0, 1, len(t)) * np.exp(-t / 0.002)
    return (0.9 * body + 0.25 * click).astype(np.float32)


def render(seconds: float, seed: int = 7) -> tuple[np.ndarray, list[float]]:
    rng = np.random.default_rng(seed)
    out = np.zeros(int(seconds * SR), dtype=np.float32)
    k = kick()
    times, t = [], LEAD_S
    while t + 0.3 < seconds:
        i = int(t * SR)
        out[i : i + len(k)] += k[: len(out) - i]
        times.append(round(t, 4))
        t += rng.uniform(*GAP_RANGE_S)
    return np.clip(out, -1, 1), times


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--seconds", type=float, default=60.0)
    args = ap.parse_args()
    sig, times = render(args.seconds)
    with wave.open(str(args.out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        pcm = (sig * 32767).astype("<i2")
        w.writeframes(np.repeat(pcm, 2).tobytes())
    args.out.with_suffix(".json").write_text(json.dumps({"kicks_s": times}, indent=1))
    print(f"{args.out}: {len(times)} kicks over {args.seconds:.0f}s")


if __name__ == "__main__":
    main()
