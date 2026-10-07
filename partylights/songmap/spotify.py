"""A small Spotify Web API client for the listening pass.

Borrows the jukebox's authorisation rather than asking for its own: the same
app credentials (juke-box/.env) and the same cached token (juke-box/
token_cache.json), refreshed and written back in the format spotipy expects,
so the jukebox carries on with the token as if it had refreshed it itself.

Only what the pass needs: read a playlist and a track, play one track on one
device, watch it, pause. Uses the post-2026 playlist paths (/items), falling
back to the old ones for apps that still have them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def read_env(path: Path) -> dict[str, str]:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Spotify:
    def __init__(self, jukebox_dir: Path):
        self.jukebox_dir = Path(jukebox_dir)
        env = read_env(self.jukebox_dir / ".env")
        self.client_id = env.get("SPOTIFY_CLIENT_ID") or os.environ.get("SPOTIFY_CLIENT_ID", "")
        self.client_secret = (env.get("SPOTIFY_CLIENT_SECRET")
                              or os.environ.get("SPOTIFY_CLIENT_SECRET", ""))
        self.cache = self.jukebox_dir / "token_cache.json"
        self._lock = threading.Lock()
        self._session = requests.Session()

    # -- auth ----------------------------------------------------------------

    def _token(self) -> str:
        with self._lock:
            if not self.cache.exists():
                raise SpotifyError("the jukebox has no Spotify login yet — connect Spotify "
                                   "from the jukebox host page first")
            info = json.loads(self.cache.read_text())
            if info.get("expires_at", 0) - 60 > time.time():
                return info["access_token"]
            if not (self.client_id and self.client_secret):
                raise SpotifyError("SPOTIFY_CLIENT_ID / SECRET missing from juke-box/.env")
            resp = self._session.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "refresh_token": info["refresh_token"]},
                auth=(self.client_id, self.client_secret), timeout=10)
            if resp.status_code != 200:
                raise SpotifyError(f"token refresh failed: {resp.status_code} {resp.text[:200]}",
                                   resp.status_code)
            new = resp.json()
            # Spotify may or may not rotate the refresh token. Keep the old one
            # if not, or the jukebox would lose its login.
            new.setdefault("refresh_token", info["refresh_token"])
            new["expires_at"] = int(time.time()) + int(new.get("expires_in", 3600))
            tmp = self.cache.with_name(self.cache.name + ".tmp")
            tmp.write_text(json.dumps(new))
            os.replace(tmp, self.cache)
            return new["access_token"]

    def _call(self, method: str, path: str, *, params=None, body=None, ok=(200, 201, 202, 204),
              retries: int = 3):
        url = path if path.startswith("http") else f"{API}{path}"
        for attempt in range(retries + 1):
            try:
                resp = self._session.request(
                    method, url, params=params, json=body, timeout=10,
                    headers={"Authorization": f"Bearer {self._token()}"})
            except requests.RequestException as e:
                if attempt == retries:
                    raise SpotifyError(f"{method} {path}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                wait = min(60, int(resp.headers.get("Retry-After", "5") or 5))
                log.warning("Spotify rate limit; waiting %d s", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code not in ok:
                raise SpotifyError(f"{method} {path}: {resp.status_code} {resp.text[:300]}",
                                   resp.status_code)
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise SpotifyError(f"{method} {path}: gave up after retries")

    # -- reads -----------------------------------------------------------------

    def me(self) -> dict:
        return self._call("GET", "/me")

    def devices(self) -> list[dict]:
        return (self._call("GET", "/me/player/devices") or {}).get("devices", [])

    def state(self) -> dict | None:
        """Current playback, or None when nothing is loaded."""
        return self._call("GET", "/me/player", params={"additional_types": "track"})

    def track(self, track_id: str) -> dict:
        return self._call("GET", f"/tracks/{track_id}")

    def artist(self, artist_id: str) -> dict | None:
        try:
            return self._call("GET", f"/artists/{artist_id}")
        except SpotifyError as e:
            log.info("artist %s unavailable (%s)", artist_id, e.status)
            return None

    def playlist(self, playlist_id: str) -> dict:
        return self._call("GET", f"/playlists/{playlist_id}", params={"fields": "id,name"})

    def playlist_tracks(self, playlist_id: str) -> list[dict]:
        """Every track item in a playlist, in order. Local files, podcast
        episodes and tracks Spotify marks unplayable here are skipped.

        Since February 2026 a development-mode app can only read the items of
        playlists the signed-in user *owns*; a followed playlist is a 403."""
        out = []
        for kind in ("items", "tracks"):
            url = f"/playlists/{playlist_id}/{kind}"
            params = {"limit": 50, "additional_types": "track"}
            try:
                while url:
                    page = self._call("GET", url, params=params)
                    params = None
                    for it in page.get("items", []):
                        t = it.get("item") or it.get("track")
                        if t and t.get("type", "track") == "track" and t.get("id") \
                                and not t.get("is_local") and t.get("is_playable", True):
                            out.append(t)
                    url = page.get("next")
                return out
            except SpotifyError as e:
                if e.status in (403, 404) and kind == "items" and not out:
                    continue
                raise
        return out

    def download(self, url: str, dest: Path) -> bool:
        try:
            resp = self._session.get(url, timeout=15)
            if resp.status_code == 200:
                tmp = dest.with_name(dest.name + ".tmp")
                tmp.write_bytes(resp.content)
                os.replace(tmp, dest)
                return True
        except requests.RequestException:
            pass
        return False

    # -- control -------------------------------------------------------------------

    def play(self, uri: str, device_id: str) -> None:
        self._call("PUT", "/me/player/play", params={"device_id": device_id},
                   body={"uris": [uri], "position_ms": 0})

    def pause(self, device_id: str | None = None) -> None:
        try:
            self._call("PUT", "/me/player/pause",
                       params={"device_id": device_id} if device_id else None,
                       ok=(200, 202, 204))
        except SpotifyError as e:
            # Pausing what is already paused is a 403 on some clients.
            if e.status not in (403, 404):
                raise

    def no_repeat_no_shuffle(self, device_id: str) -> None:
        for path, params in (("/me/player/repeat", {"state": "off"}),
                             ("/me/player/shuffle", {"state": "false"})):
            try:
                self._call("PUT", path, params={**params, "device_id": device_id})
            except SpotifyError as e:
                log.info("could not set %s: %s", path, e)
