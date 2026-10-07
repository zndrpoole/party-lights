"""Effects: moments the host drops into the middle of a song by hand.

Not looks. A look is what the rig is doing; an effect is something the host
does *to* it -- a lightning strike over the dance floor, the yard sinking to
candlelight for a toast, a heartbeat under a quiet bridge. So effects sit on
top of whatever look is running, and auto mode carries on underneath them:
switching one off lands back in the show exactly where the music has got to,
rather than in a look the host has to go and find again.

Nothing here is ever chosen by the engine. Every effect starts because a cue
asked for it (`effect/<name>`), which is the whole point: these are the host's
moments, and a rig that fired them on its own would cheapen them.

Two kinds:

* A **hit** fires once and finishes on its own: lightning.
* A **hold** fades in, stays until switched off, and fades back out: candle and
  heartbeat. One hold at a time; picking another crossfades between them.

Effects render after the engine's smoothing and the song arc, so a lightning
flash is crisp rather than eased in, and a host's candlelight is not cut dark
by a pre-drop gap they did not ask for. Manual fixture overrides, the master
dimmer, blackout and freeze all still win over them.
"""

from __future__ import annotations

import math
import random
import threading

from ..fixtures.color import BLACK, Emission, clamp, hsv, mix
from ..fixtures.patch import Patch
from .space import Space

#: Seconds for a hold to fade in, and back out when switched off.
HOLD_FADE_IN_S = 1.5
HOLD_FADE_OUT_S = 2.0


def _ease(x: float) -> float:
    """Smoothstep, so a fade starts and lands gently."""
    x = clamp(x)
    return x * x * (3.0 - 2.0 * x)


class Effect:
    name = "base"
    description = ""
    #: "hit" fires once and ends itself; "hold" runs until switched off.
    kind = "hold"

    def __init__(self, patch: Patch, space: Space, rng: random.Random):
        self.patch = patch
        self.space = space
        self.rng = rng

    def reset(self) -> None:
        """Called each time the effect starts."""

    def render(self, base: dict[str, Emission], music, dt: float) -> dict[str, Emission]:
        """What every fixture should show. `base` is the show underneath, for
        effects that work on it rather than replace it."""
        raise NotImplementedError


# -- lightning ---------------------------------------------------------------

#: Cold, slightly blue white. The accent renders it on its white emitter.
LIGHTNING_RGB = (0.80, 0.88, 1.0)
#: How fast each flash dies. Real lightning is a few strokes of tens of
#: milliseconds each; much longer and it reads as a camera flash.
FLASH_TAU_S = 0.07
#: The show underneath ducks to this while the strike is on, so the flashes
#: have darkness between them -- that contrast is what sells it.
SHADOW = 0.12
#: After the last flash, the show stays ducked this long, then eases back.
SHADOW_HOLD_S = 0.25
SHADOW_RECOVER_S = 1.6
#: Safety rail. A strike is at most three flashes inside 0.7 s, and another
#: cannot start until MIN_GAP_S after the last began, so no one-second window
#: ever holds more than three flashes however hard the button is mashed --
#: the photosensitivity guideline the strobe limit in state.py also serves.
MAX_FLASHES = 3
MIN_GAP_S = 2.0


class LightningEffect(Effect):
    name = "lightning"
    description = "A lightning strike: two or three cold white flashes, then the show eases back"
    kind = "hit"

    def reset(self) -> None:
        self._t = 0.0
        rng = self.rng
        # A leader on one side of the yard, the main stroke everywhere, and
        # sometimes a weaker restrike. Randomised so no two strikes match.
        side = rng.randrange(2)
        self.flashes: list[tuple[float, float, int | None]] = [
            (0.0, 0.55, side),
            (rng.uniform(0.12, 0.22), 1.0, None),
        ]
        if rng.random() < 0.6:
            self.flashes.append((rng.uniform(0.45, 0.65), 0.7, None))
        last = self.flashes[-1][0]
        self.duration = last + SHADOW_HOLD_S + SHADOW_RECOVER_S

    @property
    def done(self) -> bool:
        return self._t >= self.duration

    def _side(self, fid: str) -> int | None:
        try:
            return self.space.spot(fid).side
        except KeyError:
            return None

    def render(self, base, music, dt):
        t = self._t
        self._t += dt
        last = self.flashes[-1][0]
        if t < last + SHADOW_HOLD_S:
            shadow = SHADOW
        else:
            shadow = SHADOW + (1.0 - SHADOW) * _ease((t - last - SHADOW_HOLD_S)
                                                     / SHADOW_RECOVER_S)
        out = {}
        for f in self.patch:
            em = base.get(f.fid, BLACK)
            side = self._side(f.fid)
            flash = 0.0
            for start, level, only in self.flashes:
                if t >= start and (only is None or only == side):
                    flash = max(flash, level * math.exp(-(t - start) / FLASH_TAU_S))
            ducked = em.intensity * shadow
            if flash < 0.01:
                out[f.fid] = Emission(em.rgb, ducked, em.strobe, em.uv * shadow,
                                      em.emitter_bias)
            else:
                out[f.fid] = Emission(mix(em.rgb, LIGHTNING_RGB, clamp(flash * 3.0)),
                                      max(ducked, flash), 0.0, em.uv * shadow, 1.0)
        return out


# -- candle ------------------------------------------------------------------

#: Warm flame colours: brighter is more yellow, dimmer more red, as a flame is.
CANDLE_HOT = hsv(0.095, 0.95, 1.0)
CANDLE_LOW = hsv(0.045, 1.0, 1.0)
#: Average brightness, and how far the flicker moves it either way. Kept
#: gentle on purpose: a deep or sudden dip reads as a failing fixture.
CANDLE_LEVEL = 0.42
CANDLE_SLOW = 0.10
CANDLE_FAST = 0.07
#: How often the fast flicker picks a new target, and how quickly it follows.
CANDLE_JUMP_HZ = 9.0
CANDLE_FOLLOW_S = 0.07


class CandleEffect(Effect):
    name = "candle"
    description = "Warm candlelight, every fixture flickering on its own — for toasts and lulls"

    def reset(self) -> None:
        rng = self.rng
        self._t = 0.0
        # Each fixture gets its own slow wander (two incommensurate sines) and
        # its own fast flicker, so the yard reads as many small flames rather
        # than one light pulsing.
        self._phase = {f.fid: (rng.uniform(0, 2 * math.pi), rng.uniform(0, 2 * math.pi),
                               rng.uniform(0.17, 0.29), rng.uniform(0.41, 0.67))
                       for f in self.patch}
        self._fast = {f.fid: 0.0 for f in self.patch}
        self._target = {f.fid: 0.0 for f in self.patch}

    def render(self, base, music, dt):
        self._t += dt
        rng = self.rng
        follow = 1.0 - math.exp(-dt / CANDLE_FOLLOW_S)
        jump = min(1.0, CANDLE_JUMP_HZ * dt)
        out = {}
        for f in self.patch:
            fid = f.fid
            p1, p2, f1, f2 = self._phase[fid]
            slow = 0.6 * math.sin(2 * math.pi * f1 * self._t + p1) \
                + 0.4 * math.sin(2 * math.pi * f2 * self._t + p2)
            if rng.random() < jump:
                self._target[fid] = rng.uniform(-1.0, 1.0)
            self._fast[fid] += (self._target[fid] - self._fast[fid]) * follow
            level = clamp(CANDLE_LEVEL + CANDLE_SLOW * slow + CANDLE_FAST * self._fast[fid])
            heat = clamp((level - (CANDLE_LEVEL - 0.2)) / 0.4)
            # The accent's amber emitter is the best candle on the rig.
            bias = 1.0 if "accent" in f.groups else 0.0
            out[fid] = Emission(mix(CANDLE_LOW, CANDLE_HOT, heat), level, 0.0, 0.0, bias)
        return out


# -- heartbeat ---------------------------------------------------------------

HEART_RGB = hsv(0.985, 1.0, 1.0)
#: Resting rate when the music gives no tempo to lock to.
HEART_IDLE_BPM = 68.0
#: The song's tempo is halved until it is at or below this, so a 128 BPM track
#: beats at 64 and a 174 at 43.5: a heart rather than a strobe. Two thumps a
#: beat, so this also holds the heartbeat under three flashes a second --
#: which matters most for saturated red, the riskiest colour to flash.
HEART_MAX_BPM = 80.0
#: Where the second sound ("dub") falls, in seconds after the first, and how
#: strong it is. About right for a heart at rest.
DUB_AFTER_S = 0.26
DUB_LEVEL = 0.65
#: Each thump's rise and fall. The rise keeps it a pulse rather than a flash.
THUMP_RISE_S = 0.03
THUMP_TAU_S = 0.12
#: Between beats the yard is almost dark; the edges thump less than the middle.
HEART_FLOOR = 0.04
HEART_EDGE = 0.55


class HeartbeatEffect(Effect):
    name = "heartbeat"
    description = "A deep red lub-dub thumping from the middle of the yard, in time with the song"

    def reset(self) -> None:
        self._clock = 0.0

    def _cycle(self, music, dt: float) -> tuple[float, float]:
        """Seconds since the last "lub", and the length of one heartbeat.

        Locked to the song when there is a tempo: every beat, or every second
        or fourth beat for anything fast. Otherwise a resting heart.
        """
        if music.tempo_locked and music.bpm > 0:
            beat_s = 60.0 / music.bpm
            every = 1
            while music.bpm / every > HEART_MAX_BPM:
                every *= 2
            pos = (music.beat_index % every) + music.beat_phase
            return pos * beat_s, every * beat_s
        period = 60.0 / HEART_IDLE_BPM
        self._clock = (self._clock + dt) % period
        return self._clock, period

    @staticmethod
    def _thump(age: float) -> float:
        if age < 0.0:
            return 0.0
        if age < THUMP_RISE_S:
            return age / THUMP_RISE_S
        return math.exp(-(age - THUMP_RISE_S) / THUMP_TAU_S)

    def render(self, base, music, dt):
        since, period = self._cycle(music, dt)
        beat = max(self._thump(since), DUB_LEVEL * self._thump(since - DUB_AFTER_S),
                   # The tail of the previous cycle's dub, so it is not cut off
                   # when the next lub comes round quickly.
                   DUB_LEVEL * self._thump(since + period - DUB_AFTER_S))
        out = {}
        for f in self.patch:
            try:
                r = self.space.spot(f.fid).radius
            except KeyError:
                r = 0.0
            reach = 1.0 - HEART_EDGE * clamp(r)
            level = HEART_FLOOR + (1.0 - HEART_FLOOR) * beat * reach
            out[f.fid] = Emission(HEART_RGB, level, 0.0, 0.0, 0.0)
        return out


EFFECTS: tuple[type[Effect], ...] = (LightningEffect, CandleEffect, HeartbeatEffect)
BY_NAME = {cls.name: cls for cls in EFFECTS}


class EffectRack:
    """The effects in play, layered over the engine's output.

    `fire` and `clear` come from web and cue threads; `apply` from the engine
    tick. One lock covers both, and apply is cheap, so it is held throughout.
    """

    def __init__(self, patch: Patch, space: Space, rng: random.Random | None = None):
        self._lock = threading.Lock()
        rng = rng or random.Random()
        self.effects = {cls.name: cls(patch, space, rng) for cls in EFFECTS}
        self.patch = patch
        #: The hold fading in or up, and its level 0..1 before easing.
        self._hold: str | None = None
        self._level = 0.0
        #: A hold fading out, after being switched off or replaced.
        self._leaving: str | None = None
        self._leaving_level = 0.0
        #: The lightning strike in progress, and the clock for MIN_GAP_S.
        self._strike = False
        self._since_strike = math.inf

    # -- control (any thread) ---------------------------------------------

    def fire(self, name: str) -> dict:
        """Fire a hit, or toggle a hold. Raises KeyError for an unknown name."""
        if name == "off":
            self.clear()
            return {"effect": None}
        if name not in self.effects:
            raise KeyError(f"unknown effect {name!r}")
        effect = self.effects[name]
        with self._lock:
            if effect.kind == "hit":
                if self._since_strike < MIN_GAP_S:
                    return {"effect": name, "fired": False,
                            "wait": round(MIN_GAP_S - self._since_strike, 2)}
                effect.reset()
                self._strike = True
                self._since_strike = 0.0
                return {"effect": name, "fired": True}
            if self._hold == name:
                self._release()
                return {"effect": None}
            if self._hold is not None:
                self._release()
            self._hold = name
            self._level = 0.0
            effect.reset()
            return {"effect": name}

    def clear(self) -> None:
        """Fade out any hold. A strike already under way finishes."""
        with self._lock:
            if self._hold is not None:
                self._release()

    def _release(self) -> None:
        # Whatever was leaving is cut short; the newer hold matters more.
        self._leaving, self._leaving_level = self._hold, _ease(self._level)
        self._hold, self._level = None, 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {"hold": self._hold, "lightning": self._strike}

    # -- the tick ---------------------------------------------------------

    def apply(self, emissions: dict[str, Emission], music, dt: float) -> dict[str, Emission]:
        with self._lock:
            self._since_strike += dt
            if self._leaving is not None:
                self._leaving_level -= dt / HOLD_FADE_OUT_S
                if self._leaving_level <= 0.0:
                    self._leaving = None
            if self._hold is not None:
                self._level = min(1.0, self._level + dt / HOLD_FADE_IN_S)
            if self._hold is None and self._leaving is None and not self._strike:
                return emissions

            out = emissions
            for name, level in ((self._leaving, self._leaving_level),
                                (self._hold, _ease(self._level))):
                if name is None or level <= 0.0:
                    continue
                layer = self.effects[name].render(out, music, dt)
                out = {f.fid: out.get(f.fid, BLACK).blended(layer.get(f.fid, BLACK), level)
                       for f in self.patch}
            if self._strike:
                strike = self.effects["lightning"]
                out = strike.render(out, music, dt)
                if strike.done:
                    self._strike = False
            return out
