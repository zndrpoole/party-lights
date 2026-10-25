"""The jukebox poller: /api/now with the song's position, /api/status as a fallback."""
from partylights.jukebox import client as jc


class Resp:
    def __init__(self, code, data):
        self.status_code, self._data = code, data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._data


NOW = {"current_track": {"track_id": "abc", "track_name": "Song", "artist": "A",
                         "is_playing": True, "position_s": 42.5}}


def test_reads_the_position_from_api_now(monkeypatch):
    seen = []
    monkeypatch.setattr(jc.requests, "get", lambda url, timeout: seen.append(url) or Resp(200, NOW))
    info = jc.JukeboxClient("http://jb").poll_once()
    assert seen == ["http://jb/api/now"]
    assert (info.track_id, info.position_s, info.is_playing) == ("abc", 42.5, True)
    assert info.at > 0


def test_falls_back_to_api_status_on_an_old_jukebox(monkeypatch):
    seen = []

    def get(url, timeout):
        seen.append(url)
        if url.endswith("/api/now"):
            return Resp(404, None)
        return Resp(200, {"current_track": {"track_id": "abc", "track_name": "Song"}})
    monkeypatch.setattr(jc.requests, "get", get)
    c = jc.JukeboxClient("http://jb")
    assert c.poll_once().position_s is None
    c.poll_once()
    assert seen == ["http://jb/api/now", "http://jb/api/status", "http://jb/api/status"]
