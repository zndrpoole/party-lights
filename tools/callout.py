"""Play a mapped song on Spotify and call out its map live, to check it by ear.

    ./.venv/bin/python tools/callout.py "the days"

Each moment is printed as it lands. The bottom line counts bars and beats as the map has them: tap along,
and "beat 1" should fall where the bar starts.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from partylights.songmap.songmap import Store  # noqa: E402
from partylights.songmap.spotify import Spotify  # noqa: E402



def mmss(s: float) -> str:
    return f"{int(s // 60)}:{s % 60:04.1f}"


def cues(m: dict) -> list[tuple[float, str]]:
    out = []
    for s in m["sections"]:
        tag = f" [{', '.join(s['tags'])}]" if s.get("tags") else ""
        out.append((s["start"], f"{s['role'].upper()} {s['label']} ({s['bars']} bars){tag}"))
    mo = m.get("moments", {})
    for d in mo.get("drops", []):
        out.append((d["time"], f"*** DROP *** (+{d['strength_db']:.0f} dB low end)"))
    for g in mo.get("gaps", []):
        out.append((g["start"] if "start" in g else g["time"], "GAP — music stops"))
    for h in mo.get("hits", []):
        out.append((h["time"], "HIT"))
    end = mo.get("ending") or {}
    if end.get("final_hit") is not None:
        out.append((end["final_hit"], "FINAL HIT"))
    return sorted(out)


def main() -> int:
    query = " ".join(sys.argv[1:]).lower()
    store = Store()
    ids = [i for i in store.track_ids()
           if i == query or query in (store.load(i).get("track", {}).get("name") or "").lower()]
    if not ids:
        print(f"no mapped song matches {query!r}")
        return 1
    tid = ids[0]
    m = store.load(tid)
    beats = m["timing"]["beats"]
    downs = set(m["timing"]["downbeats"])
    beat_no, bar_no = [], []
    n_bar, k = 0, 0
    for i in range(len(beats)):
        if i in downs:
            n_bar, k = n_bar + 1, 1
        else:
            k += 1
        bar_no.append(n_bar)
        beat_no.append(k)

    sp = Spotify(ROOT.parent / "juke-box")
    dev = next((d for d in sp.devices() if d.get("type") == "Computer"), None)
    if dev is None:
        print("open Spotify on this Mac first")
        return 1
    print(f"{m['track']['name']} — {m['timing']['bpm']:.1f} BPM. Starting from 0:00…\n")
    sp.play(f"spotify:track:{tid}", dev["id"])

    # Song time = Spotify position, re-anchored on the monotonic clock every second.
    anchor = None
    last_poll = 0.0
    todo = cues(m)
    bi = 0
    try:
        while True:
            now = time.monotonic()
            if now - last_poll > 1.0:
                st = sp.state()
                last_poll = now
                if st and st.get("item", {}).get("id") == tid and st.get("is_playing"):
                    pos = st["progress_ms"] / 1000.0
                    if anchor is None or abs((now - anchor) - pos) > 0.15:
                        anchor = now - pos
                elif anchor is not None and st and not st.get("is_playing"):
                    print("\npaused — stopping.")
                    return 0
            if anchor is None:
                time.sleep(0.02)
                continue
            t = now - anchor
            while todo and todo[0][0] <= t:
                ct, label = todo.pop(0)
                print(f"\r{'':60}\r>> {mmss(ct)}  {label}")
            while bi < len(beats) and beats[bi] <= t:
                bi += 1
            if bi:
                b = bi - 1
                mark = "  ■" if beat_no[b] == 1 else ""
                print(f"\r   {mmss(t)}   bar {bar_no[b]:3d}  beat {beat_no[b]}{mark}   ",
                      end="", flush=True)
            if t > m["moments"].get("last_sound", beats[-1]) + 2:
                print("\n\ndone.")
                return 0
            time.sleep(0.01)
    except KeyboardInterrupt:
        sp.pause(dev["id"])
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
