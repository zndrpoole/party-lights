#!/usr/bin/env python3
"""Measure how far the lights trail the sound, from a slow-motion video of the
rig playing tools/sync_test.py's track.

    python tools/measure_sync.py IMG_0707.mov

Finds each kick in the video's own soundtrack and the moment the frame's
brightness is half way up the flash that follows, and reports the gap in real
time. Brightness is the whole frame, so keep the camera still and the room
otherwise dark; lock exposure (long-press in the Camera app) if you can. The slow-motion factor is worked
out from the kick spacing, since iPhone exports carry no capture-rate metadata.
A positive result means the lights are late: there is no setting to make them
earlier, only less late (Feel toward Sharp shortens the fade-in). A negative
result means they are early, and audio.output_delay_ms should be raised by
that much.

Needs ffmpeg.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.signal import find_peaks

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.sync_test import GAP_RANGE_S  # noqa: E402

SR = 48000
W, H = 160, 90


def video_brightness(path: Path) -> tuple[np.ndarray, float]:
    fps_txt = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    num, den = fps_txt.split("/")
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale={W}:{H},format=gray",
         "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, H * W).mean(axis=1), float(num) / float(den)


def video_audio(path: Path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR),
         "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def kick_times(audio: np.ndarray, min_gap_s: float) -> np.ndarray:
    """Onset times of the test kicks, in video seconds.

    Thresholds are local (30 s windows): an iPhone export keeps its first and
    last seconds at normal speed, and those are far louder than the slowed,
    pitched-down middle, so one global threshold sees only the ends. Each
    onset is then walked back from the peak to where the kick starts rising,
    because slowed audio spreads a kick's attack over a third of a second.
    """
    hop = 48  # 1 ms
    env = np.sqrt(np.convolve(audio * audio, np.ones(hop) / hop, "same")[::hop])
    smooth = np.convolve(env, np.ones(100) / 100, "same")
    win = 30_000
    thresh = np.empty_like(smooth)
    for i in range(0, len(smooth), win // 2):
        seg = smooth[max(0, i - win // 2) : i + win]
        lo, hi = np.percentile(seg, 40), np.percentile(seg, 99.5)
        thresh[i : i + win // 2] = lo + 0.4 * (hi - lo)
    peaks, _ = find_peaks(smooth - thresh, height=0.0, distance=int(min_gap_s * 1000))
    out = []
    for p in peaks:
        base = np.median(env[max(0, p - 6000) : max(1, p - 2500)])
        top = env[max(0, p - 500) : p + 500].max()
        on = p
        while on > max(0, p - 4000) and env[on] > base + 0.1 * (top - base):
            on -= 1
        out.append(on / 1000.0)
    return np.array(out)


def flash_time(lum: np.ndarray, fps: float, t: float, window_s: float) -> float | None:
    """When brightness crosses half way up its next rise after video time t.

    Only a real flash counts: the rise must be several grey levels, since in a
    dark room the exposure wobbles by one level and a per-frame threshold
    mistakes every wobble for a flash.
    """
    i0 = int(t * fps)
    base = float(np.median(lum[max(0, i0 - int(1.5 * fps)) : max(1, i0)]))
    seg = lum[i0 : i0 + int(window_s * fps)]
    if len(seg) == 0 or seg.max() - base < MIN_RISE:
        return None
    half = base + 0.5 * (seg.max() - base)
    j = max(1, i0 - int(0.5 * fps))
    while j < len(lum) - 1 and lum[j] < half:
        j += 1
    a, b = lum[j - 1], lum[j]
    frac = (half - a) / (b - a) if b != a else 0.0
    return (j - 1 + frac) / fps


#: Grey levels (0-255) a rise must cover to count as a flash.
MIN_RISE = 4.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--slowdown", type=float,
                    help="override the detected slow-motion factor (8 for 240 fps played at 30)")
    args = ap.parse_args()

    lum, fps = video_brightness(args.video)
    audio = video_audio(args.video)

    # The test track's kicks are 1.1-1.9 s apart, so the right factor is the
    # one at which the heard spacing, scaled back, falls in that range. (The
    # normal-speed ends of an iPhone export are a small minority of kicks.)
    slow = args.slowdown
    if slow is None:
        def fit(k):
            g = np.diff(kick_times(audio, GAP_RANGE_S[0] * 0.8 * k)) / k
            return np.mean((g > GAP_RANGE_S[0] * 0.9) & (g < GAP_RANGE_S[1] * 1.1)) if len(g) else 0.0
        slow = max((1, 2, 4, 8, 16), key=fit)
    kicks = kick_times(audio, GAP_RANGE_S[0] * 0.8 * slow)
    print(f"{args.video.name}: {len(lum)} frames at {fps:.0f} fps, slow-motion x{slow:g}, "
          f"{len(kicks)} kicks heard")

    lags = []
    for k in kicks:
        f = flash_time(lum, fps, k, 0.5 * slow)
        if f is not None:
            lags.append((f - k) / slow * 1000.0)
    if len(lags) < 3:
        print(f"only {len(lags)} kicks had a flash -- is a lit fixture in frame, on `pulse`?")
        return 1
    lags = np.array(lags)
    print(f"{len(lags)} of {len(kicks)} kicks flashed: lights trail the sound by median {np.median(lags):+.0f} ms "
          f"(middle half {np.percentile(lags, 25):+.0f} to {np.percentile(lags, 75):+.0f} ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
