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


def test_schedule_starts_before_restart_time():
    tz = bot.TZ
    assert bot.warning_start(bot.dt.time(5, 0, tzinfo=tz), [15, 5, 1]) == bot.dt.time(4, 45, tzinfo=tz)
    assert bot.warning_start(bot.dt.time(0, 5, tzinfo=tz), [15, 5, 1]) == bot.dt.time(23, 50, tzinfo=tz)
    assert bot.warning_start(bot.dt.time(5, 0, tzinfo=tz), []) == bot.dt.time(5, 0, tzinfo=tz)


def test_notify_defaults_save_reload_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_PERFORMANCE", "off")
    f = tmp_path / "data" / "notify.json"
    n = bot.NotifySettings(f)
    assert n.enabled("players") and not n.enabled("performance")
    assert n.set("players", False) == ["players"]
    assert bot.NotifySettings(f).enabled("players") is False      # persisted
    n.set("all", True)
    assert all(bot.NotifySettings(f).enabled(c) for c in bot.NOTIFY_CATEGORIES)
    n.reset()
    again = bot.NotifySettings(f)
    assert again.enabled("players") and not again.enabled("performance")  # back to .env
    import pytest
    with pytest.raises(KeyError):
        n.set("nope", True)


def test_notify_unwritable_keeps_change_in_memory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    n = bot.NotifySettings(blocker / "notify.json")   # parent is a file -> can't save
    n.set("mods", False)
    assert n.enabled("mods") is False and n.persistent is False


def test_post_respects_mutes(tmp_path):
    import asyncio
    sent = []

    class Ch:
        async def send(self, m):
            sent.append(m)

    bot.notify = bot.NotifySettings(tmp_path / "n.json")
    bot.client.get_channel = lambda _id: Ch()
    bot.notify.set("players", False)
    asyncio.run(bot.post("➕ x joined", "players"))
    asyncio.run(bot.post("✅ up", "scheduled"))
    asyncio.run(bot.post("❌ failed"))
    assert sent == ["✅ up", "❌ failed"]
