"""The analyser facade: samples in, a complete musical picture out.

Composes the three analysis stages into one object with one entry point, so the
live engine and the offline tuning tool drive identical code. That equivalence is
the point: tuning against a file is only meaningful if the file path and the live
path are the same state machine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .features import DEFAULT_BANDS, DEFAULT_REGIONS, FeatureExtractor, FeatureFrame
from .structure import StructureTracker
from .tempo import TempoTracker

log = logging.getLogger(__name__)

#: How much each onset region contributes to the beat-salience signal that
#: drives *period* estimation.
#:
#: Kick alone is tempting — it is the cleanest periodic element in most party
#: music — but it locks to the kick *pattern* rather than to the beat. On a
#: backbeat groove with kicks only on 1 and 3, the kick signal genuinely repeats
#: every two beats, and the tracker correctly reports half tempo. Measured
#: against synthetic tracks of known tempo, kick-only got 90/110/128 right and
#: halved both 140 and 174; adding snare at 0.7 got all five exact. Weighting
#: all three regions equally breaks 90 and 174, because offbeat hats pull the
#: estimate toward double time.
#:
#: Phase correction still uses kick onsets alone: the kick is the most reliable
#: anchor for *where* the beat is, even when it does not mark every beat.
TEMPO_SALIENCE_WEIGHTS = {"kick": 1.0, "snare": 0.7}


@dataclass
class MusicState:
    """Everything a look is allowed to know about the music right now."""

    frame: FeatureFrame
    bpm: float = 0.0
    beat_phase: float = 0.0
    bar_phase: float = 0.0
    phrase_phase: float = 0.0
    beat_index: int = 0
    beat: bool = False
    tempo_locked: bool = False
    tempo_confidence: float = 0.0
    events: list[str] = field(default_factory=list)
    novelty: float = 0.0
    energy_trend: float = 0.0

    # Convenience passthroughs, so looks read naturally.
    @property
    def t(self) -> float:
        return self.frame.t

    @property
    def energy(self) -> float:
        return self.frame.energy

    @property
    def silent(self) -> bool:
        return self.frame.silent

    def band(self, name: str, default: float = 0.0) -> float:
        return self.frame.bands.get(name, default)

    def smooth(self, name: str, default: float = 0.0) -> float:
        return self.frame.bands_smooth.get(name, default)

    def onset(self, region: str) -> bool:
        return self.frame.onsets.get(region, False)

    def hit(self, region: str) -> float:
        return self.frame.onset_strength.get(region, 0.0)


class Analyser:
    """Feature extraction + tempo + structure, driven by raw samples."""

    def __init__(
        self,
        *,
        sample_rate: int = 48000,
        window: int = 2048,
        hop: int = 512,
        bands=DEFAULT_BANDS,
        regions=DEFAULT_REGIONS,
        onset_sensitivity: float = 2.2,
    ):
        self.features = FeatureExtractor(
            sample_rate=sample_rate, window=window, hop=hop,
            bands=bands, regions=regions, onset_sensitivity=onset_sensitivity,
        )
        rate = self.features.rate_hz
        self.tempo = TempoTracker(rate_hz=rate)
        self.structure = StructureTracker(rate_hz=rate)
        self.state: MusicState | None = None

    @property
    def rate_hz(self) -> float:
        return self.features.rate_hz

    def feed(self, samples: np.ndarray) -> list[MusicState]:
        """Push samples; return one MusicState per completed analysis hop."""
        out: list[MusicState] = []
        for frame in self.features.feed(samples):
            # Period estimation needs kick AND snare; phase correction wants
            # kick alone. See TEMPO_SALIENCE_WEIGHTS for why.
            salience = sum(frame.flux.get(k, 0.0) * w
                           for k, w in TEMPO_SALIENCE_WEIGHTS.items())
            self.tempo.update(salience, frame.t, onset=frame.onsets.get("kick", False))
            events = self.structure.update(frame)

            out.append(MusicState(
                frame=frame,
                bpm=self.tempo.bpm,
                beat_phase=self.tempo.phase,
                bar_phase=self.tempo.bar_phase,
                phrase_phase=self.tempo.phrase_phase,
                beat_index=self.tempo.beat_index,
                beat=self.tempo.beat_fired(),
                tempo_locked=self.tempo.locked,
                tempo_confidence=self.tempo.confidence,
                events=events,
                novelty=self.structure.novelty,
                energy_trend=self.structure.energy_trend,
            ))
        if out:
            self.state = out[-1]
        return out

    def on_track_change(self) -> None:
        """New song: the previous tempo and structure are no longer evidence."""
        self.tempo.reset()
        self.structure.reset()

    def stats(self) -> dict:
        return {
            "rate_hz": round(self.rate_hz, 1),
            "frames": self.features.frames_emitted,
            **self.tempo.stats(),
            **self.structure.stats(),
        }
