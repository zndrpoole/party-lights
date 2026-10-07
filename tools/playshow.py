"""Play a song's designed show on the rig, to see a design card.

    ./.venv/bin/python tools/playshow.py "the days"
    ./.venv/bin/python tools/playshow.py "the days" --from 1:05     # start just before a moment

Starts the song on this Mac's Spotify and tells the lights where it is every
second (POST /api/show), so the rig plays the song's design card in step with
it. Prints each section and cue as it lands. Ctrl+C pauses Spotify and hands
the rig back to auto mode.

Timing comes from Spotify's reported position, good to a tenth of a second or
so: enough to judge the colours, the looks and the arc, not frame-exact hits.
That is the live fingerprint lock's job, later.
"""
import argparse
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from partylights.engine.show import Card, song_seconds  # noqa: E402
from partylights.songmap.songmap import Store  # noqa: E402
from partylights.songmap.spotify import Spotify  # noqa: E402


def mmss(s: float) -> str:
    return f"{int(s // 60)}:{s % 60:04.1f}"


def timeline(card: Card) -> list[tuple[float, str]]:
    out = [(s.at, f"{s.look.upper():13s} {s.note}") for s in card.sections]
    out += [(c.at, f"  {c.do:9s}   {c.label}") for c in card.cues]
    return sorted(out, key=lambda x: x[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("song", nargs="+", help="part of the song's name, or its track id")
    ap.add_argument("--from", dest="start", default="0", help="start here, e.g. 1:05")
    ap.add_argument("--lights", default="http://localhost:5055")
    args = ap.parse_args()

    query = " ".join(args.song).lower()
    store = Store()
    designed = {p.stem for p in (store.root / "designs").glob("*.yaml")}
    ids = [i for i in store.track_ids() if i in designed and (
        i == query or query in (store.load(i).get("track", {}).get("name") or "").lower())]
    if not ids:
        print(f"no song with a design card matches {query!r}")
        return 1
    card = Card.load(ids[0])
    start = song_seconds(args.start)

    try:
        requests.get(f"{args.lights}/api/state", timeout=2).raise_for_status()
    except requests.RequestException as e:
        print(f"the lights aren't answering at {args.lights}: {e}")
        return 1

    sp = Spotify(ROOT.parent / "juke-box")
    dev = next((d for d in sp.devices() if d.get("type") == "Computer"), None)
    if dev is None:
        print("open Spotify on this Mac first")
        return 1
    if dev.get("volume_percent") == 0:
        print("note: Spotify's volume is at 0")
    print(f"{card.name} — design card, vibe {card.vibe}. Watch the lights.\n")
    sp.play(f"spotify:track:{card.track}", dev["id"], position_ms=int(start * 1000))

    events = [e for e in timeline(card) if e[0] >= start]
    anchor = None
    last_poll = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - last_poll >= 1.0:
                last_poll = now
                t0 = time.monotonic()
                st = sp.state()
                rtt = time.monotonic() - t0
                if st and st.get("item", {}).get("id") == card.track and st.get("is_playing"):
                    pos = st["progress_ms"] / 1000.0 + rtt / 2
                    requests.post(f"{args.lights}/api/show", timeout=1,
                                  json={"track": card.track, "position_s": pos})
                    if anchor is None or abs((time.monotonic() - anchor) - pos) > 0.15:
                        anchor = time.monotonic() - pos
                elif anchor is not None:
                    print("\nSpotify stopped or moved on — show over.")
                    break
            if anchor is not None:
                t = time.monotonic() - anchor
                while events and events[0][0] <= t:
                    at, label = events.pop(0)
                    print(f"{mmss(at)}  {label}")
                if t > card.end + 3:
                    print("\ndone.")
                    break
            time.sleep(0.02)
    except KeyboardInterrupt:
        sp.pause(dev["id"])
        print("\nstopped.")
    finally:
        try:
            requests.post(f"{args.lights}/api/show/stop", timeout=1)
        except requests.RequestException:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
