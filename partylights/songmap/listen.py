"""The listening pass: play every song once, silently, and map it.

    python -m partylights.cli listen <playlist link>     # run (resumes itself)
    python -m partylights.cli listen --check             # just the checks
    python -m partylights.cli listen --status            # how far along

For each track: arm the recorder, tell Spotify to play the track from the top
on this Mac, watch it every second and a half, and stop recording a moment
after it ends. Every poll of Spotify's position, stamped on the same clock as
the recording, says where song time zero fell in the take; the median of a
couple of hundred of them pins it to within a few tens of milliseconds. Then
the take is checked (no lost samples, nothing else played, no skipping),
analysed, and saved. A bad take is played again; songs already mapped are
skipped, so stopping and restarting costs at most one song.

See LISTENING_PASS.md for the whole picture.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .recorder import SR, Take
from .songmap import Store, analyse, summary, write_json
from .spotify import Spotify, SpotifyError

log = logging.getLogger(__name__)

POLL_S = 1.5
#: Recorded either side of the song, so an origin estimate a little off loses
#: nothing: the analysis starts LEAD_S before song time zero.
LEAD_S = 1.0
TAIL_S = 2.0
#: A track must be seen playing within this long of the play command.
START_TIMEOUT_S = 10.0
#: Spotify's position may wander against the wall clock by this much between
#: polls before it counts as a skip or a stall.
JUMP_S = 1.2
#: Takes per track before giving up on it for this run.
MAX_TAKES = 3
#: Peak level (linear) below which BlackHole is hearing nothing.
SILENT_PEAK = 1e-3
#: Callback stamps further than this from the recording's clock line mean
#: samples went missing.
MAX_CLOCK_RESIDUAL_S = 0.015


@dataclass
class Problem:
    level: str        # "error" stops the pass; "warn" is shown and carried on
    message: str


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    retry: bool = True        # worth playing again
    fatal: bool = False       # the whole pass should stop
    notes: list[str] = field(default_factory=list)


def parse_playlist(text: str) -> str | None:
    m = re.search(r"playlist[:/]([A-Za-z0-9]{22})", text or "")
    if m:
        return m.group(1)
    bare = (text or "").strip().split("?")[0].rstrip("/")
    return bare if re.fullmatch(r"[A-Za-z0-9]{22}", bare) else None


def _mmss(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def _hours(s: float) -> str:
    h, m = divmod(int(s) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


class ListeningPass:
    def __init__(self, spotify, recorder, store: Store, *, jukebox_dir: Path,
                 out=print, now=time.monotonic, sleep=time.sleep):
        self.sp = spotify
        self.rec = recorder
        self.store = store
        self.jukebox_dir = Path(jukebox_dir)
        self.out = out
        self.now = now
        self.sleep = sleep
        self.device_id: str | None = None
        self._artists: dict[str, list[str]] = {}

    # -- checks --------------------------------------------------------------

    def preflight(self) -> list[Problem]:
        probs: list[Problem] = []
        try:
            me = self.sp.me()
            self.out(f"  Spotify: signed in as {me.get('display_name') or me.get('id')}")
        except SpotifyError as e:
            return [Problem("error", f"Spotify: {e}")]

        devices = self.sp.devices()
        dev = self._pick_device(devices)
        if dev is None:
            names = ", ".join(f"{d.get('name')} ({d.get('type')})" for d in devices) or "none"
            probs.append(Problem("error",
                "the Spotify app on this Mac isn't showing up as a device. Open Spotify on "
                f"this Mac and play anything for a second, then try again. (Seen: {names})"))
        else:
            self.device_id = dev["id"]
            self.out(f"  Playing on: {dev.get('name')}")

        pending = self._jukebox_pending()
        if pending:
            probs.append(Problem("error",
                f"the jukebox has {pending} song(s) waiting in its queue and would take over "
                "Spotify to play them. Clear the queue from the host page (or stop the jukebox) "
                "first."))

        out_name = self._default_output()
        if out_name is None:
            probs.append(Problem("warn", "couldn't read the Mac's sound output setting"))
        elif "blackhole" in out_name.lower():
            self.out(f"  Sound output: {out_name} — silent, good")
        elif "multi" in out_name.lower():
            probs.append(Problem("warn",
                f"sound output is '{out_name}', so the pass will play out loud through the "
                "speakers. For a silent pass, pick 'BlackHole 2ch' as the output (Option-click "
                "the volume icon in the menu bar)."))
        else:
            probs.append(Problem("error",
                f"sound output is '{out_name}', so nothing will reach BlackHole and every "
                "recording would be silent. Pick 'BlackHole 2ch' as the output (Option-click the "
                "volume icon in the menu bar), or the Multi-Output Device to listen along."))
        return probs

    @staticmethod
    def _pick_device(devices: list[dict]) -> dict | None:
        """The Spotify app on this Mac: a Computer named like this host,
        else the only Computer."""
        computers = [d for d in devices if d.get("type") == "Computer" and d.get("id")]
        host = os.uname().nodename.split(".")[0].lower().replace("-", " ")
        for d in computers:
            name = (d.get("name") or "").lower().replace("-", " ").replace("’", "'")
            if host and (host in name or name in host):
                return d
        return computers[0] if len(computers) == 1 else None

    def _jukebox_pending(self) -> int:
        db = self.jukebox_dir / "jukebox.db"
        if not db.exists():
            return 0
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                row = conn.execute("SELECT COUNT(*) FROM queue WHERE status IN "
                                   "('pending', 'playing')").fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except sqlite3.Error:
            return 0

    @staticmethod
    def _default_output() -> str | None:
        try:
            import sounddevice as sd
            return sd.query_devices(kind="output")["name"]
        except Exception:
            return None

    # -- the track list ----------------------------------------------------------

    def load_tracks(self, source: str | None) -> list[dict]:
        saved = self.store.root / "playlist.json"
        if source:
            pid = parse_playlist(source)
            if not pid:
                raise SystemExit(f"that doesn't look like a Spotify playlist link: {source}")
            meta = self.sp.playlist(pid)
            tracks = self.sp.playlist_tracks(pid)
            self.store.root.mkdir(parents=True, exist_ok=True)
            write_json(saved, {"id": pid, "name": meta.get("name"),
                               "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "tracks": tracks})
            self.out(f"  Playlist: {meta.get('name')} — {len(tracks)} tracks")
            return tracks
        if not saved.exists():
            raise SystemExit("no playlist yet: run with the playlist link the first time")
        data = json.loads(saved.read_text())
        self.out(f"  Playlist: {data.get('name')} — {len(data['tracks'])} tracks (saved list)")
        return data["tracks"]

    # -- one take ------------------------------------------------------------------

    def _is_ours(self, item: dict | None, track: dict) -> bool:
        if not item:
            return False
        if item.get("id") == track["id"]:
            return True
        # Spotify sometimes plays a relinked copy (same song, another id).
        return (item.get("name") == track.get("name")
                and [a.get("name") for a in item.get("artists", [])]
                == [a.get("name") for a in track.get("artists", [])])

    def capture(self, track: dict) -> tuple[Take | None, list[tuple[float, float]], Verdict]:
        dur = track["duration_ms"] / 1000.0
        self.rec.arm(dur + LEAD_S + TAIL_S + START_TIMEOUT_S + 20.0)
        polls: list[tuple[float, float]] = []
        try:
            self.sp.play(track["uri"], self.device_id)
        except SpotifyError as e:
            self.rec.disarm()
            return None, [], Verdict(False, f"Spotify wouldn't play it: {e}",
                                     retry=e.status >= 500 or e.status == 0)
        t_play = self.now()
        verdict = Verdict(True)
        last_ours = 0.0
        self.followed = False
        while True:
            self.sleep(POLL_S)
            t1 = self.now()
            try:
                st = self.sp.state()
            except SpotifyError as e:
                log.warning("state poll failed: %s", e)
                st = None
                if t1 - t_play > dur + 60:
                    verdict = Verdict(False, "lost contact with Spotify")
                    break
                continue
            t2 = self.now()
            tm = 0.5 * (t1 + t2)
            item = (st or {}).get("item")
            ours = self._is_ours(item, track)
            playing = bool(st and st.get("is_playing"))
            progress = (st or {}).get("progress_ms", 0) / 1000.0

            if st and st.get("currently_playing_type") == "ad":
                verdict = Verdict(False, "an advert played")
                break
            if ours and playing:
                if polls:
                    t_prev, p_prev = polls[-1]
                    if abs((progress - p_prev) - (tm - t_prev)) > JUMP_S:
                        verdict = Verdict(False, "the position jumped (skipped or seeked)")
                        break
                polls.append((tm, progress))
                last_ours = progress
            elif not polls:
                if tm - t_play > START_TIMEOUT_S:
                    verdict = Verdict(False, "the track never started playing")
                    break
                continue
            elif last_ours < dur - 3.0:
                what = "something else started playing" if item and not ours \
                    else "playback was paused or stopped"
                verdict = Verdict(False, what)
                break
            elif item and not ours:
                # Our song ended and Spotify's Autoplay moved on. Stop now;
                # song_audio() cuts the take at the end of our song so none
                # of the next one leaks into the map.
                self.followed = True
                break

            origin = float(np.median([t - p for t, p in polls]))
            if tm >= origin + dur + TAIL_S:
                break
            if tm - t_play > dur + 90:
                verdict = Verdict(False, "the track ran far past its length")
                break
        try:
            self.sp.pause(self.device_id)
        except SpotifyError as e:
            log.warning("pause failed: %s", e)
        take = self.rec.disarm()
        return take, polls, verdict

    def check_take(self, take: Take, polls) -> Verdict:
        if take.overflows:
            return Verdict(False, f"{take.overflows} audio overflow(s) — samples lost")
        resid = take.max_clock_residual_s()
        if resid > MAX_CLOCK_RESIDUAL_S:
            return Verdict(False, f"the recording clock slipped {resid * 1000:.0f} ms")
        if len(polls) < 5:
            return Verdict(False, "too few position readings to place the song")
        if not len(take.samples) or float(np.abs(take.samples).max()) < SILENT_PEAK:
            return Verdict(False, "the recording is silent — the Mac's sound isn't reaching "
                           "BlackHole", retry=False, fatal=True)
        return Verdict(True)

    def song_audio(self, take: Take, polls, duration_s: float,
                   followed: bool = False) -> tuple[np.ndarray, float, dict]:
        """The take cut to the song, from LEAD_S before song time zero to
        TAIL_S after its end -- or right at its end if Autoplay followed it.
        Returns (samples, t0, capture info)."""
        est = np.array([t - p for t, p in polls])
        origin = float(np.median(est))
        spread = float(np.median(np.abs(est - origin)))
        i = int(round(take.sample_at(origin) - LEAD_S * SR))
        samples = take.samples
        if i < 0:
            samples = np.concatenate((np.zeros(-i, dtype=np.float32), samples))
            i = 0
        tail = 0.05 if followed else TAIL_S
        j = i + int(round((LEAD_S + duration_s + tail) * SR))
        info = {"origin_spread_s": round(spread, 4), "polls": len(polls),
                "rate_error_ppm": round(take.rate_error() * 1e6, 1),
                "followed_by_autoplay": followed,
                "peak_db": round(20 * np.log10(max(float(np.abs(take.samples).max()), 1e-9)), 1)}
        return samples[i:j], -LEAD_S, info

    # -- metadata --------------------------------------------------------------------

    def track_meta(self, t: dict) -> dict:
        album = t.get("album") or {}
        images = sorted(album.get("images") or [], key=lambda im: -(im.get("width") or 0))
        art = None
        if images and album.get("id"):
            dest = self.store.art / f"{album['id']}.jpg"
            if dest.exists() or self.sp.download(images[0]["url"], dest):
                art = dest.name
        genres: list[str] = []
        for a in t.get("artists", [])[:2]:
            aid = a.get("id")
            if aid and aid not in self._artists:
                info = self.sp.artist(aid) or {}
                self._artists[aid] = info.get("genres") or []
            genres += [g for g in self._artists.get(aid, []) if g not in genres]
        return {"id": t["id"], "uri": t["uri"], "name": t.get("name"),
                "artists": [a.get("name") for a in t.get("artists", [])],
                "album": album.get("name"), "album_id": album.get("id"),
                "release_date": album.get("release_date"), "duration_ms": t.get("duration_ms"),
                "explicit": t.get("explicit"), "image_url": images[0]["url"] if images else None,
                "art": art, "genres": genres}

    # -- the run -------------------------------------------------------------------------

    def map_track(self, track: dict) -> tuple[bool, str]:
        """Up to MAX_TAKES takes of one track. Returns (mapped, note)."""
        reasons = []
        for attempt in range(1, MAX_TAKES + 1):
            take, polls, verdict = self.capture(track)
            if verdict.ok and take is not None:
                verdict = self.check_take(take, polls)
            if verdict.ok:
                samples, t0, info = self.song_audio(take, polls, track["duration_ms"] / 1000.0,
                                                    self.followed)
                song_map, frames, fp = analyse(samples, t0=t0)
                song_map["track"] = self.track_meta(track)
                song_map["capture"] = {
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "device": getattr(self.rec, "device_name", ""), "takes": attempt, **info}
                self.store.save(track["id"], song_map, frames, fp)
                self.store.append_log({"track": track["id"], "name": track.get("name"),
                                       "ok": True, "takes": attempt,
                                       "quality": song_map["quality"]["score"]})
                return True, self._one_line(song_map)
            reasons.append(verdict.reason)
            self.store.append_log({"track": track["id"], "name": track.get("name"),
                                   "ok": False, "take": attempt, "reason": verdict.reason})
            if verdict.fatal:
                raise SystemExit(f"\nStopping: {verdict.reason}.")
            if not verdict.retry:
                break
            self.out(f"      take {attempt} no good ({verdict.reason}); playing it again")
            self.sleep(2.0)
        return False, "; ".join(dict.fromkeys(reasons))

    @staticmethod
    def _one_line(m: dict) -> str:
        tm, mo = m["timing"], m.get("moments", {})
        bits = [f"{tm['bpm']:.0f} BPM", f"{len(m['sections'])} sections"]
        drops = mo.get("drops", [])
        if drops:
            bits.append(f"{len(drops)} drop{'s' if len(drops) > 1 else ''}")
        bits.append(f"quality {m['quality']['score']:.2f}")
        return ", ".join(bits)

    def run(self, tracks: list[dict], *, limit: int | None = None, redo: bool = False) -> dict:
        todo = [t for t in tracks if redo or not self.store.has(t["id"])]
        done_before = len(tracks) - len(todo)
        if limit is not None:
            todo = todo[:limit]
        self.out(f"\n  {done_before} already mapped, {len(todo)} to go "
                 f"(about {_hours(sum(t['duration_ms'] / 1000 + 8 for t in todo))}).\n")
        try:
            self.sp.no_repeat_no_shuffle(self.device_id)
        except Exception:
            pass
        results = {"mapped": 0, "failed": []}
        for n, t in enumerate(todo, 1):
            artists = ", ".join(a.get("name", "") for a in t.get("artists", []))
            self.out(f"  [{n}/{len(todo)}] {t.get('name')} — {artists} "
                     f"({_mmss(t['duration_ms'] / 1000)})")
            ok, note = self.map_track(t)
            left = sum(x["duration_ms"] / 1000 + 8 for x in todo[n:])
            if ok:
                results["mapped"] += 1
                self.out(f"      ✓ {note}" + (f"   · {_hours(left)} left" if left else ""))
            else:
                results["failed"].append((t.get("name"), note))
                self.out(f"      ✗ skipped: {note}")
        return results


def status(store: Store, out=print) -> None:
    saved = store.root / "playlist.json"
    if not saved.exists():
        out("No listening pass started yet.")
        return
    data = json.loads(saved.read_text())
    tracks = data["tracks"]
    mapped = [t for t in tracks if store.has(t["id"])]
    left = [t for t in tracks if not store.has(t["id"])]
    out(f"{data.get('name')}: {len(mapped)}/{len(tracks)} mapped, "
        f"{_hours(sum(t['duration_ms'] / 1000 + 8 for t in left))} of listening left.")
    failed: dict[str, str] = {}
    if store.log.exists():
        for line in store.log.read_text().splitlines():
            e = json.loads(line)
            if e.get("ok"):
                failed.pop(e["track"], None)
            else:
                failed[e["track"]] = f"{e.get('name')}: {e.get('reason')}"
    failed = {k: v for k, v in failed.items() if not store.has(k)}
    if failed:
        out(f"{len(failed)} failed last time (they'll be tried again):")
        for v in list(failed.values())[:20]:
            out(f"  {v}")
    low = []
    for t in mapped:
        q = store.load(t["id"]).get("quality", {}).get("score", 0)
        if q < 0.5:
            low.append(f"{t.get('name')} ({q:.2f})")
    if low:
        out(f"{len(low)} mapped with low confidence (designs will lean on live reaction): "
            + ", ".join(low[:15]) + ("…" if len(low) > 15 else ""))


def keep_awake() -> subprocess.Popen | None:
    """Stop the Mac idle-sleeping (and system-sleeping on power) while we run."""
    try:
        return subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
    except OSError:
        return None


def main(args, settings) -> int:
    from .recorder import Recorder

    store = Store()
    if args.status:
        status(store)
        return 0
    jukebox_dir = Path(args.jukebox_dir).expanduser().resolve()
    sp = Spotify(jukebox_dir)
    rec = Recorder(settings.get("audio.device", "BlackHole"))
    lp = ListeningPass(sp, rec, store, jukebox_dir=jukebox_dir)

    print("Checking…")
    probs = lp.preflight()
    for p in probs:
        print(f"  {'✗' if p.level == 'error' else '!'} {p.message}")
    if any(p.level == "error" for p in probs):
        print("\nFix the ✗ items above, then run this again.")
        return 1
    if args.check:
        print("\nAll set.")
        return 0

    tracks = lp.load_tracks(args.playlist)
    caff = keep_awake()
    rec.start()
    print("\nRecording from", rec.device_name, "— leave the Mac plugged in and the lid open.")
    print("Ctrl+C stops after pausing Spotify; run the same command to pick up where it left off.")
    try:
        res = lp.run(tracks, limit=args.limit, redo=args.redo)
    except KeyboardInterrupt:
        print("\nStopped. Nothing finished is lost; run the same command to carry on.")
        return 130
    finally:
        try:
            sp.pause(lp.device_id)
        except Exception:
            pass
        rec.stop()
        if caff is not None:
            caff.terminate()
    print(f"\nDone: {res['mapped']} mapped" + (f", {len(res['failed'])} skipped" if res["failed"]
                                                 else "") + ".")
    for name, why in res["failed"]:
        print(f"  ✗ {name}: {why}")
    return 0


def show(query: str, out=print) -> int:
    store = Store()
    ids = store.track_ids()
    hits = [i for i in ids if i == query] or [
        i for i in ids if query.lower() in (store.load(i).get("track", {}).get("name") or "").lower()]
    if not hits:
        out(f"no mapped song matches {query!r}")
        return 1
    for i in hits[:5]:
        out(summary(store.load(i)))
        out("")
    return 0
