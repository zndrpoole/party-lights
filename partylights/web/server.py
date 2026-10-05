"""Web UI and HTTP API on port 5055.

Separate from the jukebox's Flask app on 5000, and bound to 0.0.0.0 so the host
can run the rig from a phone while actually hosting. Three surfaces:

  /         full control: faders, colour, looks, palette, per-fixture override
  /live     big-button cue page with keyboard shortcuts, for fast hands
  /viz      draggable stage plan showing what each fixture is doing
  /bench    test bench: raw channel values straight to chosen fixtures
  /api/*    JSON, including /api/cue/<name> which is what a Stream Deck hits

Everything mutating is a POST. Nothing here touches DMX directly: requests
change EngineState or fire cues, and the engine picks them up on its next tick.
That keeps the real-time path free of HTTP.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import numpy as np
from flask import Flask, Response, jsonify, render_template, request
from flask.json.provider import DefaultJSONProvider

from ..config import CONFIG_DIR
from ..engine import palette as palettes
from ..engine.cues import PRESETS
from ..engine.looks import LOOKS
from ..fixtures.profile import FixtureProfile

log = logging.getLogger(__name__)


def _numpy_default(o):
    """Turn numpy scalars and arrays into builtins for JSON.

    The audio analysis hands back numpy types, which json cannot encode. They
    only appear once music is playing, so a miss here passes every silent test
    and then takes the UI down mid-party.
    """
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


class _JSONProvider(DefaultJSONProvider):
    @staticmethod
    def default(o):
        try:
            return _numpy_default(o)
        except TypeError:
            return DefaultJSONProvider.default(o)


#: Where /viz keeps the fixture arrangement. Local, like settings.local.yaml:
#: it describes one room, not the project.
DEFAULT_LAYOUT = CONFIG_DIR / "layout.local.json"
#: Live tuning set from the UI (the softness slider), kept across restarts.
DEFAULT_TUNING = CONFIG_DIR / "tuning.local.json"


def create_app(*, patch, universe, state, engine, cues, capture=None, jukebox=None,
               layout_path: str | Path = DEFAULT_LAYOUT,
               tuning_path: str | Path = DEFAULT_TUNING) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.json = _JSONProvider(app)
    app.config["JSON_SORT_KEYS"] = False

    # -- helpers ---------------------------------------------------------

    def rig_payload() -> dict:
        return {
            "fixtures": [
                {
                    "id": f.fid,
                    "label": f.label,
                    "model": f.profile.model,
                    "address": f.address,
                    "channels": f.footprint,
                    "groups": list(f.groups),
                    "position": f.position,
                    "emitters": f.profile.emitters,
                    "has_uv": f.profile.has_uv,
                    "has_strobe": f.profile.has_strobe,
                    "min_dimmer": f.profile.min_dimmer,
                    "channel_map": [
                        {"channel": f.address + c.offset, "role": c.role, "note": c.note}
                        for c in sorted(f.profile.channels, key=lambda c: c.offset)
                    ],
                }
                for f in patch.ordered()
            ],
            "groups": patch.groups,
            "max_channel": patch.max_channel,
            "looks": [
                {"name": c.name, "description": c.description,
                 "manual_only": bool(getattr(c, "manual_only", False)),
                 "needs_tempo": bool(getattr(c, "needs_tempo", False))}
                for c in LOOKS
            ],
            "palettes": [
                {"name": p.name, "description": p.description,
                 "colors": [[round(c, 4) for c in rgb] for rgb in p.colors]}
                for p in palettes.PALETTES
            ],
            "cues": cues.available(),
            "presets": [
                {"name": name, "look": look, "palette": pal, "description": desc}
                for name, (look, pal, desc) in PRESETS.items()
            ],
        }

    def music_payload() -> dict:
        # Every value is cast to a builtin. The analysis is numpy, and numpy's
        # bool_ / int64 are not JSON serialisable -- but only once audio is
        # actually flowing, since the silent defaults are plain Python values.
        # Without the casts the UI works in testing and dies when the music starts.
        m = engine.music
        if m is None:
            return {"available": False}
        return {
            "available": True,
            "t": round(float(m.t), 2),
            "energy": round(float(m.energy), 3),
            "sustained_energy": round(float(engine.sustained_energy), 3),
            "silent": bool(m.silent),
            "loudness_db": round(float(m.frame.loudness_db), 1),
            "bpm": round(float(m.bpm), 1),
            "beat_phase": round(float(m.beat_phase), 3),
            "bar_phase": round(float(m.bar_phase), 3),
            "beat_index": int(m.beat_index),
            "tempo_locked": bool(m.tempo_locked),
            "tempo_confidence": round(float(m.tempo_confidence), 2),
            "centroid": round(float(m.frame.centroid), 0),
            "bands": {k: round(float(v), 3) for k, v in m.frame.bands_smooth.items()},
            "flux": {k: round(float(v), 3) for k, v in m.frame.flux.items()},
            "onsets": {k: bool(v) for k, v in m.frame.onsets.items()},
            "onset_counts": {k: int(v) for k, v in engine.onset_counts.items()},
            "novelty": round(m.novelty, 3),
            "events": list(m.events),
        }

    def state_payload() -> dict:
        return {
            "state": state.snapshot(),
            "engine": engine.stats(),
            "dmx": universe.stats(),
            "music": music_payload(),
            "audio": capture.stats() if capture else {"device": None},
            "jukebox": jukebox.stats() if jukebox else {"connected": False},
            "strobe_elapsed": round(state.strobe_elapsed(), 1),
            "server_time": round(time.time(), 2),
        }

    def wire_payload() -> dict:
        """The frame most recently handed to the driver, decoded per fixture.

        Built from the transmitted bytes, never from engine state, so the
        visualiser shows exactly what the receivers get: blackout, freeze and
        manual overrides included.
        """
        frame_no, frame = universe.sent_frame()
        return {
            "frame": frame_no,
            "fixtures": {
                f.fid: f.profile.decode(frame[f.address - 1 : f.last_channel])
                for f in patch.ordered()
            },
        }

    layout_file = Path(layout_path)
    tuning_file = Path(tuning_path)

    def write_json(path: Path, data: dict) -> None:
        # Write then rename, so a crash mid-save cannot leave half a file.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    # Restore the last softness, so a restart does not undo the night's tuning.
    try:
        saved = json.loads(tuning_file.read_text())
        state.set_softness(float(saved.get("softness", 0.0)))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError):
        log.warning("ignoring unreadable tuning file %s", tuning_file)

    def read_layout() -> dict:
        try:
            return json.loads(layout_file.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("ignoring unreadable layout file %s", layout_file)
            return {}

    # -- pages -----------------------------------------------------------

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/live")
    def live():
        return render_template("live.html")

    @app.route("/viz")
    def viz():
        return render_template("viz.html")

    @app.route("/bench")
    def bench():
        return render_template("bench.html")

    # -- read ------------------------------------------------------------

    @app.route("/api/rig")
    def api_rig():
        return jsonify(rig_payload())

    @app.route("/api/state")
    def api_state():
        return jsonify(state_payload())

    @app.route("/api/frame")
    def api_frame():
        """The literal bytes on the wire. For debugging a misbehaving fixture."""
        frame = universe.snapshot()
        return jsonify({
            "slots": list(frame),
            "fixtures": {
                f.fid: list(frame[f.address - 1 : f.last_channel])
                for f in patch.ordered()
            },
        })

    @app.route("/api/stream")
    def api_stream():
        """Server-sent events, for the live meters.

        Pushed at 20 Hz: fast enough that the meters look continuous, slow
        enough that a phone on the far side of the house keeps up.
        """
        def generate():
            while True:
                yield f"data: {json.dumps(state_payload(), default=_numpy_default)}\n\n"
                time.sleep(0.05)
        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    @app.route("/api/wire")
    def api_wire():
        """Server-sent events of the transmitted frame, for /viz.

        Paced at the DMX refresh rate so every frame the fixtures get can show.
        """
        period = 1.0 / universe.refresh_hz

        def generate():
            last = None
            while True:
                payload = wire_payload()
                if payload["frame"] != last:
                    last = payload["frame"]
                    yield f"data: {json.dumps(payload)}\n\n"
                time.sleep(period)
        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    @app.route("/api/layout", methods=["GET"])
    def api_layout():
        return jsonify(read_layout())

    @app.route("/api/layout", methods=["POST"])
    def api_layout_save():
        """Save where fixtures sit on the /viz stage, as 0..1 fractions."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "expected {fixture_id: {x, y}}"}), 400
        layout = {}
        for fid, pos in data.items():
            if fid not in patch.by_id:
                continue
            try:
                x, y = float(pos["x"]), float(pos["y"])
            except (TypeError, KeyError, ValueError):
                return jsonify({"error": f"bad position for {fid}"}), 400
            layout[fid] = {"x": round(min(max(x, 0.0), 1.0), 4),
                           "y": round(min(max(y, 0.0), 1.0), 4)}
        write_json(layout_file, layout)
        return jsonify(layout)

    @app.route("/api/softness", methods=["POST"])
    def api_softness():
        """Sharp (-1) to smooth (+1). Saved, so it survives a restart."""
        data = request.get_json(silent=True) or {}
        try:
            state.set_softness(float(data["softness"]))
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "softness must be a number -1..1"}), 400
        write_json(tuning_file, {"softness": state.softness})
        return jsonify({"softness": state.softness, "scale": round(state.softness_scale(), 3)})

    # -- write -----------------------------------------------------------

    @app.route("/api/mode", methods=["POST"])
    def api_mode():
        data = request.get_json(silent=True) or {}
        mode = data.get("mode")
        try:
            if mode is None:
                state.toggle_mode()
            else:
                state.set_mode(mode)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"mode": state.mode})

    @app.route("/api/look", methods=["POST"])
    def api_look():
        data = request.get_json(silent=True) or {}
        name = data.get("look", "")
        try:
            return jsonify(cues.look(name))
        except KeyError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/palette", methods=["POST"])
    def api_palette():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(cues.palette(data.get("palette")))
        except KeyError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/master", methods=["POST"])
    def api_master():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(cues.master(float(data.get("master", 1.0))))
        except (TypeError, ValueError):
            return jsonify({"error": "master must be a number 0..1"}), 400

    @app.route("/api/fixture/<fid>", methods=["POST"])
    def api_fixture(fid):
        if fid not in patch.by_id:
            return jsonify({"error": f"unknown fixture {fid}"}), 404
        data = request.get_json(silent=True) or {}
        fields = {}
        if "active" in data:
            fields["active"] = bool(data["active"])
        if "rgb" in data:
            rgb = data["rgb"]
            if not isinstance(rgb, (list, tuple)) or len(rgb) != 3:
                return jsonify({"error": "rgb must be [r, g, b]"}), 400
            fields["rgb"] = tuple(float(c) for c in rgb)
        for key in ("intensity", "uv", "strobe"):
            if key in data:
                fields[key] = float(data[key])
        try:
            m = state.update_manual(fid, **fields)
        except (ValueError, TypeError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"id": fid, "active": m.active, "rgb": list(m.rgb),
                        "intensity": m.intensity, "uv": m.uv, "strobe": m.strobe})

    @app.route("/api/fixtures", methods=["POST"])
    def api_fixtures():
        """Apply the same override to several fixtures — a group, usually."""
        data = request.get_json(silent=True) or {}
        ids = data.get("ids")
        if data.get("group"):
            ids = [f.fid for f in patch.group(data["group"])]
        if not ids:
            return jsonify({"error": "pass ids[] or group"}), 400
        fields = {k: data[k] for k in ("active", "intensity", "uv", "strobe") if k in data}
        if "rgb" in data:
            fields["rgb"] = tuple(float(c) for c in data["rgb"])
        applied = []
        for fid in ids:
            if fid in patch.by_id:
                state.update_manual(fid, **fields)
                applied.append(fid)
        return jsonify({"applied": applied})

    @app.route("/api/bench", methods=["POST"])
    def api_bench():
        """Hold fixtures at raw channel values, bypassing looks and profile.

        Body: {"ids": [...]} or {"group": "pars"}, plus {"values": {role: 0..255}}
        for any of dimmer/red/green/blue/white/amber/uv.
        """
        data = request.get_json(silent=True) or {}
        ids = data.get("ids")
        if data.get("group"):
            ids = [f.fid for f in patch.group(data["group"])]
        if not ids:
            return jsonify({"error": "pass ids[] or group"}), 400
        values = data.get("values") or {}
        if not isinstance(values, dict):
            return jsonify({"error": "values must be {role: 0..255}"}), 400
        bad = [r for r in values if r not in FixtureProfile.BENCH_ROLES]
        if bad:
            return jsonify({"error": f"not settable on the bench: {bad}"}), 400
        held = {}
        try:
            for fid in ids:
                if fid in patch.by_id:
                    held[fid] = state.set_raw(fid, values)
        except (TypeError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"held": held})

    @app.route("/api/bench/release", methods=["POST"])
    def api_bench_release():
        """Hand fixtures back to the show. No ids releases every one."""
        data = request.get_json(silent=True) or {}
        state.release_raw(data.get("ids"))
        return jsonify({"held": state.active_raw()})

    @app.route("/api/cue/<path:name>", methods=["POST", "GET"])
    def api_cue(name):
        """Fire a named cue.

        GET is accepted as well as POST purely so a Stream Deck's plain
        "open this URL" action works without any plugin or scripting.
        """
        try:
            return jsonify(cues.fire(name))
        except KeyError as e:
            return jsonify({"error": str(e), "available": cues.available()}), 404
        except Exception as e:
            log.exception("cue %s failed", name)
            return jsonify({"error": str(e)}), 500

    return app
