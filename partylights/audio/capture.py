"""Live audio capture from a loopback device.

The Mac has no system loopback, so the signal path is:

    Spotify -> Multi-Output Device -> real speakers   (the room hears this)
                                   -> BlackHole       (we analyse this)

Set the Multi-Output up with your real speakers as the **Master** device and
Drift Correction enabled on BlackHole. That orientation is what makes
Multi-Output actually work; reversed, it clicks and drifts. See the README.

The PortAudio callback runs on a high-priority thread owned by CoreAudio. It does
the minimum possible: downmix to mono and copy into a preallocated ring buffer.
No FFT, no allocation, no logging. Everything expensive happens on the engine
thread, which pulls from the ring at its own pace.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger(__name__)


class RingBuffer:
    """Fixed-size circular buffer of mono float32 samples.

    Writes come from the audio callback, reads from the engine thread. The lock
    is held only across a memcpy, which is microseconds — far cheaper than the
    tearing a lock-free version would have to tolerate.
    """

    def __init__(self, capacity: int):
        self._buf = np.zeros(capacity, dtype=np.float32)
        self._capacity = capacity
        self._write = 0
        self._total = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total_written(self) -> int:
        with self._lock:
            return self._total

    def push(self, block: np.ndarray) -> None:
        n = len(block)
        if n >= self._capacity:
            # Pathologically large block: keep only the newest capacity samples.
            block = block[-self._capacity:]
            n = len(block)
        with self._lock:
            end = self._write + n
            if end <= self._capacity:
                self._buf[self._write:end] = block
            else:
                split = self._capacity - self._write
                self._buf[self._write:] = block[:split]
                self._buf[: n - split] = block[split:]
            self._write = end % self._capacity
            self._total += n

    def latest(self, n: int, delay: int = 0) -> np.ndarray:
        """Copy of the `n` most recent samples, ending `delay` samples ago.

        `delay` is how the output-delay compensation is implemented: analysing
        slightly older audio is exactly what it means to make the lights wait for
        the speakers. Costs nothing but buffer length.
        """
        n = min(n, self._capacity)
        with self._lock:
            end = (self._write - delay) % self._capacity
            start = end - n
            if start >= 0:
                return self._buf[start:end].copy()
            return np.concatenate((self._buf[start:], self._buf[:end]))

    def clear(self) -> None:
        with self._lock:
            self._buf.fill(0.0)
            self._write = 0
            self._total = 0


def list_input_devices() -> list[dict]:
    """Every input-capable device, for the CLI and for error messages."""
    import sounddevice as sd

    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            out.append({
                "index": idx,
                "name": dev["name"],
                "channels": dev["max_input_channels"],
                "default_rate": dev["default_samplerate"],
            })
    return out


def find_device(name_fragment: str) -> int:
    """Resolve a device by case-insensitive substring. Raises with the options.

    Matching on a fragment rather than an index matters because CoreAudio device
    indices shuffle when anything is plugged in or unplugged — including, at a
    party, someone's headphones.
    """
    devices = list_input_devices()
    frag = name_fragment.lower()
    for dev in devices:
        if frag in dev["name"].lower():
            return dev["index"]
    available = ", ".join(repr(d["name"]) for d in devices) or "none"
    raise RuntimeError(
        f"no input device matching {name_fragment!r}. Available inputs: {available}. "
        "If BlackHole is missing: brew install --cask blackhole-2ch"
    )


class AudioCapture:
    """An input stream feeding a ring buffer.

    `dropouts` counts callbacks that reported an overflow. A handful at startup
    is normal; a steadily climbing count means the machine is too busy and the
    lights will stutter — on the 2019 i9, usually thermal throttling.
    """

    def __init__(
        self,
        *,
        device: str | int | None = None,
        sample_rate: int = 48000,
        block_size: int = 512,
        channels: int = 2,
        buffer_seconds: float = 12.0,
    ):
        self.device_spec = device
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.channels = channels
        self.ring = RingBuffer(int(sample_rate * buffer_seconds))

        self._stream = None
        self._device_index: int | None = None
        self._device_name = ""
        self.dropouts = 0
        self._peak = 0.0
        self._lock = threading.Lock()
        # Preallocated downmix weights, so the callback never allocates them.
        self._weights = np.full((channels, 1), 1.0 / channels, dtype=np.float32)

    @property
    def device_name(self) -> str:
        return self._device_name

    def _callback(self, indata, frames, time_info, status):
        if status:
            # input_overflow is the one that matters; anything else is noise.
            if getattr(status, "input_overflow", False) or status:
                self.dropouts += 1
        # Downmix to mono. A dot product against preallocated weights keeps this
        # allocation-free on the audio thread.
        mono = (indata @ self._weights).reshape(-1)
        self.ring.push(mono)
        peak = float(np.abs(mono).max()) if mono.size else 0.0
        with self._lock:
            self._peak = max(self._peak * 0.92, peak)

    def peak(self) -> float:
        """Decaying peak level, 0..1. For the UI meter and for 'is it silent?'."""
        with self._lock:
            return self._peak

    def start(self) -> None:
        import sounddevice as sd

        if isinstance(self.device_spec, str):
            self._device_index = find_device(self.device_spec)
        else:
            self._device_index = self.device_spec

        info = sd.query_devices(self._device_index) if self._device_index is not None else None
        self._device_name = info["name"] if info else "default"
        channels = min(self.channels, info["max_input_channels"]) if info else self.channels
        if channels != self.channels:
            self.channels = channels
            self._weights = np.full((channels, 1), 1.0 / channels, dtype=np.float32)

        self._stream = sd.InputStream(
            device=self._device_index,
            samplerate=self.sample_rate,
            blocksize=self.block_size,
            channels=self.channels,
            dtype="float32",
            callback=self._callback,
            # PortAudio's default is its "high" latency, which on BlackHole is
            # an ~85 ms host buffer delivered in bursts: measured, blocks were a
            # median 48 ms old on arrival and the lights trailed the room by
            # ~100 ms, with hits clumped onto an 85 ms grid. "low" delivers
            # each 512-sample block as it lands (10.7 ms, steady).
            latency="low",
        )
        self._stream.start()
        log.info("Capturing from %r at %d Hz, %d ch, %d-sample blocks",
                 self._device_name, self.sample_rate, self.channels, self.block_size)

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def stats(self) -> dict:
        return {
            "device": self._device_name,
            "sample_rate": self.sample_rate,
            "block_size": self.block_size,
            "channels": self.channels,
            "dropouts": self.dropouts,
            "peak": round(self.peak(), 4),
            "samples": self.ring.total_written,
        }


class FilePlayback:
    """Feeds an audio file into the ring buffer in real time, silently.

    A stand-in for AudioCapture (same ring, same stats), for designing looks
    at a laptop: the stage view reacts to a known track exactly as it would to
    the room, with no loopback routing and nothing coming out of the speakers.
    Loops at the end. Decodes with ffmpeg, so any format it reads works.
    """

    def __init__(self, path: str, *, sample_rate: int = 48000, block_size: int = 512,
                 buffer_seconds: float = 12.0):
        self.path = path
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.channels = 1
        self.ring = RingBuffer(int(sample_rate * buffer_seconds))
        self.dropouts = 0
        self._samples: np.ndarray | None = None
        self._pos = 0
        self._peak = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def device_name(self) -> str:
        return f"file: {self.path}"

    def _decode(self) -> np.ndarray:
        import shutil
        import subprocess

        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found — brew install ffmpeg")
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(self.path),
             "-f", "f32le", "-ac", "1", "-ar", str(self.sample_rate), "-"],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')}")
        return np.frombuffer(proc.stdout, dtype=np.float32).copy()

    def _run(self) -> None:
        import time

        period = self.block_size / self.sample_rate
        due = time.monotonic()
        samples = self._samples
        while not self._stop.is_set():
            end = self._pos + self.block_size
            block = samples[self._pos:end]
            self._pos = end if end < len(samples) else 0
            self.ring.push(block)
            self._peak = max(self._peak * 0.92, float(np.abs(block).max()) if block.size else 0.0)
            # Pace by the clock rather than sleeping a fixed period, so the
            # file plays at true speed however long each push takes.
            due += period
            slack = due - time.monotonic()
            if slack > 0:
                time.sleep(slack)

    def peak(self) -> float:
        return self._peak

    def start(self) -> None:
        self._samples = self._decode()
        if not len(self._samples):
            raise RuntimeError(f"{self.path}: no audio")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="FilePlayback", daemon=True)
        self._thread.start()
        log.info("Playing %s into the analyser (%.1f s, silent, looping)",
                 self.path, len(self._samples) / self.sample_rate)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def stats(self) -> dict:
        return {
            "device": self.device_name,
            "sample_rate": self.sample_rate,
            "block_size": self.block_size,
            "channels": 1,
            "dropouts": 0,
            "peak": round(self.peak(), 4),
            "samples": self.ring.total_written,
            "position_s": round(self._pos / self.sample_rate, 1),
        }
