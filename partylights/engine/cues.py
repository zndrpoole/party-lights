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
from .looks import BY_NAME as LOOKS_BY_NAME

log = logging.getLogger(__name__)

#: Step size for the master dimmer cues.
MASTER_STEP = 0.1


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
        names = palettes.names()
        if name is None:
            try:
                idx = names.index(self.state.palette)
            except ValueError:
                idx = -1
            name = names[(idx + 1) % len(names)]
        elif name not in names:
            raise KeyError(f"unknown palette {name!r}")
        self.state.set_palette(name)
        return {"palette": name}

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
        """Back to music-driven, overrides released. The 'undo my fiddling' cue."""
        self.state.clear_manual()
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
            "master-up": self.master_up,
            "master-down": self.master_down,
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
            if kind == "master":
                return self.master(float(arg))

        if name in LOOKS_BY_NAME:
            return self.look(name)

        raise KeyError(f"unknown cue {name!r}")

    def available(self) -> list[str]:
        return [
            "blackout", "freeze", "mode", "next-look", "palette",
            "master-up", "master-down", "clear-manual", "resume-auto",
            *(f"look/{n}" for n in LOOKS_BY_NAME),
            *(f"palette/{n}" for n in palettes.names()),
        ]
