"""conan-bot: Discord control and event feed for the ConanExiles_jons server on Unraid.

- /conan status | players | restart  (restart is limited to ADMIN_ROLE)
- Daily restart at RESTART_TIME (America/New_York) with in-game RCON warnings
- Event feed: server up/down, unexpected exits, joins/leaves (names only),
  mod-mismatch login failures, low tick-rate alerts

Restart rights come from a narrowly scoped Unraid API key (CONTROL_MODE=api), or
from a docker-socket-proxy that only allows start/stop (CONTROL_MODE=proxy).
Only TARGET_CONTAINER can ever be started or stopped.
"""
import asyncio
import datetime as dt
import logging
import os
import re
import shutil
import ssl
from pathlib import Path
from zoneinfo import ZoneInfo

import a2s
import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from rcon.source import Client as RconClient

log = logging.getLogger("conan-bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def load_env(path=os.environ.get("ENV_FILE", "/config/.env")):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
E = os.environ.get

DISCORD_TOKEN = E("DISCORD_TOKEN")
GUILD_ID = int(E("GUILD_ID", "0"))
CHANNEL_ID = int(E("CHANNEL_ID", "0"))
ADMIN_ROLE = E("ADMIN_ROLE", "Conan Admin")

TARGET_CONTAINER = "ConanExiles_jons"  # hard-coded allowlist: never anything else
CONTROL_MODE = E("CONTROL_MODE", "api")  # api | proxy
UNRAID_URL = E("UNRAID_URL", "https://192.168.10.10/graphql")
UNRAID_API_KEY = E("UNRAID_API_KEY", "")
PROXY_URL = E("PROXY_URL", "http://docker-socket-proxy:2375")

SERVER_HOST = E("SERVER_HOST", "192.168.10.10")
QUERY_PORT = int(E("QUERY_PORT", "27015"))
RCON_PORT = int(E("RCON_PORT", "25575"))
RCON_PASSWORD = E("RCON_PASSWORD", "")

LOG_FILE = Path(E("LOG_FILE", "/conan/ConanSandbox/Saved/Logs/ConanSandbox.log"))
DB_FILE = Path(E("DB_FILE", "/conan/ConanSandbox/Saved/game_0.db"))
BACKUP_DIR = Path(E("BACKUP_DIR", "/backups"))
KEEP_SNAPSHOTS = int(E("KEEP_SNAPSHOTS", "14"))

TZ = ZoneInfo(E("TZ_NAME", "America/New_York"))
RESTART_TIME = dt.time(*map(int, E("RESTART_TIME", "05:00").split(":")), tzinfo=TZ)
WARN_MINUTES = [int(x) for x in E("WARN_MINUTES", "15,5,1").split(",")]
STOP_TIMEOUT = 120
START_TIMEOUT = 600
FPS_ALERT_MIN = float(E("FPS_ALERT_MIN", "10"))

restart_lock = asyncio.Lock()
expected_down = False  # set while the bot itself has the server stopped


# ---------- container control (allowlisted) ----------

class Control:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
        self.ssl = ssl.create_default_context()
        self.ssl.check_hostname = False
        self.ssl.verify_mode = ssl.CERT_NONE  # Unraid's self-signed cert, LAN only

    async def _gql(self, query, variables=None):
        async with self.s.post(UNRAID_URL, json={"query": query, "variables": variables or {}},
                               headers={"x-api-key": UNRAID_API_KEY}, ssl=self.ssl, timeout=30) as r:
            data = await r.json()
        if data.get("errors"):
            raise RuntimeError(data["errors"][0].get("message"))
        return data["data"]

    async def _container(self):
        d = await self._gql("{ docker { containers { id names state status } } }")
        for c in d["docker"]["containers"]:
            if f"/{TARGET_CONTAINER}" in c["names"]:
                return c
        raise RuntimeError(f"{TARGET_CONTAINER} not found")

    async def state(self):
        """Returns (state, status), e.g. ('RUNNING', 'Up 3 hours')."""
        if CONTROL_MODE == "proxy":
            async with self.s.get(f"{PROXY_URL}/containers/{TARGET_CONTAINER}/json", timeout=15) as r:
                j = await r.json()
            st = j["State"]
            return st["Status"].upper(), f"since {st.get('StartedAt', '?')[:19]}"
        c = await self._container()
        return c["state"], c["status"]

    async def _act(self, action):
        assert action in ("start", "stop")
        if CONTROL_MODE == "proxy":
            q = f"?t={STOP_TIMEOUT}" if action == "stop" else ""
            async with self.s.post(f"{PROXY_URL}/containers/{TARGET_CONTAINER}/{action}{q}",
                                   timeout=STOP_TIMEOUT + 30) as r:
                if r.status not in (204, 304):
                    raise RuntimeError(f"proxy {action}: HTTP {r.status}")
            return
        cid = (await self._container())["id"]
        await self._gql(f"mutation($id: PrefixedID!) {{ docker {{ {action}(id: $id) {{ id state }} }} }}",
                        {"id": cid})

    async def start(self):
        await self._act("start")

    async def stop(self):
        await self._act("stop")


# ---------- server helpers ----------

async def query_info():
    try:
        return await a2s.ainfo((SERVER_HOST, QUERY_PORT), timeout=3)
    except Exception:
        return None


def rcon(cmd):
    with RconClient(SERVER_HOST, RCON_PORT, passwd=RCON_PASSWORD, timeout=10) as c:
        return c.run(cmd)


async def rcon_async(cmd):
    try:
        return await asyncio.to_thread(rcon, cmd)
    except Exception as e:
        log.warning("rcon %r failed: %s", cmd, e)
        return None


def player_names(listplayers_output):
    """Conan `listplayers` is a | separated table; keep only the char name column."""
    names = []
    for line in (listplayers_output or "").splitlines()[1:]:
        cols = [c.strip() for c in line.split("|")]
        if len(cols) > 1 and cols[1]:
            names.append(cols[1])
    return names


def log_build():
    try:
        with LOG_FILE.open(errors="replace") as f:
            for _, line in zip(range(400), f):
                m = re.search(r"LogInit: Build: (\S+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return "unknown"


def last_stats_line():
    try:
        with LOG_FILE.open("rb") as f:
            f.seek(max(0, LOG_FILE.stat().st_size - 200_000))
            tail = f.read().decode(errors="replace")
    except OSError:
        return None
    hits = re.findall(r"Players=(\d+) FPS=([\d.]+):([\d.]+):", tail)
    return hits[-1] if hits else None


def snapshot_db():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"game_0-{dt.datetime.now(TZ):%Y%m%d-%H%M%S}.db"
    shutil.copy2(DB_FILE, dest)
    snaps = sorted(BACKUP_DIR.glob("game_0-*.db"))
    for old in snaps[:-KEEP_SNAPSHOTS]:
        old.unlink()
    return dest


MASK = [(re.compile(r"\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b"), "<ip>"),
        (re.compile(r"STEAM:\d+|\b7656\d{13}\b"), "<steamid>"),
        (re.compile(r"\b[0-9a-f]{32}\b"), "<id>")]


def masked_tail(n=20):
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return "(log unreadable)"
    out = "\n".join(lines)
    for rx, rep in MASK:
        out = rx.sub(rep, out)
    return out[-1800:]


# ---------- bot ----------

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
guild_obj = discord.Object(id=GUILD_ID)
control: Control | None = None


async def post(msg):
    ch = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
    await ch.send(msg[:1990])


async def restart_routine(reason, warn_minutes):
    """Warn → stop → snapshot → start → wait for query port. One at a time."""
    global expected_down
    if restart_lock.locked():
        return "A restart is already running."
    async with restart_lock:
        t0 = dt.datetime.now(TZ)
        await post(f"🔄 Restart starting ({reason}). Warnings: {', '.join(map(str, warn_minutes)) + ' min' if warn_minutes else 'none'}.")
        schedule = sorted(warn_minutes, reverse=True)
        for i, m in enumerate(schedule):
            await rcon_async(f"broadcast Server restart in {m} minute{'s' if m != 1 else ''}.")
            nxt = schedule[i + 1] if i + 1 < len(schedule) else 0
            await asyncio.sleep((m - nxt) * 60)
        expected_down = True
        try:
            await control.stop()
            snap = await asyncio.to_thread(snapshot_db)
            await control.start()
            deadline = asyncio.get_running_loop().time() + START_TIMEOUT
            info = None
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(15)
                info = await query_info()
                if info:
                    break
        except Exception as e:
            await post(f"❌ Restart failed: {e}\n```\n{masked_tail()}\n```")
            return f"Restart failed: {e}"
        finally:
            expected_down = False
        mins = (dt.datetime.now(TZ) - t0).total_seconds() / 60
        if info:
            await post(f"✅ Server back up in {mins:.1f} min. Build `{log_build()}`. Snapshot `{snap.name}`.")
            return "Restarted."
        await post(f"❌ Server started but not answering on {QUERY_PORT} after {START_TIMEOUT // 60} min.\n"
                   f"```\n{masked_tail()}\n```")
        return "Started but not answering yet."


def is_admin(inter: discord.Interaction):
    return isinstance(inter.user, discord.Member) and any(r.name == ADMIN_ROLE for r in inter.user.roles)


conan = app_commands.Group(name="conan", description="Conan Exiles server")


@conan.command(name="status", description="Server state, players and tick rate")
async def status_cmd(inter: discord.Interaction):
    await inter.response.defer(thinking=True)
    state, status = await control.state()
    info = await query_info()
    stats = last_stats_line()
    lines = [f"**{TARGET_CONTAINER}**: {state} ({status})",
             f"Query port: {'answering' if info else 'no answer'}"]
    if info:
        lines.append(f"Players: {info.player_count}/{info.max_players} on *{info.server_name}*")
    if stats:
        lines.append(f"Last tick report: players={stats[0]} min={stats[1]} avg={stats[2]}")
    lines.append(f"Build: `{log_build()}`")
    await inter.followup.send("\n".join(lines))


@conan.command(name="players", description="Who is online (names only)")
async def players_cmd(inter: discord.Interaction):
    await inter.response.defer(thinking=True)
    out = await rcon_async("listplayers")
    if out is None:
        await inter.followup.send("RCON not reachable.")
        return
    names = player_names(out)
    await inter.followup.send(", ".join(names) if names else "Nobody online.")


class ConfirmRestart(discord.ui.View):
    def __init__(self, requester):
        super().__init__(timeout=60)
        self.requester = requester

    @discord.ui.button(label="Restart now (1 min warning)", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, _):
        if inter.user.id != self.requester:
            await inter.response.send_message("Only the requester can confirm.", ephemeral=True)
            return
        await inter.response.edit_message(content="Restart confirmed.", view=None)
        await restart_routine(f"requested by {inter.user.display_name}", [1])


@conan.command(name="restart", description="Restart the server (Conan Admin only)")
async def restart_cmd(inter: discord.Interaction):
    if not is_admin(inter):
        await inter.response.send_message(f"You need the **{ADMIN_ROLE}** role.", ephemeral=True)
        return
    if restart_lock.locked():
        await inter.response.send_message("A restart is already running.", ephemeral=True)
        return
    await inter.response.send_message("Restart the server? Players get a 1-minute warning.",
                                      view=ConfirmRestart(inter.user.id), ephemeral=True)


tree.add_command(conan, guild=guild_obj)


@tasks.loop(time=RESTART_TIME)
async def daily_restart():
    await restart_routine("daily schedule", WARN_MINUTES)


@tasks.loop(seconds=60)
async def watch_state():
    """Alert when the container stops without the bot stopping it."""
    st = watch_state.last
    try:
        state, _ = await control.state()
    except Exception as e:
        log.warning("state check failed: %s", e)
        return
    if st and st == "RUNNING" and state != "RUNNING" and not expected_down:
        await post(f"⚠️ {TARGET_CONTAINER} stopped unexpectedly (state {state}).\n```\n{masked_tail()}\n```")
    if st and st != "RUNNING" and state == "RUNNING" and not restart_lock.locked():
        await post(f"▶️ {TARGET_CONTAINER} is running again.")
    watch_state.last = state


watch_state.last = None

JOIN = re.compile(r"Join succeeded: (.+?)(?:#\d+)?\s*$")
LEAVE = re.compile(r"Player disconnected: (.+?)(?:#\d+)?\s*$")
MODFAIL = re.compile(r"PreLogin failure: (\w*Mod\w*)")
STATS = re.compile(r"Players=(\d+) FPS=([\d.]+):([\d.]+):")


async def follow_log():
    """Tail the server log, coping with the file being replaced on restart."""
    pos, inode, low = 0, None, 0
    while True:
        try:
            st = LOG_FILE.stat()
            if st.st_ino != inode or st.st_size < pos:
                first = inode is None
                inode, pos = st.st_ino, (st.st_size if first else 0)
            with LOG_FILE.open(errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except OSError:
            await asyncio.sleep(10)
            continue
        for line in chunk.splitlines():
            if m := JOIN.search(line):
                await post(f"➕ {m.group(1)} joined")
            elif (m := LEAVE.search(line)) and m.group(1) != "Unknown":
                await post(f"➖ {m.group(1)} left")
            elif m := MODFAIL.search(line):
                await post(f"🧩 A player was refused at login: `{m.group(1)}` (mod mismatch).")
            elif m := STATS.search(line):
                players, fps_min = int(m.group(1)), float(m.group(2))
                low = low + 1 if players > 0 and fps_min < FPS_ALERT_MIN else 0
                if low == 3:
                    await post(f"🐢 Tick rate min under {FPS_ALERT_MIN:g} for 3 reports in a row "
                               f"({players} player(s) on, latest min {fps_min}).")
        await asyncio.sleep(5)


@client.event
async def on_ready():
    global control
    if control is None:
        control = Control(aiohttp.ClientSession())
        await tree.sync(guild=guild_obj)
        daily_restart.start()
        watch_state.start()
        client.loop.create_task(follow_log())
        log.info("ready as %s; daily restart at %s", client.user, RESTART_TIME)


if __name__ == "__main__":
    missing = [k for k in ("DISCORD_TOKEN", "GUILD_ID", "CHANNEL_ID", "RCON_PASSWORD") if not E(k)]
    if CONTROL_MODE == "api" and not UNRAID_API_KEY:
        missing.append("UNRAID_API_KEY")
    if missing:
        raise SystemExit(f"missing settings: {', '.join(missing)}")
    client.run(DISCORD_TOKEN, log_handler=None)
