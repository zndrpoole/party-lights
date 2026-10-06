"""The song arc: where in the song we are, and what the lights should do about it.

Beat-level reaction makes lights move; this makes them understand a dance
track. An EDM track is built from 16-beat phrases and tells you what is coming:
a build tightens for a few phrases, the low end falls away for a beat or a bar,
and the drop lands on the first beat of a phrase. Lighting that follows that
arc -- tightening, holding its breath in the dark, then hitting on the one --
reads as designed rather than reactive.

States:

    idle       nothing playing
    groove     the normal state; looks change on phrase boundaries
    build      tightening: patterns speed up each phrase, colour washes out,
               the accent's white rises
    predrop    the gap: the PARs go dark, the accent holds the riser
    drop       the hit, held for one phrase
    breakdown  the music falls away; the engine sends the room to UV

The director finds builds itself, from raw (un-normalised) band levels. The
structure tracker's BUILD event fires on any rise in energy, which measured on
a test track meant the intro-to-groove step, not the build; and it missed a
build after a breakdown entirely. What an EDM build does reliably is take the
low end away while the music carries on -- the kick keeps going, or a snare
roll and riser climb -- so that is the test: low end below BUILD_LOW of its
groove level, with kicks still coming or the highs well above their groove
level, held for a bar.

The drop is the moment that has to be on time, so it has a fast path. The
structure tracker's DROP event compares two-second windows and lands about two
seconds after the drop -- fine for changing a look, far too late for the hit.
So once a build has lost its low end (predrop), the first strong kick *is* the
drop, fired on that frame. A build that runs straight into its drop without a
gap is caught too: a kick with the low end back above its groove level.
Measured on the test track, the build's kicks peaked at 0.55x the groove's low
end and the drop's first half-second at 1.3x. The late DROP event remains as a
fallback, and is ignored when it merely confirms a drop we already fired.

The director also keeps the phrase grid. The tempo tracker counts 16-beat
phrases from wherever it happened to lock, which is usually not where the
track's phrases start. A fast drop is the best evidence there is of where bar
one of a phrase is, so it re-anchors the grid; every phrase-quantised decision
after that lines up with the track.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..audio.analyser import MusicState
from ..audio.structure import BREAKDOWN, DROP

IDLE, GROOVE, BUILDING, PREDROP, DROPPING, BREAKDOWN_STATE = (
    "idle", "groove", "build", "predrop", "drop", "breakdown")

PHRASE_BEATS = 16

#: Sustained energy below which nothing is playing. Matches the engine's.
IDLE_ENERGY = 0.08

#: A build: low end below BUILD_LOW of its groove level while kicks continue
#: (one within BUILD_KICK_BEATS) or the highs sit above BUILD_HIGH of theirs,
#: for BUILD_CONFIRM_BEATS (seconds without a lock).
BUILD_LOW = 0.6
BUILD_HIGH = 1.6
BUILD_KICK_BEATS = 2.0
BUILD_CONFIRM_BEATS = 4
BUILD_CONFIRM_S = 2.0
#: Low end at least this much of its groove level, on a kick, is the drop
#: arriving straight out of a build.
DROP_LOW = 1.1
DROP_LAG_BEATS = 0.5
#: A build that stops looking like one for this many beats is over.
BUILD_LAPSE_BEATS = 8
#: Time constants for the level followers: fast for "now", slow for the
#: groove reference, which only learns in the groove and the drop.
FAST_S = 0.5
SHORT_S = 0.15
REF_S = 8.0
#: Kicks within this many seconds, with the low end back above RETURN_LOW of
#: its groove level, end a breakdown.
RETURN_KICKS = 3
RETURN_WINDOW_S = 2.5
RETURN_LOW = 0.3
#: A build must have run this long before a gap counts as the pre-drop, and
#: gives up if no drop arrives within BUILD_MAX_S.
MIN_BUILD_S = 3.0
BUILD_MAX_S = 45.0
#: Kicks needed during the build before their absence means anything. Stops a
#: kickless riser build from going dark as soon as it starts.
MIN_BUILD_KICKS = 4
#: Gap without a kick, in beats (seconds without a lock), that opens the
#: pre-drop -- together with the low end falling away.
GAP_BEATS = 1.25
GAP_S = 0.7
#: Low end below this fraction of its level during the build counts as gone.
LOW_FRACTION = 0.55
#: The pre-drop gives up after this long and the build resumes.
PREDROP_MAX_BEATS = 8
PREDROP_MAX_S = 4.0
#: A kick at least this strong ends the pre-drop as the drop.
DROP_KICK = 0.35
#: The drop state lasts one phrase (or this long without a lock).
DROP_HOLD_S = 8.0
#: A late DROP event this soon after a fast drop is confirmation, not a drop.
CONFIRM_WINDOW_S = 6.0

#: Build intensity ramps over this many seconds without a lock; with one, over
#: BUILD_RAMP_PHRASES phrases.
BUILD_RAMP_S = 16.0
BUILD_RAMP_PHRASES = 2
#: How far the build lifts the floor, washes colour toward white, and raises
#: the accent's riser glow, at full ramp. The floor has to be high: a build
#: takes the bass away, so any look that follows the bass dims -- measured
#: live, a build came out at a third of the groove's brightness with 0.25.
BUILD_FLOOR = 0.75
BUILD_DESAT = 0.45
#: After the hit, the drop phrase starts with the floor lifted this far and
#: lets it go over the phrase. Without it, measured live, the drop sat barely
#: brighter than the groove once the hit had faded.
DROP_FLOOR = 0.5

#: Pattern speed doubles each phrase of a build, up to this.
MAX_RATE = 4


@dataclass
class ArcMods:
    """What the arc asks of the looks this tick. Neutral when all zero/one."""

    #: Pattern speed multiplier, a power of two, changing only on phrase lines.
    rate: int = 1
    #: Raise every PAR's brightness toward 1 by this much.
    floor: float = 0.0
    #: Mix PAR colour toward white by this much.
    desat: float = 0.0
    #: Cut the PARs: 1 is fully dark.
    blackout: float = 0.0
    #: The accent's white glow, 0..1.
    riser: float = 0.0


class ArcDirector:
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        """New track: the old phrase grid and arc are no longer evidence."""
        self.state = IDLE
        self.mods = ArcMods()
        self.anchor = 0
        #: Whether the phrase grid has been confirmed by a drop.
        self.anchored = False
        self.phrase = 0
        #: True on the tick a new phrase starts (with a lock).
        self.phrase_boundary = False
        self._t = 0.0
        self._entered = 0.0
        #: When the current build began; kept through a pre-drop that times
        #: out, so the speed-up carries on rather than starting again.
        self._build_t = 0.0
        self._build_phrase = 0
        self._build_beats = 0.0
        self._last_kick = -1e9
        self._kicks: list[float] = []
        self._build_kicks = 0
        self._low_ref = 0.0
        #: Raw level followers. See FAST_S.
        self._low_fast = 0.0
        self._low_short = 0.0
        self._groove_low = 0.0
        self._high_fast = 0.0
        self._groove_high = 0.0
        self._candidate_since: float | None = None
        self._lapsed_since: float | None = None
        self._last_drop_t = -1e9
        self._drop_pending = False
        self._drops = 0

    # -- reading ------------------------------------------------------------

    def consume_drop(self) -> bool:
        """True once per drop; the engine fires the hit and changes look."""
        if self._drop_pending:
            self._drop_pending = False
            return True
        return False

    @property
    def drops(self) -> int:
        return self._drops

    def beats(self, music: MusicState) -> float:
        """Beats since the phrase grid's anchor -- the clock looks should use
        so their patterns start on the track's bar one."""
        return music.beat_index - self.anchor + music.beat_phase

    def phrase_position(self, music: MusicState) -> float:
        """0..1 through the current phrase."""
        return (self.beats(music) % PHRASE_BEATS) / PHRASE_BEATS

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "phrase": self.phrase + 1,
            "anchored": self.anchored,
            "rate": self.mods.rate,
            "riser": round(self.mods.riser, 2),
            "drops": self._drops,
        }

    # -- updating -----------------------------------------------------------

    def update(self, music: MusicState, dt: float, sustained: float) -> None:
        self._t += dt
        t = self._t
        locked = music.tempo_locked
        events = set(music.events)
        low = max(music.smooth("sub"), music.smooth("bass"))
        beat_s = 60.0 / music.bpm if locked and music.bpm > 0 else 0.5

        # The tempo tracker restarts its count on a track change or lost lock.
        if music.beat_index < self.anchor:
            self.anchor, self.anchored = 0, False
        phrase = int((music.beat_index - self.anchor) // PHRASE_BEATS) if locked else 0
        self.phrase_boundary = locked and phrase != self.phrase
        self.phrase = phrase

        kick = music.onset("kick")
        if kick:
            self._last_kick = t
            self._kicks = [k for k in self._kicks if t - k < RETURN_WINDOW_S] + [t]
            if self.state == BUILDING:
                self._build_kicks += 1
        since_kick = t - self._last_kick
        gap = GAP_BEATS * beat_s if locked else GAP_S
        state = self.state
        elapsed = t - self._entered

        candidate = self._follow(music, dt, state, since_kick, beat_s)

        # Late DROP event: fallback, or confirmation of a drop already fired.
        if DROP in events and t - self._last_drop_t > CONFIRM_WINDOW_S:
            self._drop(music, t, anchor=False)
        elif sustained < IDLE_ENERGY and state != PREDROP:
            self._enter(IDLE, t)
        elif state in (IDLE, GROOVE, BREAKDOWN_STATE) and candidate:
            self._start_build(music, t, low)
        elif BREAKDOWN in events and state not in (BUILDING, PREDROP):
            self._enter(BREAKDOWN_STATE, t)
        elif state == IDLE:
            self._enter(GROOVE, t)
        elif state == BREAKDOWN_STATE:
            # Over when the kick is back, however loud the pad has been made
            # by the analyser's gain control.
            # The low end must be back too: with the low band near silent, the
            # onset detector's adaptive threshold fires on pad leakage (about
            # 0.7 false kicks a second on the test track's breakdown).
            back = [k for k in self._kicks if k > self._entered]
            low_back = self._groove_low <= 0.0 or self._low_fast > RETURN_LOW * self._groove_low
            if len(back) >= RETURN_KICKS and low_back:
                self._enter(GROOVE, t)
        elif state == BUILDING:
            # Track the build's own low end slowly, so the gap stands out.
            self._low_ref += (low - self._low_ref) * (1.0 - math.exp(-dt / 2.0))
            gone = low < LOW_FRACTION * self._low_ref
            last_bar = not (locked and self.anchored) or self.phrase_position(music) >= 0.75
            # The low end takes ~0.2 s to read as back, so allow it to arrive
            # just after the kick; _drop anchors to that kick's beat.
            back = (since_kick < DROP_LAG_BEATS * beat_s and self._groove_low > 0
                    and self._low_short > DROP_LOW * self._groove_low)
            if back and t - self._build_t >= MIN_BUILD_S:
                self._drop(music, t, anchor=locked)
            elif (t - self._build_t >= MIN_BUILD_S and self._build_kicks >= MIN_BUILD_KICKS
                    and since_kick > gap and gone and last_bar):
                self._enter(PREDROP, t)
            elif t - self._build_t > BUILD_MAX_S or self._lapsed(t, candidate, beat_s, locked):
                self._enter(GROOVE, t)
        elif state == PREDROP:
            limit = PREDROP_MAX_BEATS * beat_s if locked else PREDROP_MAX_S
            if kick and music.hit("kick") >= DROP_KICK:
                self._drop(music, t, anchor=locked)
            elif elapsed > limit:
                # No drop came. Back to building; it needs kicks again before
                # another gap can count.
                self._enter(BUILDING, t)
                self._build_kicks = 0
        elif state == DROPPING:
            hold = PHRASE_BEATS * beat_s if locked else DROP_HOLD_S
            if elapsed > hold:
                self._enter(GROOVE, t)

        self._update_mods(music, t)

    def _follow(self, music: MusicState, dt: float, state: str, since_kick: float,
                beat_s: float) -> bool:
        """Update the raw level followers; True while the music looks like a
        build, held long enough to count."""
        raw = music.frame.bands_raw
        low = raw.get("sub", 0.0) + raw.get("bass", 0.0)
        high = raw.get("high", 0.0) + raw.get("air", 0.0)

        def follow(value, target, tau):
            return value + (target - value) * (1.0 - math.exp(-dt / tau))

        self._low_fast = follow(self._low_fast, low, FAST_S)
        self._low_short = follow(self._low_short, low, SHORT_S)
        self._high_fast = follow(self._high_fast, high, FAST_S)
        # The groove reference only learns while the track is in its normal
        # state, so a build or breakdown cannot drag down what it is judged by.
        if state in (GROOVE, DROPPING):
            self._groove_low = follow(self._groove_low, low, REF_S)
            self._groove_high = follow(self._groove_high, high, REF_S)

        if self._groove_low <= 0.0:
            return False
        low_down = self._low_fast < BUILD_LOW * self._groove_low
        carrying = (since_kick < BUILD_KICK_BEATS * beat_s
                    or self._high_fast > BUILD_HIGH * self._groove_high)
        if not (low_down and carrying):
            self._candidate_since = None
            return False
        if self._candidate_since is None:
            self._candidate_since = self._t
        hold = BUILD_CONFIRM_BEATS * beat_s if music.tempo_locked else BUILD_CONFIRM_S
        return self._t - self._candidate_since >= hold

    def _lapsed(self, t: float, candidate: bool, beat_s: float, locked: bool) -> bool:
        """A build that has stopped looking like one for a while is over."""
        if candidate or self._candidate_since is not None:
            self._lapsed_since = None
            return False
        if self._lapsed_since is None:
            self._lapsed_since = t
        return t - self._lapsed_since > BUILD_LAPSE_BEATS * (beat_s if locked else 0.5)

    def _start_build(self, music: MusicState, t: float, low: float) -> None:
        self._enter(BUILDING, t)
        self._build_kicks, self._low_ref = 0, low
        self._build_t, self._build_phrase = t, self.phrase
        self._build_beats = self.beats(music) if music.tempo_locked else 0.0
        self._lapsed_since = None

    def _enter(self, state: str, t: float) -> None:
        if state != self.state:
            self.state = state
            self._entered = t

    def _drop(self, music: MusicState, t: float, *, anchor: bool) -> None:
        if anchor:
            # The kick may be detected a touch after the beat it belongs to, or
            # just before the next one; either way this beat is bar one.
            self.anchor = music.beat_index + (1 if music.beat_phase > 0.5 else 0)
            self.anchored = True
            self.phrase = 0
            self.phrase_boundary = True
        self._enter(DROPPING, t)
        self._entered = t
        self._last_drop_t = t
        self._drop_pending = True
        self._drops += 1

    def _update_mods(self, music: MusicState, t: float) -> None:
        m = ArcMods()
        if self.state in (BUILDING, PREDROP):
            if music.tempo_locked:
                # Speed doubles on each phrase line the build crosses; the
                # ramp runs from where the build actually began.
                phrases = max(0, self.phrase - self._build_phrase)
                m.rate = min(MAX_RATE, 2 ** phrases)
                done = self.beats(music) - self._build_beats
                ramp = min(1.0, max(0.0, done) / (BUILD_RAMP_PHRASES * PHRASE_BEATS))
            else:
                ramp = min(1.0, (t - self._build_t) / BUILD_RAMP_S)
            if self.state == PREDROP:
                ramp, m.blackout = 1.0, 1.0
            m.floor = BUILD_FLOOR * ramp
            m.desat = BUILD_DESAT * ramp
            m.riser = ramp
        elif self.state == DROPPING:
            hold = (PHRASE_BEATS * 60.0 / music.bpm if music.tempo_locked and music.bpm > 0
                    else DROP_HOLD_S)
            left = max(0.0, 1.0 - (t - self._entered) / hold)
            m.floor = DROP_FLOOR * left * left
        self.mods = m
