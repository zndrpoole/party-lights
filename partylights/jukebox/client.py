"""Polls the party jukebox for track context.

The jukebox (../juke-box) already knows things we would otherwise have to infer
from audio: exactly when a track changes, what it is, who requested it, and
whether the queue has run dry. A track boundary in particular is a free, exact
cue point for resetting the music analysis.

This is strictly a *bonus* input. Every failure path degrades to "no metadata,
keep reacting to the audio", because the lights must not stop when the jukebox
hiccups, restarts, or is simply not running. Nothing here is on the engine's
critical path: the poller runs on its own thread and only ever hands over
already-parsed results.

Reads the jukebox's existing /api/status endpoint. The jukebox is not modified.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrackInfo:
    track_id: str = ""
    title: str = ""
    artist: str = ""
    requested_by: str = ""
    is_playing: bool = False

    @property
    def known(self) -> bool:
        return bool(self.track_id or self.title)


class JukeboxClient:
    """Background poller with a track-change callback.

    `on_track_change(TrackInfo)` fires once per new track, on the poller thread.
    Keep the callback quick; the engine's handler only resets analysis state.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:5000",
        *,
        poll_seconds: float = 2.0,
        timeout: float = 2.0,
        on_track_change=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.poll_seconds = poll_seconds
        self.timeout = timeout
        self.on_track_change = on_track_change

        self.track = TrackInfo()
        self.connected = False
        self.last_error = ""
        self.polls = 0
        self.failures = 0

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Deliberately starts unset rather than empty-string, so the first
        # observed track counts as a change.
        self._last_track_id: str | None = None

    # -- polling ----------------------------------------------------------

    def poll_once(self) -> TrackInfo | None:
        """One request. Returns the current track, or None if unavailable."""
        try:
            resp = requests.get(f"{self.base_url}/api/status", timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            with self._lock:
                self.failures += 1
                self.connected = False
                self.last_error = str(e)
            return None

        with self._lock:
            self.polls += 1
            self.connected = True
            self.last_error = ""

        current = data.get("current_track") or {}
        if not isinstance(current, dict):
            current = {}
        # Field names come from the jukebox's _resolve_current_track()
        # (juke-box/app.py:88): track_id, track_name, artist, is_playing, plus
        # requested_by_nickname when the track came from the guest queue.
        info = TrackInfo(
            track_id=str(current.get("track_id") or ""),
            title=str(current.get("track_name") or ""),
            artist=str(current.get("artist") or ""),
            requested_by=str(current.get("requested_by_nickname") or ""),
            is_playing=bool(current.get("is_playing", bool(current))),
        )
        with self._lock:
            self.track = info
        return info

    def _run(self) -> None:
        log.info("Jukebox poller started against %s", self.base_url)
        announced_failure = False
        while not self._stop.is_set():
            info = self.poll_once()
            if info is None:
                # Log the outage once, not every two seconds all night.
                if not announced_failure:
                    log.warning("Jukebox unreachable at %s — carrying on with audio only (%s)",
                                self.base_url, self.last_error)
                    announced_failure = True
            else:
                if announced_failure:
                    log.info("Jukebox reachable again")
                    announced_failure = False
                key = info.track_id or info.title
                if key and key != self._last_track_id:
                    self._last_track_id = key
                    if self.on_track_change is not None:
                        try:
                            self.on_track_change(info)
                        except Exception:
                            log.exception("track-change handler failed")
            self._stop.wait(self.poll_seconds)
        log.info("Jukebox poller stopped")

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="JukeboxPoller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=self.poll_seconds + 1.0)
        self._thread = None

    def stats(self) -> dict:
        with self._lock:
            return {
                "url": self.base_url,
                "connected": self.connected,
                "polls": self.polls,
                "failures": self.failures,
                "last_error": self.last_error[:200],
                "track": {
                    "title": self.track.title,
                    "artist": self.track.artist,
                    "requested_by": self.track.requested_by,
                    "is_playing": self.track.is_playing,
                },
            }
