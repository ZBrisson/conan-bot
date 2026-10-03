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


# ---------- restart routine ----------

def _fake_env(monkeypatch, tmp_path, kind_muted=None):
    """Patch Discord, RCON, container control and sleeps; return the event list."""
    import asyncio
    ev = []
    real_sleep = asyncio.sleep

    async def fsleep(s):
        ev.append(f"sleep {s:g}")
        await real_sleep(0)

    async def rcon(cmd):
        ev.append(f"rcon {cmd}")

    class Ch:
        async def send(self, m):
            ev.append("post " + m.splitlines()[0])

    class Ctl:
        async def stop(self):
            ev.append("stop")

        async def start(self):
            ev.append("start")

    async def up():
        return object()

    (tmp_path / "game_0.db").write_text("db")
    monkeypatch.setattr(bot.asyncio, "sleep", fsleep)
    monkeypatch.setattr(bot, "rcon_async", rcon)
    monkeypatch.setattr(bot, "control", Ctl())
    monkeypatch.setattr(bot, "query_info", up)
    monkeypatch.setattr(bot, "DB_FILE", tmp_path / "game_0.db")
    monkeypatch.setattr(bot, "BACKUP_DIR", tmp_path / "snaps")
    monkeypatch.setattr(bot, "notify", bot.NotifySettings(tmp_path / "n.json"))
    bot.client.get_channel = lambda _id: Ch()
    if kind_muted:
        bot.notify.set(kind_muted, False)
    return ev


def test_restart_order_and_snapshot(monkeypatch, tmp_path):
    import asyncio
    ev = _fake_env(monkeypatch, tmp_path)
    assert asyncio.run(bot.restart_routine("test", [15, 5, 1], "scheduled")) == "Restarted."
    order = [e for e in ev if not e.startswith("sleep 15")]
    assert order[:8] == ["post 🔄 Restart starting (test). Warnings: 15, 5, 1 min.",
                         "rcon broadcast Server restart in 15 minutes.", "sleep 600",
                         "rcon broadcast Server restart in 5 minutes.", "sleep 240",
                         "rcon broadcast Server restart in 1 minute.", "sleep 60", "stop"]
    assert "start" in ev and ev[-1].startswith("post ✅ Server back up")
    assert len(list((tmp_path / "snaps").glob("game_0-*.db"))) == 1
    assert bot.expected_down is False


def test_second_restart_refused_while_running(monkeypatch, tmp_path):
    import asyncio
    _fake_env(monkeypatch, tmp_path)

    async def both():
        first = asyncio.create_task(bot.restart_routine("a", [1], "manual"))
        await asyncio.sleep(0)
        second = await bot.restart_routine("b", [1], "manual")
        return await first, second
    assert asyncio.run(both()) == ("Restarted.", "A restart is already running.")


def test_failure_posts_even_when_category_muted(monkeypatch, tmp_path):
    import asyncio
    ev = _fake_env(monkeypatch, tmp_path, kind_muted="manual")

    class Broken:
        async def stop(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(bot, "control", Broken())
    assert asyncio.run(bot.restart_routine("x", [], "manual")).startswith("Restart failed")
    posts = [e for e in ev if e.startswith("post")]
    assert len(posts) == 1 and posts[0].startswith("post ❌ Restart failed: boom")
    assert bot.expected_down is False


# ---------- container control only ever touches TARGET_CONTAINER ----------

class _Resp:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.data


class _Session:
    def __init__(self, gql_data=None):
        self.calls = []
        self.gql_data = gql_data

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return _Resp({"State": {"Status": "running", "StartedAt": "2026-10-02T23:52:04Z"}})

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json))
        if json is not None:
            return _Resp(self.gql_data(json))
        return _Resp(None, 204)


def test_proxy_mode_only_targets_one_container(monkeypatch):
    import asyncio
    monkeypatch.setattr(bot, "CONTROL_MODE", "proxy")
    monkeypatch.setattr(bot, "TARGET_CONTAINER", "Conan_A")
    monkeypatch.setattr(bot, "PROXY_URL", "http://proxy:2375")
    s = _Session()
    c = bot.Control(s)

    async def run():
        assert await c.state() == ("RUNNING", "since 2026-10-02T23:52:04")
        await c.stop()
        await c.start()
    asyncio.run(run())
    assert [x[:2] for x in s.calls] == [("GET", "http://proxy:2375/containers/Conan_A/json"),
                                        ("POST", "http://proxy:2375/containers/Conan_A/stop?t=120"),
                                        ("POST", "http://proxy:2375/containers/Conan_A/start")]


def test_api_mode_uses_target_id_and_errors_when_missing(monkeypatch):
    import asyncio
    monkeypatch.setattr(bot, "CONTROL_MODE", "api")
    monkeypatch.setattr(bot, "TARGET_CONTAINER", "Conan_A")
    containers = [{"id": "id-other", "names": ["/Conan_B"], "state": "RUNNING", "status": "Up"},
                  {"id": "id-a", "names": ["/Conan_A"], "state": "RUNNING", "status": "Up 1 hour"}]
    mutations = []

    def gql(body):
        if body["query"].startswith("mutation"):
            mutations.append((body["query"].split("{")[2].split("(")[0].strip(), body["variables"]["id"]))
            return {"data": {"docker": {}}}
        return {"data": {"docker": {"containers": containers}}}

    c = bot.Control(_Session(gql))
    asyncio.run(c.stop())
    asyncio.run(c.start())
    assert mutations == [("stop", "id-a"), ("start", "id-a")]
    assert asyncio.run(c.state()) == ("RUNNING", "Up 1 hour")
    containers.pop()
    import pytest
    with pytest.raises(RuntimeError, match="Conan_A not found"):
        asyncio.run(c.stop())


# ---------- crash watcher ----------

def test_unexpected_stop_and_recovery_posts(monkeypatch, tmp_path):
    import asyncio
    ev = _fake_env(monkeypatch, tmp_path)
    states = iter(["RUNNING", "EXITED", "RUNNING"])

    class Ctl:
        async def state(self):
            return next(states), ""

    monkeypatch.setattr(bot, "control", Ctl())
    monkeypatch.setattr(bot, "expected_down", False)
    bot.watch_state.last = None
    for _ in range(3):
        asyncio.run(bot.watch_state.coro())
    posts = [e for e in ev if e.startswith("post")]
    assert posts[0].startswith("post ⚠️") and "stopped unexpectedly" in posts[0]
    assert posts[1].startswith("post ▶️") and len(posts) == 2


def test_bot_initiated_stop_is_not_a_crash(monkeypatch, tmp_path):
    import asyncio
    ev = _fake_env(monkeypatch, tmp_path)
    states = iter(["RUNNING", "EXITED"])

    class Ctl:
        async def state(self):
            return next(states), ""

    monkeypatch.setattr(bot, "control", Ctl())
    monkeypatch.setattr(bot, "expected_down", True)
    bot.watch_state.last = None
    asyncio.run(bot.watch_state.coro())
    asyncio.run(bot.watch_state.coro())
    assert not [e for e in ev if e.startswith("post")]


# ---------- .env parsing ----------

def test_load_env_quotes_and_comments(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('A_PLAIN=value   # note\nA_QUOTED="has # hash"\nA_EMPTY=   # nothing\n# A_COMMENT=x\nA_SINGLE=\'x y\'\n')
    for k in ("A_PLAIN", "A_QUOTED", "A_EMPTY", "A_COMMENT", "A_SINGLE"):
        monkeypatch.delenv(k, raising=False)
    bot.load_env(f)
    import os as _os
    assert (_os.environ["A_PLAIN"], _os.environ["A_QUOTED"], _os.environ["A_EMPTY"], _os.environ["A_SINGLE"]) == \
        ("value", "has # hash", "", "x y")
    assert "A_COMMENT" not in _os.environ
