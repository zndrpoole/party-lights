"""Draft a design card for every mapped song that doesn't have one.

    ./.venv/bin/python tools/design_cards.py            # draft cards for new maps
    ./.venv/bin/python tools/design_cards.py --refresh  # also redo earlier drafts
    ./.venv/bin/python tools/design_cards.py --list     # what has a card, and how

A draft follows the song's map the way the first hand-made card does: a look
per section, picked by the section's role and energy and varied so repeats
don't look the same; total black for the song's stops; a build that speeds up
into each drop, one beat of dark before it and a white hit on it; a bar of
strobe on the last, biggest drop (skipped at a low vibe); and an ending that
matches the song's (a final hit and quick fade, or a slow fade out), plus a
white hit where the music slams back in after a stop.

Drafts carry no colours of their own (`palette: {from: pool}`), so the show
wears the rig's palette: the host's pool and auto switching work as usual.
They're marked `generated: true`. A card without that mark was made or edited
by hand and is never touched; delete the mark to keep a draft you've edited.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from partylights.engine.looks import BY_NAME as LOOKS  # noqa: E402

SONGS = ROOT / "songs"

#: Busy looks per section role, in the order repeats take them, and the calm
#: look each falls back to below the calm vibe.
ROLE_LOOKS = {
    "intro": (["drift", "wash", "swell"], None),
    "verse": (["pulse", "callresponse", "ripple", "pingpong"], "wash"),
    "chorus": (["mirror", "rotor", "unison", "tide"], "swell"),
    "build": (["stepper", "tide", "sparkle"], "swell"),
    "drop": (["knockout", "beams", "unison"], "pulse"),
    "breakdown": (["hush", "uv", "drift"], None),
    "outro": (["wash", "drift"], None),
}
OTHER = (["sparkle", "ripple", "wash"], "wash")
#: A section quieter than this (map energy, 0..1) gets a quiet look whatever its role.
QUIET = 0.12
#: Below these the beat grid isn't trusted: no tempo-locked looks, no beat-timed cues.
MIN_TEMPO_CONFIDENCE = 0.5
MIN_QUALITY = 0.25


def mmss(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:05.2f}"


def flow(d: dict) -> str:
    """One YAML flow mapping, strings quoted."""
    parts = []
    for k, v in d.items():
        if isinstance(v, str) and k not in ("do", "look", "calm", "role", "color", "to", "uv"):
            v = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, float):
            v = f"{v:g}"
        parts.append(f"{k}: {v}")
    return "{" + ", ".join(parts) + "}"


def draft(song_map: dict) -> str:
    track = song_map["track"]
    timing = song_map["timing"]
    moments = song_map.get("moments", {})
    sections = song_map.get("sections", [])
    quality = float(song_map.get("quality", {}).get("score", 0.0))
    bpm = float(timing.get("bpm") or 120.0)
    beat = 60.0 / bpm
    timed = quality >= MIN_QUALITY and float(timing.get("confidence", 0)) >= MIN_TEMPO_CONFIDENCE

    def usable(name):
        return timed or not getattr(LOOKS[name], "needs_tempo", False)

    drops = moments.get("drops", [])
    energies = [float(s.get("energy", 0)) for s in sections] or [0.3]
    loud = sorted(energies)[len(energies) * 3 // 4]
    vibe = round(min(0.85, 0.35 + 0.6 * min(loud, 0.6) + (0.1 if drops else 0.0)), 2)

    lines_sections, used = [], {}
    intro_steps = [s for s in sections if s.get("role") == "intro"]
    for s in sections:
        role = s.get("role", "")
        busy, calm = ROLE_LOOKS.get(role, OTHER)
        options = [n for n in busy if usable(n)] or ["wash"]
        energy = float(s.get("energy", 0))
        entry = {"at": mmss(float(s["start"])), "role": role or "section"}
        if role == "intro":
            i = intro_steps.index(s)
            entry["look"] = options[min(i, len(options) - 1)]
            entry["level"] = round(0.3 + 0.45 * i / max(1, len(intro_steps) - 1), 2)
        elif energy < QUIET and role not in ("breakdown", "outro"):
            entry["look"] = "drift"
            entry["level"] = 0.5
        else:
            n = used.get(role, 0)
            used[role] = n + 1
            entry["look"] = options[n % len(options)]
            if calm and calm != entry["look"]:
                entry["calm"] = calm
            if role == "outro":
                entry["level"] = 0.5
        entry["note"] = f"{role or 'section'}, {s.get('bars', '?')} bars, energy {energy:.2f}"
        lines_sections.append(entry)

    cues = []
    for g in moments.get("gaps", []):
        if float(g["end"]) - float(g["start"]) >= 0.25:
            cues.append({"at": mmss(float(g["start"])), "until": mmss(float(g["end"])),
                         "do": "blackout", "note": "the stop: black for as long as the silence"})
    for h in moments.get("hits", []):
        t = float(h["time"])
        if any(0 <= t - float(g["end"]) < 1.0 for g in moments.get("gaps", [])):
            cues.append({"at": mmss(t), "do": "flash", "color": "white",
                         "note": "the hit that ends the stop"})
    if timed:
        for b in moments.get("builds", []):
            start, end = float(b["start"]), float(b.get("drop") or b["end"])
            if end - start >= 2.0:
                cues.append({"at": mmss(start), "until": mmss(end), "do": "build",
                             "whiten": "0.0 → 0.8", "level": "0.5 → 1.0",
                             "note": "speeds up, washes toward white, climbs"})
        for i, d in enumerate(drops):
            t = float(d["time"])
            if any(abs(float(b.get("drop") or -9) - t) < 0.1 for b in moments.get("builds", [])):
                cues.append({"at": mmss(t - beat), "until": mmss(t), "do": "blackout",
                             "note": "a beat of dark so the drop lands harder"})
            cues.append({"at": mmss(t), "do": "flash", "color": "white", "fixtures": "all",
                         "note": f"DROP {i + 1}"})
            if len(drops) > 1 and i == len(drops) - 1:
                cues.append({"at": mmss(t), "until": mmss(t + 4 * beat), "do": "strobe",
                             "vibe_min": 0.6, "note": "the last drop is the biggest: a bar of strobe"})
    ending = moments.get("ending") or {}
    last = float(moments.get("last_sound") or track.get("duration_ms", 0) / 1000)
    if ending.get("type") == "hard" and ending.get("final_hit"):
        hit = float(ending["final_hit"])
        cues.append({"at": mmss(hit), "do": "flash", "color": "white", "fixtures": "all",
                     "note": "final hit"})
        cues.append({"at": mmss(hit + 0.08), "do": "fade", "to": "black",
                     "over": f"{max(0.8, float(ending.get('ring_s', 1.0))):.1f} s"})
    elif last > 10:
        cues.append({"at": mmss(last - 4.0), "do": "fade", "to": "black", "over": "4 s",
                     "note": "fade out with the song"})
    cues.sort(key=lambda c: c["at"])

    artists = ", ".join(track.get("artists", []))
    out = [
        f"# Design card (generated draft) — {track.get('name', '')}, {artists}",
        f"# Map: songs/maps/{track['id']}.json  ({bpm:.0f} BPM, map quality {quality:.2f}"
        + ("" if timed else ", beat grid not trusted: no beat-timed cues") + ")",
        "# Made by tools/design_cards.py. Delete `generated: true` to keep your edits.",
        "",
        "schema: 1",
        f"track: {track['id']}",
        "generated: true",
        f"map_quality: {quality:.2f}",
        "palette: {from: pool}      # wear the rig's palette, so the host's pool still applies",
        f"vibe: {vibe}",
        "",
        "sections:",
        *(f"  - {flow(e)}" for e in lines_sections),
        "",
        "cues:" + ("" if cues else " []"),
        *(f"  - {flow(c)}" for c in cues),
        "",
        "on_lost_lock: auto_in_palette",
        "",
    ]
    return "\n".join(out)


def is_generated(path: Path) -> bool:
    import yaml
    try:
        return bool((yaml.safe_load(path.read_text()) or {}).get("generated"))
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--refresh", action="store_true", help="redo earlier generated drafts")
    ap.add_argument("--list", action="store_true", help="list the cards and stop")
    ap.add_argument("--songs", default=str(SONGS), help=argparse.SUPPRESS)
    args = ap.parse_args()
    songs = Path(args.songs)
    (songs / "designs").mkdir(exist_ok=True)

    maps = sorted((songs / "maps").glob("*.json"))
    if args.list:
        for m in maps:
            card = songs / "designs" / f"{m.stem}.yaml"
            name = json.loads(m.read_text()).get("track", {}).get("name", m.stem)
            kind = "none" if not card.exists() else ("draft" if is_generated(card) else "hand")
            print(f"  {kind:5s}  {m.stem}  {name}")
        return 0

    made = kept = 0
    for m in maps:
        card = songs / "designs" / f"{m.stem}.yaml"
        if card.exists() and not (args.refresh and is_generated(card)):
            kept += 1
            continue
        try:
            text = draft(json.loads(m.read_text()))
        except (KeyError, ValueError, TypeError) as e:
            print(f"  skipped {m.stem}: {e}")
            continue
        card.write_text(text)
        made += 1
    print(f"{made} card(s) drafted, {kept} left as they were, {len(maps)} mapped song(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
