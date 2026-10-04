#!/usr/bin/env python3
"""Phase 0: prove the DMX output path, then verify the channel maps.

Nothing else in this project can be trusted until this works, because every
later layer assumes a frame reaches the fixtures and that the profile channel
maps match the real hardware.

Run with no arguments for the guided sequence. Useful individually:

    python tools/dmx_sweep.py flood            # everything to full, is anything alive?
    python tools/dmx_sweep.py sweep            # walk every patched channel, one at a time
    python tools/dmx_sweep.py sweep --from 1 --to 7
    python tools/dmx_sweep.py channel 3 200    # hold one raw channel at one value
    python tools/dmx_sweep.py fixture par1     # exercise one fixture via its profile
    python tools/dmx_sweep.py identify         # light fixtures one at a time, named
    python tools/dmx_sweep.py address          # help set addresses: holds ch1 only
    python tools/dmx_sweep.py off

What to watch for during `sweep`: the channel number printed should match what
the fixture does. If offset 5 of a PAR (channel 6 for par1) makes it start
cycling colours on its own, that confirms the mode-channel behaviour the profile
pins to zero.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from partylights.config import Settings, build_universe, load_patch, setup_logging
from partylights.fixtures.color import Emission

log = logging.getLogger("sweep")


def _hold(seconds: float) -> None:
    """Sleep, but let ctrl-c out promptly."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(0.02)


def cmd_flood(universe, patch, args):
    """Everything to full. The crudest possible 'is the link alive?' test."""
    print(f"All {patch.max_channel} patched channels to full for {args.seconds}s.")
    print("Expect: every fixture on, probably white-ish. Nothing? The link is the problem,")
    print("not the profiles — check cable direction, the fixture's DMX mode, and addresses.")
    for ch in range(1, patch.max_channel + 1):
        universe.set(ch, 255)
    _hold(args.seconds)
    universe.clear()


def cmd_sweep(universe, patch, args):
    """Walk one channel at a time so you can map what each one actually does."""
    lo = args.from_ch or 1
    hi = args.to_ch or patch.max_channel
    owner = {}
    for f in patch:
        for i, ch in enumerate(f.channels()):
            spec = next((c for c in f.profile.channels if c.offset == i), None)
            owner[ch] = (f.fid, spec.role if spec else "?", spec.note if spec else "")

    print(f"Sweeping channels {lo}..{hi} at {args.value}, {args.dwell}s each. Ctrl-C to stop.")
    print("Compare what you see against the 'expected' column.\n")
    for ch in range(lo, hi + 1):
        universe.clear()
        universe.set(ch, args.value)
        fid, role, note = owner.get(ch, ("unpatched", "-", ""))
        suffix = f"  <- {note}" if note else ""
        print(f"  ch {ch:3d}  expected: {fid:>8s} {role:<8s}{suffix}")
        _hold(args.dwell)
    universe.clear()


def cmd_channel(universe, patch, args):
    """Hold a single raw channel. For poking at something by hand."""
    print(f"Channel {args.channel} = {args.value}, holding for {args.seconds}s.")
    universe.clear()
    universe.set(args.channel, args.value)
    _hold(args.seconds)
    universe.clear()


def cmd_fixture(universe, patch, args):
    """Exercise one fixture through its profile: dimmer, each emitter, strobe.

    This is the test that validates the profile rather than the wiring. If the
    colours come out named correctly here, the channel map is right.
    """
    f = patch.by_id.get(args.fid)
    if f is None:
        print(f"No fixture {args.fid!r}. Known: {', '.join(sorted(patch.by_id))}")
        return
    print(f"Exercising {f.describe()}\n")

    steps: list[tuple[str, Emission]] = [
        ("dimmer fade up (white)", None),  # handled specially below
        ("red", Emission(rgb=(1, 0, 0))),
        ("green", Emission(rgb=(0, 1, 0))),
        ("blue", Emission(rgb=(0, 0, 1))),
    ]
    if f.profile.has("white") or f.profile.has("amber"):
        steps.append(("warm white (should use the white/amber emitters)",
                      Emission(rgb=(1.0, 0.85, 0.7))))
    if f.profile.has_uv:
        steps.append(("UV only (looks dim but makes white things glow)",
                      Emission(rgb=(0, 0, 0), uv=1.0)))
    if f.profile.has_strobe:
        steps.append(("strobe, mid rate", Emission(rgb=(1, 1, 1), strobe=0.4)))

    for name, em in steps:
        print(f"  {name}")
        if em is None:
            for pct in range(0, 101, 4):
                f.render_into(universe, Emission(rgb=(1, 1, 1), intensity=pct / 100))
                time.sleep(0.03)
            _hold(0.4)
        else:
            f.render_into(universe, em)
            _hold(args.dwell)
    universe.clear()
    print("\nIf every label matched what you saw, this profile is correct.")


def cmd_identify(universe, patch, args):
    """Light each fixture in turn, by name, in physical order.

    Use this after addressing to confirm par1..par12 really are in the order you
    listed them in rig.yaml — which is what makes a chase look like movement.
    """
    print("Lighting each fixture in turn, in rig.yaml order.")
    print("They should illuminate in physical sequence. If they jump around, reorder rig.yaml.\n")
    for f in patch.ordered():
        universe.clear()
        print(f"  {f.position + 1:2d}. {f.fid:>8s}  @{f.address:3d}  {f.label}")
        f.render_into(universe, Emission(rgb=(1, 1, 1), intensity=1.0))
        _hold(args.dwell)
    universe.clear()


def cmd_address(universe, patch, args):
    """Hold channel 1 at full, which is the addressing aid.

    Fixtures ship on address 1, so every unaddressed fixture lights up. Walk the
    room setting each fixture's address to its target from rig.yaml; as you set
    each one correctly it goes dark, and when the room is dark you are done.
    """
    print("Channel 1 held at full.\n")
    print("Every fixture still on address 1 is lit. Set each fixture's address to its")
    print("target below; it goes dark as soon as it moves off address 1.")
    print("When the whole room is dark, addressing is complete.\n")
    for f in patch.ordered():
        print(f"  {f.fid:>8s} -> address {f.address:3d}   ({f.footprint}ch, {f.label})")
    print(f"\nHolding for {args.seconds}s. Ctrl-C when done.")
    universe.clear()
    universe.set(1, 255)
    _hold(args.seconds)
    universe.clear()


def cmd_off(universe, patch, args):
    print("All channels to zero.")
    universe.clear()
    _hold(1.0)


def cmd_guided(universe, patch, args):
    """The default: the sequence to run the first time hardware is plugged in."""
    print("=" * 72)
    print("Guided check. Ctrl-C at any point.")
    print("=" * 72)
    print(f"\nPatch:\n{patch.summary()}\n")
    print(f"Output: {universe.driver.describe()}")
    print(f"Frame:  {universe.slot_count} slots at {universe.refresh_hz} Hz\n")

    input("1/3 — FLOOD: everything to full. Press enter...")
    cmd_flood(universe, patch, argparse.Namespace(seconds=4.0))

    input("\n2/3 — IDENTIFY: each fixture in turn, by name. Press enter...")
    cmd_identify(universe, patch, argparse.Namespace(dwell=1.5))

    first = patch.ordered()[0].fid if len(patch) else None
    if first:
        input(f"\n3/3 — PROFILE: exercise {first} through its channel map. Press enter...")
        cmd_fixture(universe, patch, argparse.Namespace(fid=first, dwell=1.5))

    print("\nDone. Stats:", universe.stats())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--driver", help="override dmx.driver (serial, ftdi, null)")
    ap.add_argument("--log", default="INFO")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("flood", help="all patched channels to full")
    p.add_argument("--seconds", type=float, default=5.0)
    p.set_defaults(fn=cmd_flood)

    p = sub.add_parser("sweep", help="walk channels one at a time")
    p.add_argument("--from", dest="from_ch", type=int)
    p.add_argument("--to", dest="to_ch", type=int)
    p.add_argument("--value", type=int, default=255)
    p.add_argument("--dwell", type=float, default=1.2)
    p.set_defaults(fn=cmd_sweep)

    p = sub.add_parser("channel", help="hold one raw channel")
    p.add_argument("channel", type=int)
    p.add_argument("value", type=int, nargs="?", default=255)
    p.add_argument("--seconds", type=float, default=10.0)
    p.set_defaults(fn=cmd_channel)

    p = sub.add_parser("fixture", help="exercise one fixture via its profile")
    p.add_argument("fid")
    p.add_argument("--dwell", type=float, default=1.5)
    p.set_defaults(fn=cmd_fixture)

    p = sub.add_parser("identify", help="light each fixture in turn, by name")
    p.add_argument("--dwell", type=float, default=1.5)
    p.set_defaults(fn=cmd_identify)

    p = sub.add_parser("address", help="addressing aid: hold channel 1")
    p.add_argument("--seconds", type=float, default=600.0)
    p.set_defaults(fn=cmd_address)

    p = sub.add_parser("off", help="all channels to zero")
    p.set_defaults(fn=cmd_off)

    args = ap.parse_args()
    setup_logging(args.log)

    settings = Settings.load()
    patch = load_patch()
    universe = build_universe(settings, patch, driver_override=args.driver)

    try:
        universe.driver.open()
    except Exception as e:
        print(f"\nCould not open the DMX output:\n  {e}\n")
        print("Try --driver null to exercise everything without hardware.")
        return 1

    universe.start()
    try:
        fn = getattr(args, "fn", cmd_guided)
        fn(universe, patch, args)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        universe.stop(blackout=True)
        universe.driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
