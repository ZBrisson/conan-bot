"""Offline tests for log parsing and masking. Run: python -m pytest tests/"""
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ENV_FILE", "/nonexistent")
bot = importlib.import_module("bot")


def write_log(tmp_path, lines):
    p = tmp_path / "ConanSandbox.log"
    p.write_text("\n".join(lines) + "\n")
    bot.LOG_FILE = p
    return p


def test_build_found_past_line_400(tmp_path):
    filler = [f"LogPakFile: Display: line {i}" for i in range(435)]
    write_log(tmp_path, filler + ["LogInit: Build: ++exiles+release-beta-CL-378132", "LogInit: Engine Version: 5.8.2"])
    assert bot.log_build() == "++exiles+release-beta-CL-378132"


def test_build_unknown_when_missing(tmp_path):
    write_log(tmp_path, ["nothing here"] * 10)
    assert bot.log_build() == "unknown"


def test_build_unknown_when_log_missing(tmp_path):
    bot.LOG_FILE = tmp_path / "missing.log"
    assert bot.log_build() == "unknown"


def test_event_regexes():
    assert bot.JOIN.search("LogNet: Join succeeded: Someone#1234").group(1) == "Someone"
    assert bot.LEAVE.search("LogNet: Player disconnected: Someone#1234").group(1) == "Someone"
    assert bot.MODFAIL.search("LogNet: PreLogin failure: ServerHasNoMods (32)").group(1) == "ServerHasNoMods"
    assert bot.STATS.search("Players=1 FPS=8.15:33.41:41.4").groups() == ("1", "8.15", "33.41")


def test_masking(tmp_path):
    write_log(tmp_path, ["from 203.0.113.7:52613 STEAM:76561190000000000 id 0123456789abcdef0123456789abcdef"])
    out = bot.masked_tail()
    assert "203.0.113.7" not in out and "7656119" not in out and "<ip>" in out and "<steamid>" in out


def test_player_names():
    out = "Idx | Char name | Player name | User ID | Platform ID | Platform Name\n  0 | Conan | Someone | x | y | Steam\n"
    assert bot.player_names(out) == ["Conan"]
