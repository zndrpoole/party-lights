#!/usr/bin/env python3
"""Run the live analysis chain offline against an audio file.

The single most useful development tool in this project. The analyser here is
the same object the engine drives, fed the same way, so what you tune against a
file is what the rig does at the party — no hardware, no speakers, no waiting for
the right moment in a song.

    python tools/analyze_file.py track.mp3
    python tools/analyze_file.py track.mp3 --timeline        # per-second detail
    python tools/analyze_file.py track.mp3 --expect-bpm 128  # verify tempo
    python tools/analyze_file.py track.wav --sensitivity 1.8

Needs ffmpeg for anything that is not a plain WAV (it is already installed).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partylights.audio.analyser import Analyser
from partylights.config import setup_logging

SAMPLE_RATE = 48000


def decode(path: Path, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any audio file to mono float32 at `sample_rate`, via ffmpeg."""
    if not path.exists():
        raise SystemExit(f"no such file: {path}")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found — brew install ffmpeg")

    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-f", "f32le", "-ac", "1", "-ar", str(sample_rate), "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg failed:\n{proc.stderr.decode(errors='replace')}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def bar(value: float, width: int = 20, ch: str = "#") -> str:
    n = int(round(max(0.0, min(1.0, value)) * width))
    return ch * n + "." * (width - n)


def analyse(samples: np.ndarray, *, sensitivity: float, block: int = 4096):
    a = Analyser(sample_rate=SAMPLE_RATE, onset_sensitivity=sensitivity)
    states = []
    for i in range(0, len(samples), block):
        states.extend(a.feed(samples[i : i + block]))
    return a, states


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--sensitivity", type=float, default=2.2,
                    help="onset threshold multiplier; lower finds more hits")
    ap.add_argument("--timeline", action="store_true", help="per-second detail")
    ap.add_argument("--expect-bpm", type=float,
                    help="assert the detected tempo matches this (allows 2x/0.5x)")
    ap.add_argument("--log", default="WARNING")
    args = ap.parse_args()
    setup_logging(args.log)

    samples = decode(args.file)
    duration = len(samples) / SAMPLE_RATE
    print(f"{args.file.name}: {duration:.1f}s, {len(samples):,} samples @ {SAMPLE_RATE} Hz")

    a, states = analyse(samples, sensitivity=args.sensitivity)
    if not states:
        print("no analysis frames produced — file too short?")
        return 1

    regions = a.features.region_names
    bands = a.features.band_names

    onset_counts = {r: sum(1 for s in states if s.onset(r)) for r in regions}
    # Ignore the first couple of seconds: every normaliser is still settling.
    settled = [s for s in states if s.t > 2.0] or states
    bpms = [s.bpm for s in settled if s.bpm > 0]
    locked = [s for s in settled if s.tempo_locked]

    print(f"\nAnalysis rate: {a.rate_hz:.1f} Hz, {len(states):,} frames")
    print(f"\nTempo")
    if bpms:
        print(f"  median        {np.median(bpms):.1f} BPM")
        print(f"  range         {min(bpms):.1f} - {max(bpms):.1f} BPM")
        print(f"  locked        {100.0 * len(locked) / len(settled):.0f}% of the track")
        print(f"  confidence    {np.mean([s.tempo_confidence for s in settled]):.2f} mean")
    else:
        print("  never locked — try --sensitivity 1.6, or the track may be beatless")

    print(f"\nOnsets (per region)")
    for r in regions:
        n = onset_counts[r]
        per_min = n / (duration / 60) if duration else 0
        print(f"  {r:<6s} {n:5d}   {per_min:6.1f}/min")

    print(f"\nLevel")
    print(f"  mean energy   {np.mean([s.energy for s in settled]):.2f}")
    print(f"  silent frames {100.0 * sum(1 for s in states if s.silent) / len(states):.0f}%")

    print(f"\nBand averages")
    for b in bands:
        mean = float(np.mean([s.smooth(b) for s in settled]))
        print(f"  {b:<8s} {bar(mean)} {mean:.2f}")

    events = [(s.t, e) for s in states for e in s.events]
    print(f"\nStructure ({len(events)} events)")
    for t, e in events[:40]:
        print(f"  {int(t) // 60:02d}:{t % 60:05.2f}  {e}")
    if len(events) > 40:
        print(f"  ... and {len(events) - 40} more")

    if args.timeline:
        print(f"\nTimeline (one row per second)")
        hdr = "  time   bpm  lk  energy               " + "  ".join(f"{r[:5]:>5s}" for r in regions)
        print(hdr)
        bucket: dict[int, list] = {}
        for s in states:
            bucket.setdefault(int(s.t), []).append(s)
        for sec in sorted(bucket):
            group = bucket[sec]
            e = float(np.mean([g.energy for g in group]))
            bpm = float(np.median([g.bpm for g in group]))
            lk = "Y" if group[-1].tempo_locked else "-"
            hits = "  ".join(f"{sum(1 for g in group if g.onset(r)):5d}" for r in regions)
            print(f"  {sec // 60:02d}:{sec % 60:02d} {bpm:5.1f}   {lk}  {bar(e)} {hits}")

    if args.expect_bpm:
        if not bpms:
            print(f"\nFAIL: expected {args.expect_bpm} BPM, never locked")
            return 1
        got = float(np.median(bpms))
        # Accept octave relatives: locking to half or double is a different kind
        # of result than being simply wrong, and worth reporting as such.
        for mult, label in ((1.0, "exact"), (2.0, "double"), (0.5, "half")):
            if abs(got - args.expect_bpm * mult) / (args.expect_bpm * mult) < 0.04:
                verdict = "PASS" if mult == 1.0 else f"PASS ({label} tempo)"
                print(f"\n{verdict}: expected {args.expect_bpm}, detected {got:.1f}")
                return 0
        print(f"\nFAIL: expected {args.expect_bpm} BPM, detected {got:.1f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
