"""Run the listening pass silently, then draft design cards for what it mapped.

    ./.venv/bin/python tools/silent_pass.py                 # carry on, most popular first
    ./.venv/bin/python tools/silent_pass.py --hours 7       # stop after about 7 hours
    ./.venv/bin/python tools/silent_pass.py --playlist "<link>"   # first run only

Switches the Mac's sound output to BlackHole 2ch (so nothing comes out of the
speakers), runs `cli listen --popular-first`, and ALWAYS switches the output
back to what it was when it ends, however it ends (done, Ctrl+C, killed, or an
error). Then runs tools/design_cards.py, so new maps get a draft card.

Needs SwitchAudioSource (brew install switchaudio-osx). The jukebox's queue
must be empty; `listen` checks.
"""
import argparse
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")
SILENT = "BlackHole 2ch"
#: The pass's own estimate per song: its length plus about 8 s of overhead.
AVG_SONG_S = 3.6 * 60 + 8


def output(name: str | None = None) -> str:
    if name is None:
        return subprocess.run(["SwitchAudioSource", "-c", "-t", "output"],
                              capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["SwitchAudioSource", "-t", "output", "-s", name],
                   capture_output=True, text=True, check=True)
    return name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--playlist", help="Spotify playlist link (only needed the first time)")
    ap.add_argument("--hours", type=float, help="stop after about this long")
    ap.add_argument("--limit", type=int, help="map at most this many songs")
    args = ap.parse_args()
    if not shutil.which("SwitchAudioSource"):
        print("SwitchAudioSource is missing: brew install switchaudio-osx")
        return 1

    limit = args.limit
    if args.hours:
        limit = min(limit or 10**6, max(1, int(args.hours * 3600 // AVG_SONG_S)))
    cmd = [PY, "-m", "partylights.cli", "listen", "--popular-first"]
    if args.playlist:
        cmd.insert(4, args.playlist)
    if limit:
        cmd += ["--limit", str(limit)]

    before = output()
    print(f"Sound output: {before} -> {SILENT} for the pass (it goes back afterwards).")
    child = None

    def restore(*_):
        if child and child.poll() is None:
            child.send_signal(signal.SIGINT)       # listen pauses Spotify on Ctrl+C
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
        output(before)
        print(f"\nSound output back to {before}.")

    signal.signal(signal.SIGTERM, lambda *a: (restore(), sys.exit(143)))
    try:
        output(SILENT)
        time.sleep(1)
        child = subprocess.Popen(cmd, cwd=ROOT)
        code = child.wait()
    except KeyboardInterrupt:
        code = 130
    finally:
        restore()
    subprocess.call([PY, "tools/design_cards.py"], cwd=ROOT)
    return code


if __name__ == "__main__":
    sys.exit(main())
