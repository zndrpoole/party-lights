"""Web UI and HTTP API on port 5055.

Separate from the jukebox's Flask app on 5000, and bound to 0.0.0.0 so the host
can run the rig from a phone while actually hosting. Three surfaces:

  /         full control: faders, colour, looks, palette, per-fixture override
  /live     big-button cue page with keyboard shortcuts, for fast hands
  /api/*    JSON, including /api/cue/<name> which is what a Stream Deck hits

Everything mutating is a POST. Nothing here touches DMX directly: requests
change EngineState or fire cues, and the engine picks them up on its next tick.
That keeps the real-time path free of HTTP.
"""

from __future__ import annotations

import json
import logging
import time

from flask import Flask, Response, jsonify, render_template, request

from ..engine import palette as palettes
from ..engine.looks import LOOKS

log = logging.getLogger(__name__)


def create_app(*, patch, universe, state, engine, cues, capture=None, jukebox=None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
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
        }

    def music_payload() -> dict:
        m = engine.music
        if m is None:
            return {"available": False}
        return {
            "available": True,
            "t": round(m.t, 2),
            "energy": round(m.energy, 3),
            "sustained_energy": round(engine.sustained_energy, 3),
            "silent": m.silent,
            "loudness_db": round(m.frame.loudness_db, 1),
            "bpm": round(m.bpm, 1),
            "beat_phase": round(m.beat_phase, 3),
            "bar_phase": round(m.bar_phase, 3),
            "beat_index": m.beat_index,
            "tempo_locked": m.tempo_locked,
            "tempo_confidence": round(m.tempo_confidence, 2),
            "centroid": round(m.frame.centroid, 0),
            "bands": {k: round(v, 3) for k, v in m.frame.bands_smooth.items()},
            "flux": {k: round(v, 3) for k, v in m.frame.flux.items()},
            "onsets": dict(m.frame.onsets),
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

    # -- pages -----------------------------------------------------------

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/live")
    def live():
        return render_template("live.html")

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
                yield f"data: {json.dumps(state_payload())}\n\n"
                time.sleep(0.05)
        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

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
