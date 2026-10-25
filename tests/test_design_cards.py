"""Generated design cards load, play, and never overwrite a hand-made card."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import design_cards  # noqa: E402
from partylights.config import DEFAULT_RIG, load_patch  # noqa: E402
from partylights.engine import palette as palettes  # noqa: E402
from partylights.engine.looks import BY_NAME  # noqa: E402
from partylights.engine.show import Card, ShowRunner, song_seconds  # noqa: E402

THE_DAYS = "2FAZskT9yRjp2Oow9szJD8"


@pytest.fixture
def songs(tmp_path):
    (tmp_path / "maps").mkdir()
    (tmp_path / "designs").mkdir()
    shutil.copy(ROOT / "songs" / "maps" / f"{THE_DAYS}.json", tmp_path / "maps")
    return tmp_path


def run(songs, *extra):
    return subprocess.run([sys.executable, str(ROOT / "tools" / "design_cards.py"),
                           "--songs", str(songs), *extra],
                          capture_output=True, text=True, check=True).stdout


def test_a_draft_loads_and_follows_the_map(songs):
    run(songs)
    card = Card.load(THE_DAYS, songs)
    assert card.palette is None                     # wears the rig's palette
    assert all(s.look in BY_NAME and not getattr(BY_NAME[s.look], "manual_only", False)
               for s in card.sections)
    song_map = json.loads((songs / "maps" / f"{THE_DAYS}.json").read_text())
    drops = [d["time"] for d in song_map["moments"]["drops"]]
    flashes = [c.at for c in card.cues if c.do == "flash"]
    assert all(any(abs(f - d) < 0.01 for f in flashes) for d in drops)
    gaps = [(c.at, c.until) for c in card.cues if c.do == "blackout"]
    for g in song_map["moments"]["gaps"]:
        assert any(abs(a - g["start"]) < 0.01 and abs(u - g["end"]) < 0.01 for a, u in gaps)
    assert sum(c.do == "strobe" for c in card.cues) == 1


def test_a_draft_plays_in_the_rigs_palette(songs):
    run(songs)
    runner = ShowRunner(load_patch(DEFAULT_RIG), root=songs)
    runner.start(THE_DAYS, 60.0)
    look, pal, level = runner.plan(0.5, palettes.get("witch"))
    assert pal.name == "witch" and look in BY_NAME


def test_hand_made_cards_are_never_touched(songs):
    hand = songs / "designs" / f"{THE_DAYS}.yaml"
    hand.write_text("schema: 1\nsections: []\n")
    run(songs, "--refresh")
    assert hand.read_text() == "schema: 1\nsections: []\n"


def test_refresh_redoes_drafts_only(songs):
    run(songs)
    card = songs / "designs" / f"{THE_DAYS}.yaml"
    card.write_text(card.read_text().replace("vibe:", "# edited\nvibe:"))
    assert "kept" not in run(songs)  # no-op without --refresh
    assert "# edited" in card.read_text()
    run(songs, "--refresh")
    assert "# edited" not in card.read_text()


def test_untrusted_beat_grid_gets_no_beat_timed_cues(songs):
    m = songs / "maps" / f"{THE_DAYS}.json"
    data = json.loads(m.read_text())
    data["quality"]["score"] = 0.1
    m.write_text(json.dumps(data))
    run(songs)
    card = Card.load(THE_DAYS, songs)
    assert {c.do for c in card.cues} <= {"blackout", "flash", "fade"}
    assert not any(getattr(BY_NAME[s.look], "needs_tempo", False) for s in card.sections)
