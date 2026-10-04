#!/usr/bin/env python3
"""Confirm the loopback tap is actually hearing the music.

Run this before a party, with music playing. It is the fastest way to tell the
three failure modes apart:

  * BlackHole not installed      -> no matching device
  * Multi-Output not selected    -> device found, but silence
  * Working                      -> levels move with the music

    python tools/audio_check.py
    python tools/audio_check.py --device BlackHole --seconds 20
    python tools/audio_check.py --list
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partylights.audio.analyser import Analyser
from partylights.audio.capture import AudioCapture, list_input_devices
from partylights.config import Settings, setup_logging


def bar(value: float, width: int = 28) -> str:
    n = int(round(max(0.0, min(1.0, value)) * width))
    return "#" * n + "." * (width - n)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", help="substring of the input device name")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--list", action="store_true", help="list inputs and exit")
    ap.add_argument("--log", default="WARNING")
    args = ap.parse_args()
    setup_logging(args.log)

    if args.list:
        for d in list_input_devices():
            print(f"{d['index']:>4}  {d['channels']}ch  {d['default_rate']:>7.0f}  {d['name']}")
        return 0

    settings = Settings.load()
    device = args.device or settings.get("audio.device", "BlackHole")

    try:
        capture = AudioCapture(
            device=device,
            sample_rate=int(settings.get("audio.sample_rate", 48000)),
            block_size=int(settings.get("audio.block_size", 512)),
            channels=int(settings.get("audio.channels", 2)),
        )
        capture.start()
    except Exception as e:
        print(f"\nCould not open an input matching {device!r}:\n  {e}\n")
        return 1

    analyser = Analyser(sample_rate=capture.sample_rate)
    print(f"Listening to {capture.device_name!r} for {args.seconds:.0f}s. "
          f"Play something.\n")
    print(f"{'level':<30} {'dBFS':>7} {'bpm':>6} {'lock':>5}  onsets")

    last_read = 0
    heard = False
    end = time.monotonic() + args.seconds
    try:
        while time.monotonic() < end:
            total = capture.ring.total_written
            new = total - last_read
            if new > 0:
                block = capture.ring.latest(min(new, capture.sample_rate))
                last_read = total
                for st in analyser.feed(block):
                    pass
            st = analyser.state
            peak = capture.peak()
            if peak > 0.01:
                heard = True
            db = st.frame.loudness_db if st else -120.0
            bpm = st.bpm if st else 0.0
            lock = "yes" if (st and st.tempo_locked) else "-"
            hits = ""
            if st:
                hits = " ".join(r for r in ("kick", "snare", "hat") if st.onset(r))
            print(f"\r{bar(peak):<30} {db:>7.1f} {bpm:>6.1f} {lock:>5}  {hits:<18}",
                  end="", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()

    print("\n")
    stats = capture.stats()
    print(f"device   {stats['device']}")
    print(f"samples  {stats['samples']:,}")
    print(f"dropouts {stats['dropouts']}")

    if not heard:
        # The stream worked (samples arrived at the right rate) but every sample
        # was silent, so the problem is upstream of us.
        print(f"\nThe device opened and delivered {stats['samples']:,} samples, but they")
        print("were all silent. Something upstream is not feeding it:")
        if "blackhole" in stats["device"].lower():
            print("  - macOS output is not set to the Multi-Output Device that")
            print("    includes BlackHole. Check System Settings > Sound > Output.")
            print("  - or nothing is actually playing.")
        else:
            print(f"  - {stats['device']} may not have microphone permission")
            print("    (System Settings > Privacy & Security > Microphone), or")
            print("  - the room is simply quiet.")
        return 1
    if stats["dropouts"] > 10:
        print("\nA lot of dropouts. The machine is struggling — expect the lights")
        print("to stutter. On the 2019 i9 this is usually thermal throttling.")
    print("\nCapture is working.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
