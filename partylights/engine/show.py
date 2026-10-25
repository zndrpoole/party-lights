"""Per-song shows: a song's design card, played against the song's clock.

A design card (songs/designs/<track id>.yaml) is a lighting designer's plan for
one song: its palette, how hard it goes, a look per section, and timed cues for
the moments the song's map found -- a stop, a build, a drop, the final hit.

While a show runs it is in charge. The engine asks it, every tick, which look
and palette to use and how bright, and it draws its cues over the top. The
engine's own guesswork about builds and drops stands down, since the card
already knows where they are.

The clock is song time, in seconds, anchored by whoever knows where the song
is: the jukebox poller (`follow`, every couple of seconds while the party
runs), a player that reads Spotify's position (tools/playshow.py), and later
the live fingerprint lock. `sync()` re-anchors only on a real disagreement, so
a jittery position reading does not make the show stutter.

Cues, all in song time:

    blackout  [at, until]   PARs, par1/par2 and the accent's colour out; uv: off
                            kills UV too
    flash     at            everything to full (white by default), decaying fast
    slam      at            the bass lights (group: bass) to every emitter at full,
                            held for `hold` s then eased out over `fade` s; pair
                            it with a blackout ending at `at` for a cut-then-slam
    build     [at, until]   gate the PARs on the beat grid at a rate that climbs
                            (1/2 notes to 1/16), whiten and level ramping across
    strobe    [at, until]   the fixtures' own strobe in white; skipped when the
                            vibe is under vibe_min, capped by the strobe limit
    fade      at, over      to: black fades everything out and holds it dark;
                            to: <look> is a look change, already done by sections
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..fixtures.color import BLACK, Emission, clamp, hsv, mix
from ..fixtures.patch import Patch
from . import palette as palettes
from .palette import Palette

log = logging.getLogger(__name__)

SONGS = Path(__file__).resolve().parents[2] / "songs"
WHITE = (1.0, 1.0, 1.0)

#: Seconds for a cue's flash to fall to a third. Short enough to read as a hit.
FLASH_TAU_S = 0.25
#: Below this effective vibe, each section uses its `calm:` look.
CALM_BELOW = 0.4
#: A position reading this far from the running clock re-anchors it. Spotify's
#: readings wobble by about a tenth of a second; a seek is seconds.
RESYNC_S = 0.15
#: With no sync for this long the player has gone, so the show ends.
STALE_S = 6.0
#: How long after the song's last sound the show lets go.
END_PAD_S = 3.0
#: Build gate rates, in notes per beat: 1/2 notes, then 1/4, 1/8, 1/16.
BUILD_RATES = (0.5, 1.0, 2.0, 4.0)
#: A slam's hold and fade, in seconds, when the card does not say.
SLAM_HOLD_S = 0.5
SLAM_FADE_S = 1.5


def song_seconds(text) -> float:
    """"1:20.03" or a plain number of seconds."""
    if isinstance(text, (int, float)):
        return float(text)
    m = re.fullmatch(r"\s*(?:(\d+):)?(\d+(?:\.\d+)?)\s*(s)?\s*", str(text))
    if not m:
        raise ValueError(f"not a song time: {text!r}")
    return int(m.group(1) or 0) * 60 + float(m.group(2))


def ramp(text, default: tuple[float, float]) -> tuple[float, float]:
    """"0.0 → 0.8" (or "->"), or one number held throughout."""
    if text is None:
        return default
    if isinstance(text, (int, float)):
        return float(text), float(text)
    parts = re.split(r"\s*(?:→|->)\s*", str(text).strip())
    return float(parts[0]), float(parts[-1])


@dataclass
class Section:
    at: float
    look: str
    calm: str | None = None
    level: float = 1.0
    note: str = ""


@dataclass
class Cue:
    do: str
    at: float
    until: float | None = None
    raw: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.raw.get("note") or self.do


@dataclass
class Card:
    track: str
    name: str
    palette: Palette | None      # None: wear the rig's current palette
    vibe: float
    sections: list[Section]
    cues: list[Cue]
    beats: list[float]
    bar_s: float
    end: float

    @classmethod
    def load(cls, track_id: str, root: Path = SONGS) -> "Card":
        import yaml

        design = yaml.safe_load((root / "designs" / f"{track_id}.yaml").read_text())
        song_map = json.loads((root / "maps" / f"{track_id}.json").read_text())

        # The palette: the card's own colours, a stock palette by name
        # ({stock: blood-moon}), or none ({from: pool}, and generated cards):
        # then the show wears whatever the rig's palette is, so the host's
        # pool and auto switching carry on as usual.
        pal = design.get("palette") or {}
        colors = tuple(hsv(*c["hsv"]) for c in pal.get("colors", []) if "hsv" in c)
        if colors:
            palette = Palette(pal.get("name", track_id), colors,
                              "from the song's design card", theme="misc")
        elif pal.get("stock"):
            palette = palettes.get(str(pal["stock"]))
        else:
            palette = None

        sections = sorted(
            (Section(song_seconds(s["at"]), s["look"], s.get("calm"),
                     float(s.get("level", 1.0)), s.get("note", ""))
             for s in design.get("sections", [])), key=lambda s: s.at)
        cues = sorted(
            (Cue(c["do"], song_seconds(c["at"]),
                 song_seconds(c["until"]) if "until" in c else None, c)
             for c in design.get("cues", [])), key=lambda c: c.at)

        timing = song_map["timing"]
        moments = song_map.get("moments", {})
        end = float(moments.get("last_sound") or song_map["track"].get("duration_ms", 0) / 1000)
        name = song_map.get("track", {}).get("name", track_id)
        return cls(track_id, name, palette, float(design.get("vibe", 0.5)), sections, cues,
                   [float(b) for b in timing["beats"]], 240.0 / float(timing["bpm"]), end)

    def section_at(self, t: float) -> Section | None:
        i = bisect.bisect_right([s.at for s in self.sections], t) - 1
        return self.sections[i] if i >= 0 else None

    def beat_phase(self, t: float) -> float:
        """Beats since the song's first beat, fractional, from the map's grid."""
        b = self.beats
        if len(b) < 2:
            return 0.0
        i = bisect.bisect_right(b, t) - 1
        if i < 0:
            return (t - b[0]) / (b[1] - b[0])
        if i >= len(b) - 1:
            return (len(b) - 1) + (t - b[-1]) / (b[-1] - b[-2])
        return i + (t - b[i]) / (b[i + 1] - b[i])


class ShowRunner:
    """The show in play, if any. Started and synced from web threads; read by
    the engine tick. One lock, held briefly."""

    def __init__(self, patch: Patch, *, root: Path = SONGS, clock=time.monotonic):
        self.patch = patch
        self.root = root
        self.clock = clock
        self._lock = threading.Lock()
        self.card: Card | None = None
        self._anchor = 0.0           # clock() at song time 0
        self._synced = 0.0
        #: Cues already fired once (flashes), by index.
        self._fired: set[int] = set()
        self._flash = 0.0
        self._flash_rgb = WHITE
        #: The palette in play: the card's, or the rig's for a card without one.
        self._palette: Palette | None = None
        #: Who started the show in play: "jukebox" for follow(), else "manual".
        self._source = "manual"
        #: Tracks whose card or map failed to load, so a bad file is reported
        #: once rather than on every poll.
        self._broken: set[str] = set()
        self._aliases: dict[str, str] | None = None
        #: Whether follow() may start shows. The host can switch it off live.
        self.follow_enabled = True

    # -- control (any thread) --------------------------------------------

    def resolve(self, track_id: str) -> str | None:
        """The track id whose show this track plays, or None without one.

        The playlist was de-duplicated, so the jukebox may play another release
        of a mapped song; songs/aliases.json maps those to the kept track.
        """
        if not track_id:
            return None
        if self._aliases is None:
            try:
                self._aliases = json.loads((self.root / "aliases.json").read_text()).get("by_id", {})
            except (OSError, ValueError):
                self._aliases = {}
        for tid in (track_id, self._aliases.get(track_id)):
            if tid and tid not in self._broken and (self.root / "designs" / f"{tid}.yaml").exists() \
                    and (self.root / "maps" / f"{tid}.json").exists():
                return tid
        return None

    def follow(self, track_id: str, position_s: float | None, playing: bool,
               at: float | None = None, *, allowed: bool = True) -> dict | None:
        """The jukebox's latest reading: start, keep in step or end this song's show.

        `at` is when the position was read, on this runner's clock. Shows it
        started end when the song is paused, has no card, or `allowed` is False
        (the host is driving the lights by hand); a show started by hand is
        left alone.
        """
        track = self.resolve(track_id) if (allowed and self.follow_enabled) else None
        with self._lock:
            ours = self.card is not None and self._source == "jukebox"
            current = self.card.track if self.card is not None else None
        if track is None or not playing or position_s is None:
            if ours:
                self.stop()
            return None
        if current is not None and not ours and current != track:
            return None       # a hand-started show of another song keeps going
        position = position_s + (self.clock() - at if at is not None else 0.0)
        try:
            result = self.sync(track, position)
        except Exception as e:
            self._broken.add(track)
            log.warning("Show for %s won't load, playing auto instead: %s", track, e)
            return None
        if result.get("started"):
            with self._lock:
                self._source = "jukebox"
        return result

    def start(self, track_id: str, position_s: float) -> Card:
        card = Card.load(track_id, self.root)
        now = self.clock()
        with self._lock:
            self.card = card
            self._anchor = now - position_s
            self._synced = now
            self._fired = set()
            self._flash = 0.0
            self._source = "manual"
        log.info("Show: %s from %.1fs", card.name, position_s)
        return card

    def sync(self, track_id: str, position_s: float) -> dict:
        """A fresh reading of where the song is. Starts the show if this is a
        new track; re-anchors only on a real disagreement."""
        with self._lock:
            same = self.card is not None and self.card.track == track_id
        if not same:
            self.start(track_id, position_s)
            return {"started": True}
        now = self.clock()
        with self._lock:
            drift = (now - self._anchor) - position_s
            self._synced = now
            if abs(drift) > RESYNC_S:
                self._anchor = now - position_s
                # A seek back must be able to fire its flashes again.
                if drift > 0:
                    self._fired = {i for i in self._fired
                                   if self.card.cues[i].at < position_s}
                log.info("Show re-synced by %.2fs", -drift)
            return {"started": False, "drift_s": round(drift, 3)}

    def stop(self) -> None:
        with self._lock:
            if self.card is not None:
                log.info("Show stopped: %s", self.card.name)
            self.card = None

    @property
    def active(self) -> bool:
        return self.card is not None

    def song_time(self) -> float | None:
        with self._lock:
            return None if self.card is None else self.clock() - self._anchor

    def snapshot(self) -> dict | None:
        with self._lock:
            card = self.card
            if card is None:
                return None
            t = self.clock() - self._anchor
        sec = card.section_at(t)
        return {"track": card.track, "name": card.name, "t": round(t, 2),
                "section": sec.note if sec else None, "look": sec.look if sec else None,
                "source": self._source}

    # -- the tick ---------------------------------------------------------

    def effective_vibe(self, host_vibe: float) -> float:
        """The card's vibe, shifted by the host's slider (0.5 = as designed)."""
        card = self.card
        return clamp((card.vibe if card else 0.5) + (host_vibe - 0.5))

    def plan(self, host_vibe: float, rig_palette: Palette | None = None
             ) -> tuple[str, Palette | None, float] | None:
        """Look, palette and level for now, or None when no show is running
        (or the show has just ended). The palette is the card's, else
        `rig_palette`, the one the rig would wear anyway."""
        with self._lock:
            card = self.card
            if card is None:
                return None
            now = self.clock()
            t = now - self._anchor
            if t > card.end + END_PAD_S or now - self._synced > STALE_S:
                log.info("Show over: %s", card.name)
                self.card = None
                return None
        pal = card.palette or rig_palette
        self._palette = pal
        sec = card.section_at(t)
        if sec is None:
            return card.sections[0].look if card.sections else "ambient", pal, 0.0
        look = sec.calm if (sec.calm and self.effective_vibe(host_vibe) < CALM_BELOW) else sec.look
        return look, pal, sec.level

    def _lead(self):
        """The palette's first colour, for flashes and builds over dark fixtures."""
        pal = (self.card.palette if self.card else None) or self._palette
        return pal.at(0) if pal is not None else WHITE

    def shade(self, emissions: dict[str, Emission], host_vibe: float, dt: float,
              max_strobe_s: float) -> dict[str, Emission]:
        """Draw the cues over the rendered, smoothed show."""
        with self._lock:
            card = self.card
            if card is None:
                return emissions
            t = self.clock() - self._anchor
            fired = self._fired
        out = dict(emissions)
        pars = self.patch.group("pars") or self.patch.ordered()
        accents = self.patch.group("accent")
        # The RGB bass lights go dark with the PARs; the accent keeps its UV.
        dark = pars + [f for f in self.patch.group("bass") if f not in accents and f not in pars]
        vibe = self.effective_vibe(host_vibe)

        def each(fixtures, fn):
            for f in fixtures:
                out[f.fid] = fn(out.get(f.fid, BLACK))

        for i, cue in enumerate(card.cues):
            if cue.do == "flash":
                # Fire once as the clock passes it, even if a tick skipped
                # over; never for a flash the song is already well past.
                if i not in fired and cue.at <= t < cue.at + 0.5:
                    with self._lock:
                        fired.add(i)
                        self._flash = 1.0
                        self._flash_rgb = (WHITE if cue.raw.get("color", "white") == "white"
                                           else self._lead())
                continue
            if cue.do == "fade":
                continue        # to black: below, over the flash; to a look: sections
            if cue.do == "slam":
                continue        # below, over the flash
            if cue.until is None or not (cue.at <= t < cue.until):
                continue
            x = (t - cue.at) / max(cue.until - cue.at, 1e-3)
            if cue.do == "blackout":
                each(dark, lambda em: Emission(em.rgb, 0.0, 0.0, 0.0, em.emitter_bias))
                keep_uv = cue.raw.get("uv", True) not in (False, "off")
                each(accents, lambda em: Emission(em.rgb, 0.0, 0.0,
                                                  em.uv if keep_uv else 0.0, em.emitter_bias))
            elif cue.do == "build":
                w0, w1 = ramp(cue.raw.get("whiten"), (0.0, 0.8))
                l0, l1 = ramp(cue.raw.get("level"), (0.5, 1.0))
                whiten, level = w0 + (w1 - w0) * x, l0 + (l1 - l0) * x
                rate = BUILD_RATES[min(len(BUILD_RATES) - 1, int(x * len(BUILD_RATES)))]
                gate = 1.0 if (card.beat_phase(t) * rate) % 1.0 < 0.5 else 0.15
                each(pars, lambda em: Emission(mix(em.rgb if max(em.rgb) > 0 else
                                                   self._lead(), WHITE, whiten),
                                               level * gate * max(em.intensity, 0.6),
                                               em.strobe, em.uv, em.emitter_bias))
            elif cue.do == "strobe":
                if vibe < float(cue.raw.get("vibe_min", 0.0)):
                    continue
                if max_strobe_s > 0 and t - cue.at > max_strobe_s:
                    continue
                each(self.patch, lambda em: Emission(WHITE, 1.0, 0.55, 0.0, 1.0))

        # The flash goes over the other cues: a hit lands over a build or a
        # blackout's edge.
        if self._flash > 0.01:
            k = self._flash
            each(self.patch, lambda em: Emission(mix(em.rgb, self._flash_rgb, k),
                                                 max(em.intensity, k), em.strobe, em.uv,
                                                 max(em.emitter_bias, k)))
            self._flash *= math.exp(-dt / FLASH_TAU_S)
        else:
            self._flash = 0.0

        # A slam goes over the flash and the blackout it usually follows.
        for cue in card.cues:
            if cue.do != "slam" or t < cue.at:
                continue
            hold = float(cue.raw.get("hold", SLAM_HOLD_S))
            fade = float(cue.raw.get("fade", SLAM_FADE_S))
            x = clamp((t - cue.at - hold) / max(fade, 1e-3))
            k = 1.0 - x * x * (3.0 - 2.0 * x)
            if k <= 0.0:
                continue
            group = self.patch.group(cue.raw.get("fixtures", "bass"))
            # Every emitter stays full; the dimmer does the fade.
            each(group, lambda em: Emission(WHITE, k, 0.0, em.uv, 1.0, boost=1.0))

        # A fade to black takes everything down with it, the last hit included.
        for cue in card.cues:
            if cue.do == "fade" and cue.raw.get("to") == "black" and t >= cue.at:
                over = song_seconds(cue.raw.get("over", 1.0))
                k = clamp(1.0 - (t - cue.at) / max(over, 1e-3))
                each(self.patch, lambda em: Emission(em.rgb, em.intensity * k, em.strobe,
                                                     em.uv * k, em.emitter_bias))
        return out
