"""Section, drop and breakdown detection.

Beat-level reaction makes lights move; section-level reaction makes them feel
like they understand the song. A chase that runs identically through the intro,
the drop and the breakdown reads as decoration. One that opens up when the drop
lands reads as lighting design.

The method is deliberately simple and runs in real time: hold a few seconds of
band-energy vectors, and compare the recent window against the one before it.
A large spectral change is a section boundary; the *direction* of the energy
change tells us which kind.

This is a heuristic, not music information retrieval. It will miss subtle
transitions and occasionally fire on a dramatic fill. For lighting that is an
acceptable trade: a false positive is a palette change nobody questions.
"""

from __future__ import annotations

import logging
from collections import deque

import numpy as np

log = logging.getLogger(__name__)

#: Event names emitted by `update()`.
SECTION_CHANGE = "section"
DROP = "drop"
BREAKDOWN = "breakdown"
BUILD = "build"


class StructureTracker:
    def __init__(
        self,
        *,
        rate_hz: float = 93.75,
        window_s: float = 2.0,
        decimate: int = 8,
        novelty_threshold: float = 0.12,
        dynamic_range_db: float = 60.0,
        min_gap_s: float = 4.0,
        drop_gap_s: float = 8.0,
        drop_db: float = 4.0,
    ):
        """
        window_s     how much audio each side of the comparison covers
        decimate     keep every Nth frame; 8 gives ~12 Hz, plenty for structure
        min_gap_s    debounce between section events
        drop_gap_s   debounce between drops, which should be rare by nature
        drop_db      level change across a boundary that makes it a drop or a
                     breakdown rather than merely a section change
        dynamic_range_db
                     how far below the loudest band we still care about, when
                     building the spectral-shape vector
        """
        self.novelty_threshold = novelty_threshold
        self.min_gap_s = min_gap_s
        self.drop_gap_s = drop_gap_s
        self.drop_db = drop_db
        self.dynamic_range_db = dynamic_range_db
        self.decimate = decimate

        per_window = max(4, int(window_s * rate_hz / decimate))
        self._per_window = per_window
        self._vectors: deque[np.ndarray] = deque(maxlen=per_window * 2)
        #: Normalised energy, for the trend signal -- we want "is this building
        #: relative to the track" there, which is what the AGC gives us.
        self._energy: deque[float] = deque(maxlen=per_window * 3)
        #: Raw RMS, for classifying a boundary as a drop or a breakdown. This
        #: has to be the unnormalised level: the AGC exists precisely to cancel
        #: level changes, so measuring a drop with it barely registers one. On a
        #: test track the raw level across a drop went 0.0094 -> 0.0551 (5.9x,
        #: about 15 dB) while the normalised energy moved only 0.087 -> 0.128.
        self._level: deque[float] = deque(maxlen=per_window * 2)

        self._counter = 0
        self.novelty = 0.0
        self._last_section_t = -1e9
        self._last_drop_t = -1e9
        self._last_event = ""
        self._last_event_t = -1e9

    # -- state ------------------------------------------------------------

    @property
    def last_event(self) -> str:
        return self._last_event

    def seconds_since_event(self, t: float) -> float:
        return t - self._last_event_t

    @property
    def energy_trend(self) -> float:
        """Recent energy slope, roughly -1..1. Positive means building."""
        if len(self._energy) < 8:
            return 0.0
        arr = np.fromiter(self._energy, dtype=np.float64, count=len(self._energy))
        half = len(arr) // 2
        early, late = arr[:half].mean(), arr[half:].mean()
        return float(np.clip(late - early, -1.0, 1.0))

    # -- driving ----------------------------------------------------------

    def update(self, frame) -> list[str]:
        """Feed one FeatureFrame. Returns any events that fired."""
        self._counter += 1
        if self._counter % self.decimate:
            return []

        # Store LINEAR band energy. The dB/shape transform is applied after
        # averaging, not before -- see _shape_vector for why the order matters.
        vec = np.fromiter(
            (frame.bands_raw.get(k, 0.0) for k in sorted(frame.bands_raw)),
            dtype=np.float64,
        )
        if vec.size == 0:
            return []

        self._vectors.append(vec)
        self._energy.append(frame.energy)
        self._level.append(frame.rms)

        if len(self._vectors) < self._vectors.maxlen:
            return []

        half = self._per_window
        window = list(self._vectors)
        # Average energy over each half in the linear domain, then convert each
        # average once into a dB shape vector.
        prev = _shape_vector(np.mean(np.stack(window[:half]), axis=0), self.dynamic_range_db)
        recent = _shape_vector(np.mean(np.stack(window[half:]), axis=0), self.dynamic_range_db)

        self.novelty = _cosine_distance(prev, recent)

        events: list[str] = []
        t = frame.t
        if self.novelty >= self.novelty_threshold and t - self._last_section_t >= self.min_gap_s:
            self._last_section_t = t
            events.append(SECTION_CHANGE)

            # Classify by what the *level* did across the boundary. A drop and a
            # breakdown are both big spectral changes; the sign of the level
            # change separates them. Measured in dB, which makes one threshold
            # work regardless of how the track is mastered.
            delta_db = self._level_change_db()
            if delta_db >= self.drop_db and t - self._last_drop_t >= self.drop_gap_s:
                self._last_drop_t = t
                events.append(DROP)
            elif delta_db <= -self.drop_db:
                events.append(BREAKDOWN)

        # A build is a sustained rise without a boundary -- the eight bars before
        # a drop, where lighting should be tightening rather than changing.
        elif self.energy_trend > 0.14 and t - self._last_event_t >= self.min_gap_s:
            events.append(BUILD)

        if events:
            self._last_event = events[-1]
            self._last_event_t = t
            log.debug("structure @%.1fs: %s (novelty %.3f)", t, ",".join(events), self.novelty)
        return events

    def _level_change_db(self) -> float:
        """Level change across the comparison boundary, in dB.

        Positive means the second half is louder. Compares mean RMS either side
        rather than instantaneous level, so a single loud hit cannot fake a drop.
        """
        levels = list(self._level)
        if len(levels) < 4:
            return 0.0
        half = len(levels) // 2
        before = float(np.mean(levels[:half]))
        after = float(np.mean(levels[half:]))
        floor = 1e-5
        return 20.0 * float(np.log10(max(after, floor) / max(before, floor)))

    def reset(self) -> None:
        """Called on a track change: the previous song tells us nothing now."""
        self._vectors.clear()
        self._energy.clear()
        self._level.clear()
        self._counter = 0
        self.novelty = 0.0
        self._last_section_t = -1e9
        self._last_drop_t = -1e9

    def stats(self) -> dict:
        return {
            "novelty": round(self.novelty, 3),
            "trend": round(self.energy_trend, 3),
            "level_change_db": round(self._level_change_db(), 1),
            "last_event": self._last_event,
        }


def _shape_vector(bands: np.ndarray, dynamic_range_db: float) -> np.ndarray:
    """Band energies as a gain-invariant spectral *shape* vector, in dB.

    The log domain is not a nicety here, it is the difference between the
    method working and not working. Measured across a drop on a test track:

        linear band energies       cosine distance 0.004
        L1-normalised linear       cosine distance 0.004
        dB, clamped to 60 dB       cosine distance 0.240

    Linear energy is dominated by the kick in both sections, so two sections
    that sound completely different produce near-parallel vectors. Snares and
    hats sit 25-30 dB down and move a linear vector almost not at all, while in
    dB they move it decisively.

    The floor is set relative to the loudest band rather than at an absolute
    level, so the result does not change when someone turns the party up.

    Order of operations matters and is easy to get backwards: average the band
    energies in the LINEAR domain first, then apply this transform once to each
    average. Transforming every frame and averaging the results destroys the
    contrast -- measured 0.004 that way against 0.179 the right way round.
    Because the floor is relative to each vector's own maximum, a frame captured
    between hits has everything near its noise floor and comes out as an almost
    flat vector of large values; averaging a window of those washes out exactly
    the spectral differences we are looking for.
    """
    db = 20.0 * np.log10(np.maximum(bands, 1e-9))
    return np.maximum(0.0, db - (db.max() - dynamic_range_db))


def _cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.clip(1.0 - float(a @ b) / (na * nb), 0.0, 1.0))
