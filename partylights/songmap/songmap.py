"""A song map: everything the listening pass learned about one recording.

    songs/maps/<track id>.json           the map: timing, sections, moments
    songs/fingerprints/<track id>.npz    for finding our place live (~30 KB)
    songs/frames/<track id>.npz          the raw features, for re-analysis
    songs/art/<album id>.jpg             album art, for the designs

The frames are kept so that when the analysis improves, every map can be
rebuilt in minutes from what was heard, instead of replaying 30 hours of
music. They are the only large files (about 1 MB a song) and are not checked
in; back them up with the rest of songs/ after a pass.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import beats as beats_mod
from .beats import analyse_beats
from .fingerprint import Fingerprint, build as build_fingerprint
from .frames import FPS, Frames, extract
from .moments import analyse_moments
from .structure import analyse_sections

SCHEMA = 1
#: Bumped whenever the analysis changes what it would write, so `reanalyse`
#: knows which maps are stale.
ANALYSIS_VERSION = "2026-10-06.1"

ROOT = Path(__file__).resolve().parents[2] / "songs"


class Store:
    """Where maps and their companions live. One per songs/ directory."""

    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self.maps = self.root / "maps"
        self.fingerprints = self.root / "fingerprints"
        self.frames = self.root / "frames"
        self.art = self.root / "art"
        self.log = self.root / "pass-log.jsonl"

    def ensure(self) -> None:
        for d in (self.maps, self.fingerprints, self.frames, self.art):
            d.mkdir(parents=True, exist_ok=True)

    def map_path(self, track_id: str) -> Path:
        return self.maps / f"{track_id}.json"

    def has(self, track_id: str) -> bool:
        return self.map_path(track_id).exists()

    def load(self, track_id: str) -> dict:
        return json.loads(self.map_path(track_id).read_text())

    def fingerprint(self, track_id: str) -> Fingerprint:
        return Fingerprint.load(self.fingerprints / f"{track_id}.npz")

    def track_ids(self) -> list[str]:
        return sorted(p.stem for p in self.maps.glob("*.json"))

    def save(self, track_id: str, song_map: dict, frames: Frames, fp: Fingerprint) -> None:
        self.ensure()
        fp.save(self.fingerprints / f"{track_id}.npz")
        save_frames(self.frames / f"{track_id}.npz", frames)
        write_json(self.map_path(track_id), song_map)

    def append_log(self, entry: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        with self.log.open("a") as f:
            f.write(json.dumps(entry) + "\n")


def write_json(path: Path, data: dict) -> None:
    """Write then rename, so a crash mid-save cannot leave half a map."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


# -- frames on disk ------------------------------------------------------------
#
# Bytes, not floats: levels at 0.4 dB steps and onsets at 1/255 of their own
# range are far finer than anything the analysis distinguishes, and it makes
# 500 songs half a gigabyte instead of two. Re-analysis from stored frames
# matches analysis of the original to within a millisecond (tests).

DB_LO = -100.0


def _q_db(x: np.ndarray) -> np.ndarray:
    return np.clip(np.round((x - DB_LO) / -DB_LO * 255.0), 0, 255).astype(np.uint8)


def _dq_db(q: np.ndarray) -> np.ndarray:
    return q.astype(np.float32) / 255.0 * -DB_LO + DB_LO


def save_frames(path: Path, frames: Frames) -> None:
    on = np.column_stack((frames.onset, frames.region_onset))
    scale = np.maximum(on.max(axis=0), 1e-9).astype(np.float32)
    tmp = path.with_name(path.name + ".tmp.npz")
    np.savez_compressed(
        tmp, t0=np.float64(frames.t0), fps=np.float64(FPS),
        mel=_q_db(frames.mel), bands=_q_db(frames.bands), rms=_q_db(frames.rms_db),
        chroma=np.clip(np.round(frames.chroma * 255.0), 0, 255).astype(np.uint8),
        onset=np.clip(np.round(on / scale * 255.0), 0, 255).astype(np.uint8),
        onset_scale=scale)
    os.replace(tmp, path)


def load_frames(path: Path) -> Frames:
    with np.load(path) as z:
        on = z["onset"].astype(np.float32) / 255.0 * z["onset_scale"]
        return Frames(t0=float(z["t0"]), mel=_dq_db(z["mel"]), bands=_dq_db(z["bands"]),
                      chroma=z["chroma"].astype(np.float32) / 255.0,
                      onset=on[:, 0], region_onset=on[:, 1:], rms_db=_dq_db(z["rms"]))


# -- analysis ---------------------------------------------------------------------

def _beat_accents(frames: Frames, times: np.ndarray) -> dict:
    """Per beat, how hard each region hits on it, 0..1 within the song. Lets a
    design put an accent where the snare actually is, not where it should be."""
    out = {}
    idx = np.round((times - frames.t0) * FPS).astype(int)
    for name, col in (("low", 0), ("mid", 1), ("high", 2)):
        c = frames.region_onset[:, col]
        vals = np.array([c[max(0, i - 2):i + 3].max() if 0 <= i < len(c) else 0.0
                         for i in idx])
        top = np.percentile(vals, 98) if len(vals) else 0.0
        out[name] = [round(float(min(v / top, 1.0)), 2) if top > 0 else 0.0 for v in vals]
    return out


def quality(b: beats_mod.Beats, sections: list, moments: dict) -> dict:
    """How much to trust this map, for deciding how boldly to design on it.

    1.0 means a steady grid, clear bars and a sensible structure. Under ~0.5
    the design should lean on reactive looks, not timed cues.
    """
    notes = list(b.warnings)
    score = 0.35 * b.confidence + 0.35 * b.downbeat_confidence + 0.3 * b.stability
    if len(sections) < 2:
        score *= 0.6
        notes.append("no clear sections")
    if not moments.get("ending"):
        notes.append("ending not found")
    return {"score": round(float(score), 2), "notes": notes}


def analyse_frames(frames: Frames) -> dict:
    """The map body (timing, sections, moments) from frames alone."""
    b = analyse_beats(frames)
    sections, bars = analyse_sections(frames, b)
    moments = analyse_moments(frames, b, sections, bars) if len(b.times) else {}
    return {
        "schema": SCHEMA,
        "analysis_version": ANALYSIS_VERSION,
        "timing": {
            "bpm": round(b.bpm, 2),
            "confidence": round(b.confidence, 2),
            "downbeat_confidence": round(b.downbeat_confidence, 2),
            "stability": round(b.stability, 2),
            "beats": [round(float(t), 3) for t in b.times],
            "downbeats": [int(i) for i in b.downbeats],
        },
        "sections": [s.to_dict() for s in sections],
        "moments": moments,
        "beat_accents": _beat_accents(frames, b.times) if len(b.times) else {},
        "quality": quality(b, sections, moments),
    }


def analyse(samples: np.ndarray, *, t0: float = 0.0) -> tuple[dict, Frames, Fingerprint]:
    """Analyse a whole recording. `t0` is the song time of the first sample,
    so every time in the map is song time (Spotify's position, near enough)."""
    frames = extract(samples, t0=t0)
    return analyse_frames(frames), frames, build_fingerprint(frames)


def summary(song_map: dict) -> str:
    """A few lines a human can check a map against the song by ear."""
    tr = song_map.get("track", {})
    tm = song_map["timing"]
    mo = song_map.get("moments", {})
    q = song_map.get("quality", {})
    lines = [f"{tr.get('name', '?')} — {', '.join(tr.get('artists', [])) or '?'}",
             f"  {tm['bpm']:.1f} BPM  ·  quality {q.get('score', 0):.2f}"
             + (f"  ·  {'; '.join(q.get('notes', []))}" if q.get("notes") else "")]
    for s in song_map["sections"]:
        tags = f" [{', '.join(s['tags'])}]" if s["tags"] else ""
        lines.append(f"  {_mmss(s['start'])}  {s['role']:<9} {s['label']}  "
                     f"{s['bars']:>2} bars  energy {s['energy']:.2f}{tags}")
    for d in mo.get("drops", []):
        build = f", built from {_mmss(d['build_start'])}" if "build_start" in d else ""
        lines.append(f"  {_mmss(d['time'])}  {d['kind'].upper()} (+{d['strength_db']:.0f} dB low end){build}")
    for g in mo.get("gaps", []):
        lines.append(f"  {_mmss(g['start'])}  gap, {g['beats']:.1f} beats of silence")
    for h in mo.get("hits", []):
        lines.append(f"  {_mmss(h['time'])}  hit (+{h['jump_db']:.0f} dB)")
    end = mo.get("ending", {})
    if end.get("type") == "fade":
        lines.append(f"  {_mmss(end['fade_start'])}  fades out to {_mmss(end['end'])}")
    elif end.get("type") == "hard":
        lines.append(f"  {_mmss(end['final_hit'])}  final hit, rings {end['ring_s']:.1f} s")
    return "\n".join(lines)


def _mmss(t: float) -> str:
    t = max(0.0, float(t))
    return f"{int(t // 60)}:{t % 60:05.2f}"
