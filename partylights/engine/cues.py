"""Named cues: one action, many triggers.

Every manual action in the rig is a named cue, and every trigger -- the web
buttons, a keyboard shortcut, a Stream Deck button running curl, a MIDI pad
later -- fires the same cue by name through the same endpoint. Adding the Stream
Deck therefore needs no code: a button runs

    curl -X POST http://localhost:5055/api/cue/blackout

Adding MIDI later means mapping notes to these same names.
"""

from __future__ import annotations

import logging

from . import palette as palettes
from .effects import BY_NAME as EFFECTS_BY_NAME
from .looks import BY_NAME as LOOKS_BY_NAME

log = logging.getLogger(__name__)

#: Step size for the master dimmer and vibe cues.
MASTER_STEP = 0.1
VIBE_STEP = 0.1

#: A preset is a look and a palette chosen together, fired as one cue. Firing
#: one takes manual control, like any hand-picked look, so it holds until
#: resume-auto. Listed calm to wild, which is the order the UI shows them in.
#: None use a look that needs a tempo lock, since a preset can be fired at any
#: moment -- between songs, or over a track the tempo tracker has not locked.
PRESETS: dict[str, tuple[str, str, str]] = {
    "seance": ("uv", "deep-uv",
               "Blacklight from the accent over a dark violet bed — for a quiet moment"),
    "graveyard": ("drift", "graveyard",
                  "Cold blue-green clouds drifting through the yard, no hits"),
    "halloween-smooth": ("swell", "halloween-deep",
                         "Held orange and purple, slow swells, no hits"),
    "blood-moon": ("wash", "blood-moon",
                   "Crimson and violet across the yard, brightness riding the mix"),
    "cauldron": ("ripple", "witch",
                 "Green and purple rings bubbling out from the middle on every kick"),
    "monster-mash": ("unison", "candy-corn",
                     "The whole yard hitting together on the downbeat in candy-corn colours"),
    "toxic": ("mirror", "acid",
              "Lime and magenta bursting from the centre out and back"),
    "fright-night": ("knockout", "blood-moon",
                     "Lit red, every kick cuts it dark — the hardest preset"),
}


class CueRouter:
    """Resolves cue names to actions against the engine and universe."""

    def __init__(self, engine, universe, state):
        self.engine = engine
        self.universe = universe
        self.state = state

    # -- individual cues --------------------------------------------------

    def blackout(self) -> dict:
        """Panic button. Toggles a hard zero on every slot at the last moment
        before the wire, so nothing upstream can override it."""
        on = not self.universe.blackout
        self.universe.set_blackout(on)
        self.state.blackout = on
        return {"blackout": on}

    def freeze(self) -> dict:
        """Hold the current frame. For a toast or a speech: the lights stop
        moving without the room going dark."""
        on = not self.universe.frozen
        self.universe.set_freeze(on)
        self.state.freeze = on
        return {"freeze": on}

    def mode(self) -> dict:
        """Hand control between the music and the host."""
        return {"mode": self.state.toggle_mode()}

    def next_look(self) -> dict:
        name = self.engine.next_look()
        self.state.set_look(name)
        return {"look": name}

    def look(self, name: str) -> dict:
        if name not in LOOKS_BY_NAME:
            raise KeyError(f"unknown look {name!r}")
        # Selecting a look by hand implies taking manual control; otherwise the
        # auto selector would immediately override the choice, which from the
        # host's point of view looks like the button not working.
        self.state.set_mode("manual")
        self.state.set_look(name)
        self.engine.select(name)
        return {"look": name, "mode": "manual"}

    def palette(self, name: str | None = None) -> dict:
        """Pick a palette by name, or with no name step to the next one in the
        active pool. Never touches palette auto switching."""
        if name is None:
            pool = list(self.state.palette_pool)
            try:
                idx = pool.index(self.state.palette)
            except ValueError:
                idx = -1
            name = pool[(idx + 1) % len(pool)]
        elif name not in palettes.BY_NAME:
            raise KeyError(f"unknown palette {name!r}")
        self.state.set_palette(name)
        return {"palette": name}

    def palette_auto(self) -> dict:
        """Toggle shuffling the palette on each song change."""
        return {"palette_auto": self.state.toggle_palette_auto()}

    def preset(self, name: str) -> dict:
        if name not in PRESETS:
            raise KeyError(f"unknown preset {name!r}")
        look, palette, _ = PRESETS[name]
        self.palette(palette)
        result = self.look(look)
        return {"preset": name, "palette": palette, **result}

    def effect(self, name: str) -> dict:
        """Lightning fires once; candle and heartbeat toggle; "off" fades out
        whatever is held. Never touches mode, look or palette, so auto mode
        carries on underneath. See engine/effects.py."""
        result = self.engine.effects.fire(name)
        log.info("effect %s -> %s", name, result)
        return result

    def vibe(self, value: float) -> dict:
        self.state.set_vibe(value)
        return {"vibe": self.state.vibe}

    def vibe_up(self) -> dict:
        return self.vibe(self.state.vibe + VIBE_STEP)

    def vibe_down(self) -> dict:
        return self.vibe(self.state.vibe - VIBE_STEP)

    def master(self, value: float) -> dict:
        self.state.set_master(value)
        return {"master": self.state.master}

    def master_up(self) -> dict:
        return self.master(self.state.master + MASTER_STEP)

    def master_down(self) -> dict:
        return self.master(self.state.master - MASTER_STEP)

    def clear_manual(self) -> dict:
        """Release every per-fixture override back to the looks."""
        self.state.clear_manual()
        return {"cleared": True}

    def resume_auto(self) -> dict:
        """Back to music-driven, overrides and effects released. The 'undo my
        fiddling' cue. Leaves the vibe alone: that is a setting, not fiddling."""
        self.state.clear_manual()
        self.engine.effects.clear()
        self.state.set_mode("auto")
        self.universe.set_freeze(False)
        self.state.freeze = False
        return {"mode": "auto"}

    # -- dispatch ---------------------------------------------------------

    def fire(self, name: str, **kwargs) -> dict:
        """Run a cue by name. Raises KeyError for an unknown cue."""
        simple = {
            "blackout": self.blackout,
            "freeze": self.freeze,
            "mode": self.mode,
            "next-look": self.next_look,
            "palette": self.palette,
            "palette-auto": self.palette_auto,
            "master-up": self.master_up,
            "master-down": self.master_down,
            "vibe-up": self.vibe_up,
            "vibe-down": self.vibe_down,
            "clear-manual": self.clear_manual,
            "resume-auto": self.resume_auto,
        }
        if name in simple:
            result = simple[name](**kwargs) if kwargs else simple[name]()
            log.info("cue %s -> %s", name, result)
            return result

        # "look/chase" and "palette/neon" forms, which is what a Stream Deck
        # button or a hotkey binds to.
        if "/" in name:
            kind, _, arg = name.partition("/")
            if kind == "look":
                return self.look(arg)
            if kind == "palette":
                return self.palette(arg)
            if kind == "preset":
                return self.preset(arg)
            if kind == "master":
                return self.master(float(arg))
            if kind == "effect":
                return self.effect(arg)
            if kind == "vibe":
                return self.vibe(float(arg))

        if name in LOOKS_BY_NAME:
            return self.look(name)

        raise KeyError(f"unknown cue {name!r}")

    def available(self) -> list[str]:
        return [
            "blackout", "freeze", "mode", "next-look", "palette", "palette-auto",
            "master-up", "master-down", "vibe-up", "vibe-down",
            "clear-manual", "resume-auto",
            *(f"look/{n}" for n in LOOKS_BY_NAME),
            *(f"palette/{n}" for n in palettes.names()),
            *(f"preset/{n}" for n in PRESETS),
            *(f"effect/{n}" for n in EFFECTS_BY_NAME), "effect/off",
        ]
