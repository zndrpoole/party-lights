"""Web API encoding.

Bug guarded: /api/state returned 500 "Object of type bool is not JSON
serializable" the moment music started. The analysis produces numpy bool_ /
int64 / float32, while the silent defaults are plain Python values, so every
silent test passed and the UI died at the party. Symptom: meters frozen and
controls unresponsive while the lights kept running.
"""

import json

import numpy as np
import pytest
from flask import Flask

from partylights.config import DEFAULT_RIG, load_patch
from partylights.dmx.null import NullDriver
from partylights.dmx.universe import Universe
from partylights.fixtures.color import Emission
from partylights.web.server import _JSONProvider, _numpy_default, create_app

NUMPY_PAYLOAD = {
    "silent": np.bool_(False),
    "beat_index": np.int64(7),
    "peak": np.float32(0.25),
    "bands": np.array([0.1, 0.2]),
    "onsets": {"kick": np.bool_(True)},
}


def test_api_json_provider_encodes_numpy_values():
    app = Flask(__name__)
    app.json = _JSONProvider(app)
    out = json.loads(app.json.dumps(NUMPY_PAYLOAD))
    assert out == {"silent": False, "beat_index": 7, "peak": 0.25,
                   "bands": [0.1, 0.2], "onsets": {"kick": True}}


def test_event_stream_encoding_handles_numpy_values():
    # /api/stream uses json.dumps directly, not Flask's provider.
    out = json.loads(json.dumps(NUMPY_PAYLOAD, default=_numpy_default))
    assert out["silent"] is False and out["onsets"]["kick"] is True


# -- stage visualiser -------------------------------------------------------
#
# /viz must show what the transmitter sends, not what the engine intended. These
# pin that: decode reads bytes back through the profile, and the wire stream is
# built from the driver's frame, so blackout shows as dark.

@pytest.fixture
def patch():
    return load_patch(DEFAULT_RIG)


def test_decode_reads_back_what_render_wrote(patch):
    par = patch.by_id["par1"].profile
    d = par.decode(par.render(Emission(rgb=(1.0, 0.0, 0.0), intensity=1.0)))
    assert d["dimmer"] == 1.0
    assert d["emitters"] == {"red": 1.0, "green": 0.0, "blue": 0.0}
    assert d["display"] == [255, 0, 0] and d["output"] == 1.0
    assert d["strobe"] == 0.0


def test_decode_shows_the_dimmer_and_strobe_rate(patch):
    par = patch.by_id["par1"].profile
    d = par.decode(bytes([0, 0, 0, 255, 255, 0, 0]))   # blue, dimmer 0, strobe max
    assert d["output"] == 0.0 and d["display"] == [0, 0, 0]
    assert d["strobe"] == 1.0
    assert par.decode(bytes([255, 0, 0, 0, 7, 0, 0]))["strobe"] == 0.0   # 0-7 is open


def test_decode_mixes_accent_white_amber_and_uv(patch):
    acc = patch.by_id["accent"].profile
    white = acc.decode(acc.render(Emission(rgb=(1.0, 1.0, 1.0))))
    assert white["emitters"]["white"] == 1.0 and white["display"] == [255, 255, 255]
    uv = acc.decode(acc.render(Emission(rgb=(0.0, 0.0, 0.0), uv=1.0)))
    assert uv["emitters"]["uv"] == 1.0 and uv["display"][2] > uv["display"][0] > 0


def _client(patch, tmp_path):
    universe = Universe(NullDriver(), slot_count=patch.max_channel)
    app = create_app(patch=patch, universe=universe, state=None, engine=None,
                     cues=None, layout_path=tmp_path / "layout.json",
                     tuning_path=tmp_path / "tuning.json")
    return app.test_client(), universe


def _first_event(client):
    resp = client.get("/api/wire")
    line = next(resp.response).decode()
    resp.close()
    return json.loads(line.removeprefix("data: "))


def test_wire_stream_comes_from_the_sent_frame_not_the_buffer(patch, tmp_path):
    client, universe = _client(patch, tmp_path)
    universe.set_block(1, patch.by_id["par1"].profile.render(Emission(rgb=(1, 1, 1))))
    # Nothing has been handed to the driver yet, so the rig is dark.
    assert _first_event(client)["fixtures"]["par1"]["output"] == 0.0


def test_layout_round_trips_and_drops_unknown_fixtures(patch, tmp_path):
    client, _ = _client(patch, tmp_path)
    assert client.get("/api/layout").get_json() == {}
    saved = client.post("/api/layout", json={"par1": {"x": 0.25, "y": 1.7},
                                             "ghost": {"x": 0, "y": 0}}).get_json()
    assert saved == {"par1": {"x": 0.25, "y": 1.0}}
    assert client.get("/api/layout").get_json() == saved
    assert client.post("/api/layout", json={"par1": {"x": "left"}}).status_code == 400


def test_softness_is_saved_and_restored_across_restarts(patch, tmp_path):
    from partylights.engine.state import EngineState
    tuning = tmp_path / "tuning.json"
    make = lambda st: create_app(patch=patch, universe=Universe(NullDriver()), state=st,
                                 engine=None, cues=None, layout_path=tmp_path / "l.json",
                                 tuning_path=tuning).test_client()
    client = make(EngineState())
    out = client.post("/api/softness", json={"softness": 0.5}).get_json()
    assert out == {"softness": 0.5, "scale": 2.0}
    assert client.post("/api/softness", json={"softness": "x"}).status_code == 400
    restarted = EngineState()
    make(restarted)
    assert restarted.softness == 0.5
