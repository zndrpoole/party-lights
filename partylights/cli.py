"""Command line entry point.

    python -m partylights.cli run                 # the whole rig
    python -m partylights.cli run --no-audio      # UI and looks, no capture
    python -m partylights.cli run --driver null   # no hardware at all
    python -m partylights.cli audio-devices       # find your loopback device
    python -m partylights.cli rig                 # print the patch
    python -m partylights.cli cues                # list cues
    python -m partylights.cli streamdeck ~/cues   # write Stream Deck scripts

Startup order matters and is handled here: the DMX writer comes up first so the
fixtures are receiving frames (all zero) before anything tries to light them,
and shutdown always sends a final blackout frame. With no microcontroller in the
interface, fixtures hold their last value forever, and once this process exits
there is nothing left to turn them off.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from .config import Settings, build_universe, load_patch, setup_logging
from .engine.cues import CueRouter
from .engine.engine import Engine
from .engine.looks import BY_NAME as LOOKS_BY_NAME
from .engine.state import EngineState

log = logging.getLogger("partylights")


def cmd_audio_devices(args) -> int:
    from .audio.capture import list_input_devices

    devices = list_input_devices()
    if not devices:
        print("No input devices found.")
        return 1
    print(f"{'idx':>4}  {'ch':>3}  {'rate':>7}  name")
    for d in devices:
        print(f"{d['index']:>4}  {d['channels']:>3}  {d['default_rate']:>7.0f}  {d['name']}")
    print("\nFor the loopback tap, look for BlackHole. If it is missing:")
    print("  brew install --cask blackhole-2ch")
    print("\nThen build a Multi-Output Device in Audio MIDI Setup with your real")
    print("speakers as the Master and Drift Correction ticked on BlackHole.")
    print("Put the device name (or any unique part of it) in config/settings.yaml")
    print("under audio.device.")
    return 0


def cmd_rig(args) -> int:
    patch = load_patch(args.rig)
    print(patch.summary())
    print()
    print(f"groups: {', '.join(patch.groups)}")
    print(f"frame:  {patch.max_channel} slots "
          f"(~{(patch.max_channel + 1) * 11 / 250.0:.1f} ms on the wire per frame)")
    return 0


def cmd_cues(args) -> int:
    print("Cues (POST or GET /api/cue/<name>):\n")
    for name in ("blackout", "freeze", "mode", "next-look", "palette",
                 "palette-auto", "master-up", "master-down", "clear-manual", "resume-auto"):
        print(f"  {name}")
    print("\nLooks (as look/<name>):")
    for name, cls in LOOKS_BY_NAME.items():
        flag = "  [manual only]" if getattr(cls, "manual_only", False) else ""
        print(f"  look/{name:<10} {cls.description}{flag}")
    return 0


def cmd_streamdeck(args) -> int:
    """Write one shell script per cue, for the Stream Deck to run.

    The Stream Deck's built-in "System -> Open" action can run a file, so a
    one-line script per cue needs no plugin, no developer account and no
    scripting inside the Stream Deck software. Point a button at the script.
    """
    outdir = Path(args.outdir).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    base = f"http://{args.host}:{args.port}"

    # The six that fit a Stream Deck Mini. Everything else is still reachable
    # from the web UI, and the full cue list is in `cues`.
    suggested = [
        ("1-blackout", "blackout"),
        ("2-mode", "mode"),
        ("3-next-look", "next-look"),
        ("4-palette", "palette"),
        ("5-blinder", "look/blinder"),
        ("6-freeze", "freeze"),
    ]
    for filename, cue in suggested:
        path = outdir / f"{filename}.sh"
        path.write_text(
            "#!/bin/sh\n"
            f"# Stream Deck button: {cue}\n"
            f"exec curl -s -X POST {base}/api/cue/{cue} >/dev/null\n"
        )
        path.chmod(0o755)
        print(f"wrote {path}")

    print(f"\nIn the Stream Deck app, add a 'System -> Open' action to each button")
    print(f"and point it at the matching script in {outdir}.")
    print("Reassign freely — every cue is just a URL, so swapping buttons is a")
    print("one-line edit. `python -m partylights.cli cues` lists them all.")
    return 0


def cmd_run(args) -> int:
    settings = Settings.load(args.settings)
    patch = load_patch(args.rig)

    universe = build_universe(settings, patch, driver_override=args.driver)
    state = EngineState(
        look=settings.get("engine.start_look", "ambient"),
        palette=settings.get("engine.palette", "halloween"),
        palette_auto=bool(settings.get("engine.palette_auto", False)),
        max_strobe_seconds=float(settings.get("engine.max_strobe_seconds", 4.0)),
    )

    # Audio capture is optional: the UI, the looks and manual control all work
    # without it, which is what you want while setting up the rig.
    capture = None
    if args.audio_file:
        from .audio.capture import FilePlayback
        capture = FilePlayback(
            args.audio_file,
            sample_rate=int(settings.get("audio.sample_rate", 48000)),
            block_size=int(settings.get("audio.block_size", 512)),
        )
    elif not args.no_audio:
        from .audio.capture import AudioCapture
        capture = AudioCapture(
            device=settings.get("audio.device", "BlackHole"),
            sample_rate=int(settings.get("audio.sample_rate", 48000)),
            block_size=int(settings.get("audio.block_size", 512)),
            channels=int(settings.get("audio.channels", 2)),
        )

    engine = Engine(
        patch, universe, state,
        capture=capture,
        tick_hz=float(settings.get("engine.tick_hz", 100)),
        output_delay_ms=float(settings.get("audio.output_delay_ms", 0)),
        attack_ms=float(settings.get("engine.attack_ms", 0)),
        release_ms=float(settings.get("engine.release_ms", 0)),
    )
    cues = CueRouter(engine, universe, state)

    jukebox = None
    if settings.get("jukebox.enabled", True) and not args.no_jukebox:
        from .jukebox.client import JukeboxClient
        jukebox = JukeboxClient(
            settings.get("jukebox.url", "http://localhost:5000"),
            poll_seconds=float(settings.get("jukebox.poll_seconds", 2.0)),
            on_track_change=lambda info: engine.on_track_change(info.title, info.artist),
        )

    # --- bring it up, output first ---
    try:
        universe.driver.open()
    except Exception as e:
        print(f"\nCould not open the DMX output:\n  {e}\n")
        print("Run with --driver null to work without hardware.")
        return 1
    universe.start()

    if capture is not None:
        try:
            capture.start()
        except Exception as e:
            log.warning("Audio capture unavailable (%s) — running without it. "
                        "The looks will idle; manual control still works.", e)
            capture = None
            engine.capture = None

    engine.start()
    if jukebox is not None:
        jukebox.start()

    app = create_web_app(patch, universe, state, engine, cues, capture, jukebox)
    host = settings.get("web.host", "0.0.0.0")
    port = int(settings.get("web.port", 5055))

    stopping = threading.Event()

    def shutdown(*_):
        if stopping.is_set():
            return
        stopping.set()
        log.info("Shutting down — sending a final blackout frame")
        if jukebox is not None:
            jukebox.stop()
        engine.stop()
        if capture is not None:
            capture.stop()
        universe.stop(blackout=True)
        universe.driver.close()

    signal.signal(signal.SIGINT, lambda *a: (shutdown(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *a: (shutdown(), sys.exit(0)))

    print()
    print(f"  Output   {universe.driver.describe()}")
    print(f"  Frame    {universe.slot_count} slots at {universe.refresh_hz:.0f} Hz")
    print(f"  Fixtures {len(patch)} on channels 1..{patch.max_channel}")
    print(f"  Audio    {capture.device_name if capture else 'disabled'}")
    print(f"  Jukebox  {jukebox.base_url if jukebox else 'disabled'}")
    print()
    print(f"  Control  http://localhost:{port}/")
    print(f"  Live     http://localhost:{port}/live")
    print(f"  Stage    http://localhost:{port}/viz")
    print()

    try:
        app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
    finally:
        shutdown()
    return 0


def create_web_app(patch, universe, state, engine, cues, capture, jukebox):
    from .web.server import create_app
    return create_app(patch=patch, universe=universe, state=state,
                      engine=engine, cues=cues, capture=capture, jukebox=jukebox)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="partylights", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="INFO")
    ap.add_argument("--settings", default=None)
    ap.add_argument("--rig", default=None)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("run", help="run the rig")
    p.add_argument("--driver", help="override dmx.driver (serial, ftdi, null)")
    p.add_argument("--no-audio", action="store_true", help="skip audio capture")
    p.add_argument("--audio-file", metavar="PATH",
                   help="analyse this file instead of the loopback, silently and "
                        "looping -- for designing looks in /viz without speakers")
    p.add_argument("--no-jukebox", action="store_true", help="skip jukebox polling")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("audio-devices", help="list audio input devices")
    p.set_defaults(fn=cmd_audio_devices)

    p = sub.add_parser("rig", help="print the patch")
    p.set_defaults(fn=cmd_rig)

    p = sub.add_parser("cues", help="list available cues")
    p.set_defaults(fn=cmd_cues)

    p = sub.add_parser("streamdeck", help="write Stream Deck button scripts")
    p.add_argument("outdir")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", default="5055")
    p.set_defaults(fn=cmd_streamdeck)

    args = ap.parse_args(argv)
    setup_logging(args.log)
    if not hasattr(args, "fn"):
        ap.print_help()
        return 1
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
