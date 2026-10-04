"""The DMX universe and the thread that ships it.

One writer thread owns the output. Everything else — the engine, the web UI, cue
handlers — mutates a shared value buffer under a short lock. The writer snapshots
that buffer and hands the snapshot to the driver *outside* the lock, so a slow or
stalled driver can never block the engine, and a slow engine can never stop the
output. The fixtures keep receiving the last known good frame either way.

Frame length is deliberately not fixed at 512. DMX512 permits a short frame, and
513 bytes at 250 kbaud with two stop bits takes 22.6 ms to clock out — which caps
you near 44 Hz and leaves no timing headroom. Sending only the slots we actually
patched (94 for the current rig) drops that to about 4 ms, which buys both a
higher refresh rate and visibly lower latency.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

from .driver import Driver

log = logging.getLogger(__name__)

DMX_SLOTS = 512
#: Some fixtures sulk at very short frames; never go below this many slots.
MIN_SLOTS = 32

_QOS_CLASS_USER_INTERACTIVE = 0x21


def try_raise_thread_priority() -> bool:
    """Ask macOS to treat the calling thread as latency-sensitive. Best effort.

    Returns True if the request was accepted. Failure is not an error — it just
    means we rely on ordinary scheduling, which is usually fine until the machine
    gets busy. (The 2019 i9 gets busy.)
    """
    try:
        libsystem = ctypes.CDLL("/usr/lib/libSystem.dylib")
        rc = libsystem.pthread_set_qos_class_self_np(_QOS_CLASS_USER_INTERACTIVE, 0)
        return rc == 0
    except Exception:
        return False


class Universe:
    """A 512-slot DMX universe with a steady-rate output thread.

    Channel numbers in the public API are 1-based, matching every fixture menu,
    address label and DMX chart in existence. The internal buffer is 0-based.
    """

    def __init__(self, driver: Driver, *, slot_count: int = DMX_SLOTS, refresh_hz: float = 40.0):
        self.driver = driver
        self.refresh_hz = refresh_hz
        self._slot_count = max(MIN_SLOTS, min(DMX_SLOTS, slot_count))

        self._lock = threading.Lock()
        self._values = bytearray(DMX_SLOTS)
        self._snapshot = bytearray(DMX_SLOTS)
        # Preallocated source for blackout and clear, so neither allocates.
        self._zeros = bytes(DMX_SLOTS)

        self._frozen = False
        self._blackout = False

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        # Stats, for the UI and for diagnosing a flickering rig.
        self._frames = 0
        self._late = 0
        self._fps = 0.0
        self._priority_boosted = False

    # -- geometry ---------------------------------------------------------

    @property
    def slot_count(self) -> int:
        return self._slot_count

    def set_slot_count(self, n: int) -> None:
        """Grow/shrink the transmitted frame. Called once the patch is known."""
        with self._lock:
            self._slot_count = max(MIN_SLOTS, min(DMX_SLOTS, n))
        log.info("DMX frame length set to %d slots (~%.1f ms on the wire)",
                 self._slot_count, (self._slot_count + 1) * 11 / 250.0)

    # -- writing ----------------------------------------------------------

    def set(self, channel: int, value: int) -> None:
        """Set one 1-based channel to 0..255."""
        if not 1 <= channel <= DMX_SLOTS:
            raise ValueError(f"channel {channel} out of range 1..{DMX_SLOTS}")
        with self._lock:
            self._values[channel - 1] = value & 0xFF

    def set_block(self, start_channel: int, values) -> None:
        """Write consecutive channels starting at a 1-based address."""
        n = len(values)
        if not 1 <= start_channel or start_channel + n - 1 > DMX_SLOTS:
            raise ValueError(f"block {start_channel}..{start_channel + n - 1} out of range")
        with self._lock:
            self._values[start_channel - 1 : start_channel - 1 + n] = bytes(values)

    def clear(self) -> None:
        """Zero every slot. The engine's own idea of state is unaffected."""
        with self._lock:
            self._values[:] = self._zeros

    # -- live controls ----------------------------------------------------

    @property
    def blackout(self) -> bool:
        return self._blackout

    def set_blackout(self, on: bool) -> None:
        """Force every transmitted slot to zero without disturbing the buffer.

        This is the panic button, so it acts at the very last moment before the
        wire — nothing upstream can accidentally override it.
        """
        with self._lock:
            self._blackout = bool(on)
        log.info("Blackout %s", "ON" if on else "off")

    @property
    def frozen(self) -> bool:
        return self._frozen

    def set_freeze(self, on: bool) -> None:
        """Hold the current output. Buffer writes are ignored until released."""
        with self._lock:
            self._frozen = bool(on)
        log.info("Freeze %s", "ON" if on else "off")

    # -- reading ----------------------------------------------------------

    def snapshot(self) -> bytes:
        """What was last handed to the driver — i.e. what the fixtures see."""
        with self._lock:
            return bytes(self._snapshot[: self._slot_count])

    def buffer(self) -> bytes:
        """What the engine most recently asked for, ignoring freeze/blackout."""
        with self._lock:
            return bytes(self._values[: self._slot_count])

    def stats(self) -> dict:
        with self._lock:
            return {
                "frames": self._frames,
                "late": self._late,
                "fps": round(self._fps, 1),
                "target_fps": self.refresh_hz,
                "slots": self._slot_count,
                "blackout": self._blackout,
                "frozen": self._frozen,
                "priority_boosted": self._priority_boosted,
                "driver": self.driver.describe(),
            }

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="DMXWriter", daemon=True)
        self._thread.start()

    def stop(self, *, blackout: bool = True) -> None:
        """Stop writing. By default sends a final all-zero frame first.

        Leaving fixtures latched on after the process exits is the single most
        annoying failure mode of a hobby rig: there is nothing left running to
        turn them off.
        """
        if self._thread is None:
            return
        if blackout:
            self.set_blackout(True)
            time.sleep(max(0.05, 3.0 / self.refresh_hz))
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        self._priority_boosted = try_raise_thread_priority()
        log.info("DMX writer started at %.1f Hz (priority boost: %s)",
                 self.refresh_hz, "yes" if self._priority_boosted else "no")

        period = 1.0 / self.refresh_hz
        next_due = time.perf_counter()
        window_start = next_due
        window_frames = 0

        while not self._stop.is_set():
            # Compose the frame under the lock, then release it before touching
            # the driver. A serial write is milliseconds; holding the lock across
            # it would stall every engine tick and every UI request.
            with self._lock:
                n = self._slot_count
                if self._blackout:
                    self._snapshot[:n] = self._zeros[:n]
                elif not self._frozen:
                    self._snapshot[:n] = self._values[:n]
                # frozen and not blacked out: leave _snapshot exactly as it is
                self._frames += 1

            self.driver.send(memoryview(self._snapshot)[:n])
            window_frames += 1

            now = time.perf_counter()
            if now - window_start >= 1.0:
                with self._lock:
                    self._fps = window_frames / (now - window_start)
                window_start, window_frames = now, 0

            next_due += period
            slack = next_due - time.perf_counter()
            if slack > 0.002:
                # Sleep most of it, then spin the remainder: sleep() alone drifts.
                time.sleep(slack - 0.001)
                while time.perf_counter() < next_due:
                    pass
            elif slack > 0:
                while time.perf_counter() < next_due:
                    pass
            else:
                # We missed the deadline. Resync rather than accumulate debt,
                # otherwise one hiccup makes every later frame late too.
                with self._lock:
                    self._late += 1
                next_due = time.perf_counter()

        log.info("DMX writer stopped after %d frames (%d late)", self._frames, self._late)
