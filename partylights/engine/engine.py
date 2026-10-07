"""The engine: audio in, DMX out, forty times a second.

The tick is deliberately boring and always the same shape:

    pull audio -> analyse -> choose a look -> render it -> blend ->
    manual override -> master dimmer -> write the universe -> test bench

Everything that could fail is contained. A look that raises is dropped for that
frame and the previous frame's values stand; a dead analyser degrades to the
ambient look; a missing jukebox is simply no metadata. The one thing that must
never happen at a party is the room going dark because of a software error, so
no exception on this path is allowed to propagate to the output thread.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import replace

import numpy as np

from ..audio.analyser import Analyser, MusicState
from ..audio.capture import AudioCapture
from ..audio.features import Envelope, FeatureFrame
from ..audio.structure import BREAKDOWN, DROP, SECTION_CHANGE
from ..dmx.universe import Universe
from ..fixtures.color import BLACK, Emission, clamp, mix
from ..fixtures.patch import Patch
from . import palette as palettes
from .arc import BREAKDOWN_STATE, BUILDING, PREDROP, ArcDirector
from .effects import EffectRack
from .looks import BY_NAME, auto_selectable, build_all
from .show import ShowRunner
from .space import Space
from .state import EngineState

log = logging.getLogger(__name__)

#: Seconds to crossfade between looks. Long enough that a change of look reads
#: as a transition rather than a cut; 0.9 felt abrupt on the real rig.
FADE_S = 1.8

#: Minimum seconds a look runs in auto mode before another may be chosen,
#: unless a structural event forces it. Without this the rig twitches between
#: looks every time the energy wobbles across a threshold.
#:
#: Measured on the *audio* clock (MusicState.t), not wall clock. The engine
#: otherwise mixes two clocks: look dwell on wall time while everything it
#: reacts to is timestamped in audio time. Live they advance together, but they
#: diverge whenever audio does not arrive in real time -- which made offline
#: testing silently misleading, and would freeze the dwell if capture stalled.
AUTO_DWELL_S = 32.0

#: With a tempo lock, auto mode changes look on phrase boundaries instead of
#: after AUTO_DWELL_S: every AUTO_PHRASES phrases (16 bars, about 30 s at
#: 128 BPM), or after half that if a section change was heard in between.
AUTO_PHRASES = 4

#: What a drop switches to, in turn. Weight first: after a build, a whole room
#: landing together hits harder than motion. Later drops in the same night get
#: the moving scenes, so the biggest moments do not all look alike.
DROP_LOOKS = ("unison", "pingpong", "knockout", "rotor")

WHITE = (1.0, 1.0, 1.0)

#: Seconds for the drop hit to fade: on a drop the whole room goes to full in
#: the current look's colours, then settles into the new look over about 1.5 s.
DROP_FLASH_TAU_S = 0.45

#: Thresholds on *sustained* energy, not instantaneous energy.
#:
#: This distinction caused a real bug. FeatureFrame.energy is peak-normalised
#: instantaneous RMS, which on a track with clear drums averages only about
#: 0.13: it is near zero between hits, and roughly 38% of frames read as
#: outright silent. Comparing that directly against a threshold made auto mode
#: flap into the idle look on every gap between kicks, so a 46-second 128 BPM
#: track never left `ambient`. Holding the peak with a slow release instead
#: gives a signal that actually means "the music is going", which is what a
#: look-selection decision wants. Looks still use instantaneous energy for
#: brightness, where the fast response is the point.
IDLE_ENERGY = 0.08
BUSY_ENERGY = 0.60
MID_ENERGY = 0.30

#: Attack/release for the sustained-energy follower, in seconds. The long
#: release is what spans the gaps between hits.
ENERGY_ATTACK_S = 0.05
ENERGY_RELEASE_S = 2.5

#: The vibe control (EngineState.vibe, 0 calm .. 1 wild; 0.5 is the auto mode
#: as written) works through four things, none of them brightness or fade
#: times, which Master and Feel already own:
#:
#: * Which looks auto mode may pick: those whose `wildness` sits inside a band
#:   that slides up with the vibe. Calm keeps to still, hitless looks; wild
#:   drops the calmest ones. See _band().
#: * How loud the music seems to auto mode. Wild reads the same track as more
#:   energetic, so it reaches the busy looks sooner; calm the reverse.
#: * How long a look holds: twice as long at calm, half at wild.
#: * How hard the song arc lands -- the build, the dark gap before a drop and
#:   the drop's flash. Full from the middle up, fading out towards calm, so a
#:   calm room is never suddenly cut dark.
#:
#: Band edges at each end of the slider. At 0.5 every auto look is inside,
#: which is what makes the middle the auto mode as written.
VIBE_CEILING_CALM = 0.25
VIBE_CEILING_SLOPE = 1.3
VIBE_FLOOR_SLOPE = 0.8
#: A band holding fewer looks than this at some energy borrows in-band looks
#: from the rest of the pool, so a calm room still rotates instead of sitting
#: on one look all night.
VIBE_MIN_CHOICES = 2


class Engine:
    def __init__(
        self,
        patch: Patch,
        universe: Universe,
        state: EngineState,
        *,
        capture: AudioCapture | None = None,
        analyser: Analyser | None = None,
        tick_hz: float = 100.0,
        output_delay_ms: float = 0.0,
        attack_ms: float = 0.0,
        release_ms: float = 0.0,
    ):
        self.patch = patch
        self.universe = universe
        self.state = state
        self.capture = capture
        self.analyser = analyser or Analyser(
            sample_rate=capture.sample_rate if capture else 48000
        )
        self.tick_hz = tick_hz
        self.output_delay_samples = int(
            (output_delay_ms / 1000.0) * (capture.sample_rate if capture else 48000)
        )

        #: Fixture positions, shared by every look. See set_layout().
        self.space = Space(patch)
        #: The host's effects -- lightning, candle, heartbeat -- over the top.
        self.effects = EffectRack(patch, self.space)
        #: A song's designed show, when one is playing. See show.py.
        self.show = ShowRunner(patch)
        #: Where we are in the song: build, pre-drop, drop, phrase grid.
        self.arc = ArcDirector()
        self.looks = build_all(patch, self.space, self.arc)
        self._active = state.look if state.look in self.looks else "ambient"
        self._previous: str | None = None
        self._fade = 1.0
        self.looks[self._active].reset()

        #: Audio-clock timestamp of the last look change. See AUTO_DWELL_S.
        self._auto_since = 0.0
        #: Phrase boundaries crossed, and whether a section change was heard,
        #: since the last look change. See AUTO_PHRASES.
        self._phrases_held = 0
        self._section_heard = False
        #: Set for the one tick a drop lands. See _direct().
        self._dropped = False
        self._last_read = 0
        self._music: MusicState | None = None
        #: Onsets seen per region since start, counted across every analysis
        #: frame. The UI flashes on a change, so it cannot miss a hit that
        #: fell between two of its polls.
        self.onset_counts: dict[str, int] = {}
        self._energy_env = Envelope(ENERGY_ATTACK_S, ENERGY_RELEASE_S, rate_hz=tick_hz)

        #: Output smoothing. Time constants for brightness rising and falling,
        #: applied to every look after rendering. See _smooth().
        self.attack_s = max(0.0, attack_ms / 1000.0)
        self.release_s = max(0.0, release_ms / 1000.0)
        self._smoothed: dict[str, tuple[float, float]] = {}
        #: The drop hit, 0..1, decaying. See DROP_FLASH_TAU_S.
        self._flash = 0.0

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ticks = 0
        self._errors = 0
        self._last_error = ""

    # -- public ----------------------------------------------------------

    @property
    def active_look(self) -> str:
        return self._active

    @property
    def music(self) -> MusicState | None:
        return self._music

    @property
    def sustained_energy(self) -> float:
        """Peak-held energy, 0..1 — "is the music going right now".

        See IDLE_ENERGY for why look selection must not use the instantaneous
        value instead.
        """
        return self._energy_env.value

    def on_track_change(self, title: str = "", artist: str = "") -> None:
        """A new song started.

        Resets analysis for the new track. If the host has turned on palette
        auto switching, this is also when the palette changes -- a track
        boundary is the one moment the room already expects something to
        happen. The pick is shuffled from the active pool, never repeating
        the palette just played. With switching off the palette is left alone.
        """
        self.analyser.on_track_change()
        self.arc.reset()
        self.state.track_title = title
        self.state.track_artist = artist
        if self.state.palette_auto:
            pool = list(self.state.palette_pool)
            choices = [n for n in pool if n != self.state.palette] or pool
            if choices:
                self.state.set_palette(random.choice(choices))
        log.info("Track change: %s — %s (palette %s)",
                 artist or "?", title or "?", self.state.palette)

    def set_layout(self, layout: dict) -> None:
        """Adopt fixture positions from /viz. Spatial looks follow next tick."""
        self.space.set_layout(layout)

    def select(self, name: str) -> bool:
        """Switch look, starting a crossfade. False if the name is unknown."""
        if name not in self.looks or name == self._active:
            return name in self.looks
        self._previous = self._active
        self._active = name
        self._fade = 0.0
        self.looks[name].reset()
        self._auto_since = self._music.t if self._music else 0.0
        self._phrases_held = 0
        self._section_heard = False
        log.info("Look -> %s", name)
        return True

    def next_look(self) -> str:
        """Advance to the next look in registry order. Used by the cue API."""
        order = [n for n in self.looks if not getattr(self.looks[n], "manual_only", False)]
        if not order:
            return self._active
        try:
            idx = order.index(self._active)
        except ValueError:
            idx = -1
        self.select(order[(idx + 1) % len(order)])
        return self._active

    def stats(self) -> dict:
        return {
            "ticks": self._ticks,
            "errors": self._errors,
            "last_error": self._last_error,
            "look": self._active,
            # What a layered scene is built from; None for hand-written looks.
            "layers": getattr(self.looks[self._active], "layer_names", None),
            "arc": self.arc.snapshot(),
            "sustained_energy": round(self._energy_env.value, 3),
            "fading_from": self._previous if self._fade < 1.0 else None,
            "fade": round(self._fade, 2),
            "effects": self.effects.snapshot(),
            "show": self.show.snapshot(),
        }

    # -- vibe ------------------------------------------------------------

    def _band(self) -> tuple[float, float]:
        """The wildness range auto mode may pick from at the current vibe."""
        v = self.state.vibe
        return (max(0.0, (v - 0.5) * VIBE_FLOOR_SLOPE),
                VIBE_CEILING_CALM + VIBE_CEILING_SLOPE * v + 1e-9)

    def _in_band(self, name: str, band: tuple[float, float]) -> bool:
        return band[0] <= getattr(self.looks[name], "wildness", 0.5) <= band[1]

    def _arc_strength(self) -> float:
        """How much of the song arc to show: all of it from the middle of
        the vibe up, fading to none at fully calm."""
        return min(1.0, 2.0 * self.state.vibe)

    # -- the tick --------------------------------------------------------

    def _pull_audio(self) -> None:
        """Feed the analyser everything captured since the last tick.

        Reading exactly what is new avoids both re-analysing samples (which
        would corrupt the onset and tempo state) and skipping any.
        """
        if self.capture is None:
            return
        total = self.capture.ring.total_written
        available = total - self._last_read
        if available <= 0:
            return
        # Cap the catch-up so a long stall cannot cause one enormous FFT batch
        # that stalls us further.
        available = min(available, self.capture.sample_rate)
        block = self.capture.ring.latest(available, delay=self.output_delay_samples)
        self._last_read = total
        states = self.analyser.feed(block)
        if states:
            self._music = self._merge(states)
        elif self._music is not None and (any(self._music.frame.onsets.values())
                                          or self._music.events or self._music.beat):
            # Nothing new this tick: keep the levels but drop the one-shot
            # flags, or the looks would act on the same hit twice.
            self._music = self._quiet(self._music)

    def _merge(self, states: list[MusicState]) -> MusicState:
        """Fold one tick's analysis frames into one state.

        Ticks (100 Hz) and analysis hops (~94 Hz) do not line up, and after any
        stall a tick catches up several hops at once. Levels come from the
        newest frame; onsets, beats and events from all of them, so a kick in
        an earlier hop is not thrown away.
        """
        for s in states:
            for region, fired in s.frame.onsets.items():
                if fired:
                    self.onset_counts[region] = self.onset_counts.get(region, 0) + 1
        last = states[-1]
        if len(states) == 1:
            return last
        onsets = {r: any(s.frame.onsets.get(r, False) for s in states) for r in last.frame.onsets}
        strength = {
            r: max((s.frame.onset_strength.get(r, 0.0) for s in states if s.frame.onsets.get(r)),
                   default=last.frame.onset_strength.get(r, 0.0))
            for r in last.frame.onset_strength
        }
        events = list(dict.fromkeys(e for s in states for e in s.events))
        frame = replace(last.frame, onsets=onsets, onset_strength=strength)
        return replace(last, frame=frame, events=events, beat=any(s.beat for s in states))

    @staticmethod
    def _quiet(music: MusicState) -> MusicState:
        frame = replace(music.frame, onsets={r: False for r in music.frame.onsets})
        return replace(music, frame=frame, events=[], beat=False)

    def _direct(self, music: MusicState, dt: float) -> None:
        """Follow the song arc. Runs every tick, in manual mode too: a host who
        picked a look by hand still gets the build, the dark gap and the hit.
        """
        self.arc.update(music, dt, self._energy_env.value)
        self._dropped = self.arc.consume_drop()
        if self._dropped:
            self._flash = self._arc_strength()
        if self.arc.phrase_boundary:
            self._phrases_held += 1
        if SECTION_CHANGE in music.events:
            self._section_heard = True

    def _choose_auto(self, music: MusicState) -> None:
        """Pick a look from what the music is doing.

        Reacts immediately to a drop or breakdown and otherwise holds a look:
        with a tempo lock for AUTO_PHRASES phrases, changing only on a phrase
        boundary; without one for AUTO_DWELL_S. Holding is what makes this
        feel like a lighting operator rather than an energy meter driving a
        selector switch, and changing on the phrase is what makes it feel
        like one who knows the track.
        """
        now = music.t
        allowed = [n for n in auto_selectable() if n in self.looks]
        if not allowed:
            return

        def pick(name: str) -> None:
            if name in self.looks:
                self.select(name)

        sustained = self._energy_env.value
        if sustained < IDLE_ENERGY:
            if self._active != "ambient":
                pick("ambient")
            return

        events = set(music.events)
        band = self._band()
        vibe = self.state.vibe

        def usable(name: str) -> bool:
            return (name in self.looks and self._in_band(name, band)
                    and (music.tempo_locked or not self.looks[name].needs_tempo))

        if self._dropped:
            # Biggest moment in a track: the hit is already lit (see _direct);
            # land in a heavy look, a different one each drop.
            choices = [n for n in DROP_LOOKS if usable(n)]
            if not choices:
                # A vibe too calm for any of them: land in the liveliest look
                # it does allow.
                calm = [n for n in allowed if n not in ("ambient", "hush") and usable(n)]
                choices = sorted(calm, key=lambda n: -self.looks[n].wildness)[:1]
            if choices:
                pick(choices[(self.arc.drops - 1) % len(choices)])
            return
        if BREAKDOWN in events:
            # The music drops away, so does the room: PARs out, UV only.
            pick("hush")
            return
        if self.arc.state == BREAKDOWN_STATE and self._active == "hush":
            return
        if self.arc.state in (BUILDING, PREDROP) and self._active not in ("ambient", "hush"):
            # Hold the look while the build tightens it; the drop changes it.
            # A build straight out of a breakdown first needs a look to hold.
            return

        # Leaving the idle look is always allowed. The dwell exists to stop
        # the rig twitching between *active* looks; applying it to `ambient`
        # means starting the software mid-song leaves the room idle for the
        # whole dwell while people are already dancing. Observed directly: 8
        # bars of music at sustained energy 0.40 and the engine stayed on
        # ambient, because a track that is already playing when we start
        # produces no drop event to break the dwell.
        # Hush is held while the breakdown lasts -- until the director hears
        # the kick come back. Energy alone cannot say: the analyser's gain
        # control makes a quiet pad read as busy within seconds, and on the
        # test track hush lasted one tick.
        leaving_idle = sustained > MID_ENERGY and (
            self._active == "ambient"
            or (self._active == "hush" and self.arc.state != BREAKDOWN_STATE))
        # The vibe stretches the hold at calm and shortens it at wild; and a
        # look the vibe has since ruled out goes at the next chance rather
        # than seeing out its hold, so the slider is felt within a phrase.
        stretch = 2.0 ** (1.0 - 2.0 * vibe)
        phrases = max(1, round(AUTO_PHRASES * stretch))
        dwell = AUTO_DWELL_S * stretch
        ruled_out = (self._active not in ("ambient", "hush")
                     and not self._in_band(self._active, band))
        if music.tempo_locked:
            due = self.arc.phrase_boundary and (
                ruled_out
                or self._phrases_held >= phrases
                or (self._section_heard and self._phrases_held >= phrases // 2))
        else:
            forced = SECTION_CHANGE in events and now - self._auto_since > dwell / 2
            due = ruled_out or forced or now - self._auto_since >= dwell
        if not (due or leaving_idle):
            return

        # Otherwise choose by energy, rotating among the candidates at that
        # energy so a long track does not sit on one look forever.
        # Each list alternates still and moving looks, so consecutive picks
        # contrast. The tempo-locked moves (rotor, sweep, chase) only appear
        # with a lock, which they need to look intentional.
        # The vibe scales how loud the music seems here: x2 at wild, x0.5 calm.
        energy = sustained * 2.0 ** (2.0 * vibe - 1.0)
        if energy > BUSY_ENERGY:
            candidates = (["pulse", "rotor", "unison", "pingpong", "knockout", "sweep",
                           "beams", "chase"]
                          if music.tempo_locked
                          else ["pulse", "ripple", "unison", "callresponse", "knockout",
                                "mirror"])
        elif energy > MID_ENERGY:
            candidates = (["wash", "ripple", "drift", "stepper", "unison", "callresponse"]
                          if music.tempo_locked
                          else ["wash", "ripple", "drift", "callresponse", "unison",
                                "sparkle"])
        else:
            candidates = ["wash", "drift", "uv", "ambient"]
        candidates = [c for c in candidates if c in self.looks and self._in_band(c, band)]
        if len(candidates) < VIBE_MIN_CHOICES:
            candidates += [n for n in allowed if n not in candidates
                           and n not in ("ambient", "hush") and usable(n)]
        if not candidates:
            return
        try:
            idx = candidates.index(self._active)
        except ValueError:
            idx = -1
        pick(candidates[(idx + 1) % len(candidates)])

    def _render_look(self, name: str, music: MusicState, pal, dt: float) -> dict[str, Emission]:
        """Render one look, swallowing any failure.

        A look is the most likely thing in this project to be edited at 1am, so
        a bug in one must not take the rig down. An empty result leaves the
        fixtures where they were.
        """
        try:
            return self.looks[name].render(music, pal, dt) or {}
        except Exception as e:
            self._errors += 1
            self._last_error = f"{name}: {e}"
            if self._errors % 50 == 1:
                log.exception("look %s failed", name)
            return {}

    def _arc_shape(self, emissions: dict[str, Emission]) -> dict[str, Emission]:
        """The build, applied to whatever look is up: PARs lift toward full
        and wash toward white as it tightens. Before smoothing, so it eases in.
        Scaled by the vibe; see _arc_strength()."""
        mods = self.arc.mods
        k = self._arc_strength()
        floor, desat = mods.floor * k, mods.desat * k
        if floor <= 0.0 and desat <= 0.0:
            return emissions
        out = dict(emissions)
        for f in self.patch.group("pars"):
            em = emissions.get(f.fid)
            if em is None:
                continue
            out[f.fid] = Emission(mix(em.rgb, WHITE, desat),
                                  floor + (1.0 - floor) * em.intensity,
                                  em.strobe, em.uv, em.emitter_bias)
        return out

    def _arc_cut(self, emissions: dict[str, Emission]) -> dict[str, Emission]:
        """The pre-drop gap and the riser. After smoothing, so the PARs cut
        to dark at once instead of fading over the look's release -- the gap
        is often a single beat. The accent carries the riser in white. Scaled
        by the vibe, like the build."""
        mods = self.arc.mods
        k = self._arc_strength()
        blackout, riser = mods.blackout * k, mods.riser * k
        if blackout <= 0.0 and riser <= 0.0:
            return emissions
        out = dict(emissions)
        keep = 1.0 - blackout
        for f in self.patch.group("pars"):
            em = emissions.get(f.fid, BLACK)
            out[f.fid] = Emission(em.rgb, em.intensity * keep, em.strobe, em.uv,
                                  em.emitter_bias)
        for f in self.patch.group("accent"):
            em = emissions.get(f.fid, BLACK)
            out[f.fid] = Emission(mix(em.rgb, WHITE, riser),
                                  max(em.intensity, 0.85 * riser), em.strobe, em.uv,
                                  max(em.emitter_bias, riser))
        return out

    def _drop_hit(self, emissions: dict[str, Emission], pal, dt: float) -> dict[str, Emission]:
        """Lift every fixture towards full while a drop hit is decaying.

        Keeps each fixture's own colour, so the hit lands in the look's
        palette rather than as a white flash; a fixture the look left
        colourless takes the palette's first colour.
        """
        if self._flash < 0.01:
            self._flash = 0.0
            return emissions
        flash = self._flash
        self._flash *= math.exp(-dt / DROP_FLASH_TAU_S)
        out = dict(emissions)
        for f in self.patch:
            em = emissions.get(f.fid, BLACK)
            rgb = em.rgb if max(em.rgb) > 0.0 else pal.at(0)
            out[f.fid] = Emission(rgb, max(em.intensity, flash), em.strobe, em.uv,
                                  em.emitter_bias)
        return out

    def _smooth(self, emissions: dict[str, Emission], dt: float) -> dict[str, Emission]:
        """Fast attack, slow release on brightness, per fixture.

        Looks react to the music frame by frame, and on cheap LED fixtures that
        reads as flicker: a light that snaps between dark and a brief flash
        looks broken rather than musical. Letting brightness rise almost
        instantly but fall over a few hundred milliseconds keeps every hit
        punchy while turning the drop-off into a fade. It also papers over some
        of the timing jitter wireless DMX adds.

        Only intensity and UV are smoothed. Colour still changes on the frame
        the look asks for, so a hit can land in a new colour. Manual overrides
        are applied after this and are never smoothed: a blackout or a fixture
        the host has grabbed must respond immediately.
        """
        if self.attack_s <= 0.0 and self.release_s <= 0.0:
            return emissions
        # The active look may ask for its own release; see Look.release_s.
        release = getattr(self.looks.get(self._active), "release_s", None)
        if release is None:
            release = self.release_s
        # The softness control stretches or shrinks both, attack included:
        # a smooth room should not have hits that snap on.
        scale = self.state.softness_scale()
        attack, release = self.attack_s * scale, release * scale
        up = 1.0 - math.exp(-dt / attack) if attack > 0.0 else 1.0
        down = 1.0 - math.exp(-dt / release) if release > 0.0 else 1.0

        out: dict[str, Emission] = {}
        for f in self.patch:
            em = emissions.get(f.fid, BLACK)
            target = (em.intensity, em.uv)
            prev = self._smoothed.get(f.fid)
            if prev is None:
                level = target
            else:
                level = tuple(
                    p + (t - p) * (up if t > p else down) for p, t in zip(prev, target)
                )
            self._smoothed[f.fid] = level
            out[f.fid] = Emission(em.rgb, level[0], em.strobe, level[1], em.emitter_bias)
        return out

    def tick(self, dt: float) -> None:
        self._ticks += 1
        self._pull_audio()

        music = self._music
        if music is None:
            # No audio yet. Synthesise a silent state so looks still animate --
            # ambient should breathe before the first track starts.
            music = MusicState(frame=FeatureFrame(t=time.monotonic(), rms=0.0,
                                                  loudness_db=-120.0, energy=0.0))

        # Track sustained energy before anything reads it.
        self._energy_env.update(0.0 if music.silent else music.energy)

        state = self.state
        pal = palettes.get(state.palette)

        self._direct(music, dt)

        # A designed show picks the look and palette itself. Otherwise honour
        # the host's choice in manual mode, or let the music pick.
        plan = self.show.plan(state.vibe)
        if plan is not None:
            look, pal, level = plan
            if look != self._active:
                self.select(look)
            # The card has the song's builds and drops; the arc's guesses at
            # them would land twice.
            self._flash = 0.0
        elif state.mode == "manual":
            if state.look != self._active:
                self.select(state.look)
        else:
            self._choose_auto(music)

        # Enforce the strobe time limit regardless of mode.
        if not state.strobe_allowed(self._active):
            log.info("Strobe time limit reached (%.1fs) — falling back to pulse",
                     state.max_strobe_seconds)
            state.set_look("pulse")
            self.select("pulse")

        # Hand every look the softness scale before it renders.
        scale = state.softness_scale()
        for look in self.looks.values():
            look.time_scale = scale

        emissions = self._render_look(self._active, music, pal, dt)

        # Crossfade out of the previous look.
        if self._fade < 1.0 and self._previous:
            self._fade = min(1.0, self._fade + dt / (FADE_S * scale))
            old = self._render_look(self._previous, music, pal, dt)
            blended: dict[str, Emission] = {}
            for f in self.patch:
                a = old.get(f.fid, BLACK)
                b = emissions.get(f.fid, BLACK)
                blended[f.fid] = a.blended(b, self._fade)
            emissions = blended
            if self._fade >= 1.0:
                self._previous = None

        if plan is not None:
            if level < 1.0:
                emissions = {fid: em.with_intensity(level) for fid, em in emissions.items()}
            emissions = self._smooth(emissions, dt)
            # Cues after smoothing, like the arc's cut: a one-beat blackout
            # must be dark at once, not fading over the look's release.
            emissions = self.show.shade(emissions, state.vibe, dt, state.max_strobe_seconds)
        else:
            emissions = self._arc_shape(emissions)
            emissions = self._smooth(emissions, dt)
            # The drop hit goes after smoothing: through the attack it never
            # reached full (byte 146 of 255 on the live rig), because it was
            # already decaying while the smoother rose. Its own decay is the fade.
            emissions = self._drop_hit(emissions, pal, dt)
            emissions = self._arc_cut(emissions)
        # The host's effects go over the show, arc and all; see effects.py.
        emissions = self.effects.apply(emissions, music, dt)

        # Manual overrides sit above the looks: a fixture the host has taken is
        # not touched by whatever the music is doing.
        for fid, em in state.active_manual().items():
            emissions[fid] = em

        master = state.master
        floor = state.dimmer_floor
        for f in self.patch:
            em = emissions.get(f.fid, BLACK)
            if master < 1.0:
                em = em.with_intensity(master)
            try:
                f.render_into(self.universe, em, floor)
            except Exception as e:
                self._errors += 1
                self._last_error = f"{f.fid}: {e}"

        # The test bench goes last and skips the profile's conversions, so the
        # bytes it asks for are the bytes sent. Blackout and freeze still win:
        # they act in the universe, after this.
        for fid, values in state.active_raw().items():
            f = self.patch.by_id.get(fid)
            if f is not None and f.enabled:
                self.universe.set_block(f.address, f.profile.raw_frame(values))

    # -- lifecycle -------------------------------------------------------

    def run(self) -> None:
        period = 1.0 / self.tick_hz
        last = time.monotonic()
        log.info("Engine running at %.0f Hz, look=%s", self.tick_hz, self._active)
        while not self._stop.is_set():
            now = time.monotonic()
            dt = now - last
            last = now
            try:
                self.tick(min(dt, 0.25))
            except Exception as e:
                # Last line of defence. The writer thread keeps sending the last
                # good frame, so the room stays lit while we log and continue.
                self._errors += 1
                self._last_error = str(e)
                if self._errors % 50 == 1:
                    log.exception("engine tick failed")
            slack = period - (time.monotonic() - now)
            if slack > 0:
                time.sleep(slack)
        log.info("Engine stopped after %d ticks (%d errors)", self._ticks, self._errors)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="Engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._thread = None
