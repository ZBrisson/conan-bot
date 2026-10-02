<img src="icon.png" width="96" align="right" alt="">

# conan-bot

A Discord bot for running a **Conan Exiles Enhanced** dedicated server in Docker on **Unraid**. Trusted members can check on the server and restart it from Discord. The bot also runs a daily restart with in-game warnings and posts what happens on the server to a channel.

## Features

- **Slash commands**
  - `/conan status`: container state, query-port health, player count, latest server tick rate, game build
  - `/conan players`: who is online (character names only)
  - `/conan restart`: restart with a 1-minute in-game warning. Limited to one Discord role, and needs a confirm button.
  - `/conan notify show | set | reset`: turn notification categories on or off from Discord. Anyone can view; changing needs the admin role.
- **Scheduled restart** (default 05:00 daily; the server goes down at that time and the warnings start earlier):
  1. Broadcasts in-game warnings over RCON (default 15, 5 and 1 minutes before).
  2. Stops the server and snapshots `game_0.db` (keeps the newest N).
  3. Starts the server and waits until the Steam query port answers. If the server image updates on start (e.g. steamcmd-based images), each restart also picks up game and mod updates.
  4. Posts the result. On failure it includes a log excerpt with IPs and IDs masked.
- **Event feed** to one channel:
  - server up/down, and unexpected stops
  - player joined/left (names only, never IPs or Steam IDs)
  - mod-mismatch login rejections (e.g. `ServerHasNoMods`)
  - low tick-rate alerts (minimum server FPS below a threshold for 3 reports in a row while players are on)
- **Notification settings**: each category can be on or off (see below).
- **Least privilege**
  - It can start/stop exactly one container (`TARGET_CONTAINER`).
  - It never needs the Docker socket.
  - It runs as `99:100` (Unraid's `nobody:users`) and mounts the game files read-only.

## Requirements

- An Unraid server (7.2 or newer for the built-in API) running a Conan Exiles dedicated server container.
- RCON enabled on the game server and reachable from the bot (LAN only; don't forward it to the internet). In `ConanSandbox/Saved/Config/<Platform>Server/Game.ini` (edit it only while the server is stopped; the server rewrites its config on shutdown and drops unknown changes):
  ```ini
  [RconPlugin]
  RconEnabled=1
  RconPassword=<strong password>
  RconPort=25575
  ```
  The RCON port must also be published by the game server container.
- One of these for container control:
  - **`CONTROL_MODE=api`** (recommended): an Unraid API key that can read and update Docker containers and nothing else.
    ```bash
    unraid-api apikey --create --name "conan bot" --roles "" --permissions "DOCKER:READ_ANY,DOCKER:UPDATE_ANY"
    ```
    Key names allow only letters, numbers and spaces. `--roles ""` is required when using `--permissions` alone (otherwise: "Invalid data structure").
  - **`CONTROL_MODE=proxy`**: a [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) that only allows container inspect/start/stop (`CONTAINERS=1 POST=1 ALLOW_START=1 ALLOW_STOP=1`, everything else off).

## Discord setup

1. [Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. To keep the bot private: **Installation** → Install Link **None**, then **Bot** → turn off **Public Bot**.
3. **Bot** → **Reset Token** → copy it into your `.env`. No privileged intents are needed.
4. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`.
   - Bot permissions: **View Channels**, **Send Messages**, **Embed Links**.
   - Open the generated URL and add the bot to your server.
5. Turn on Developer Mode (User Settings → Advanced). Right-click your server → **Copy Server ID** (`GUILD_ID`), and right-click the feed channel → **Copy Channel ID** (`CHANNEL_ID`).
6. Create the role named in `ADMIN_ROLE` (default `Conan Admin`) for people allowed to restart.

## Installation (Unraid)

1. Create a config directory and settings file:
   ```bash
   mkdir -p /mnt/user/appdata/conan-bot/snapshots /mnt/user/appdata/conan-bot/data
   curl -fsSLo /mnt/user/appdata/conan-bot/.env \
     https://raw.githubusercontent.com/ZBrisson/conan-bot/main/.env.example
   nano /mnt/user/appdata/conan-bot/.env
   chown -R 99:100 /mnt/user/appdata/conan-bot && chmod 600 /mnt/user/appdata/conan-bot/.env
   ```
2. Install the template and create the container:
   ```bash
   curl -fsSLo /boot/config/plugins/dockerMan/templates-user/my-conan-bot.xml \
     https://raw.githubusercontent.com/ZBrisson/conan-bot/main/my-conan-bot.xml
   ```
   Then **Docker → Add Container → Template: conan-bot**. Set **Conan server files** to your game server's appdata directory (the one containing `ConanSandbox/`).

   Or, without the template:
   ```bash
   docker run -d --name conan-bot --restart unless-stopped \
     -v /mnt/user/appdata/conan-bot:/config:ro \
     -v /mnt/user/appdata/<your-conan-appdata>:/conan:ro \
     -v /mnt/user/appdata/conan-bot/data:/data \
     -v /mnt/user/appdata/conan-bot/snapshots:/backups \
     ghcr.io/zbrisson/conan-bot:latest
   ```
3. Check `docker logs conan-bot` for `ready as <bot>; daily restart at 05:00 (warnings from 04:45)`. The slash commands appear in your server within a minute.

## Configuration

Settings come from environment variables or `/config/.env`. See [`.env.example`](.env.example).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | | Bot token |
| `GUILD_ID` / `CHANNEL_ID` | yes | | Server for slash commands; channel for the event feed |
| `ADMIN_ROLE` | | `Conan Admin` | Role allowed to use `/conan restart` |
| `TARGET_CONTAINER` | yes | | The only container the bot may start/stop |
| `SERVER_HOST` | yes | | Host where the query and RCON ports listen |
| `QUERY_PORT` / `RCON_PORT` | | `27015` / `25575` | |
| `RCON_PASSWORD` | yes | | |
| `CONTROL_MODE` | | `api` | `api` or `proxy` |
| `UNRAID_URL` / `UNRAID_API_KEY` | api mode | | `https://<unraid-host>/graphql` and the scoped key |
| `PROXY_URL` | proxy mode | `http://docker-socket-proxy:2375` | |
| `TZ_NAME` / `RESTART_TIME` | | `America/New_York` / `05:00` | When the server goes down; warnings begin `max(WARN_MINUTES)` earlier |
| `WARN_MINUTES` | | `15,5,1` | In-game warnings before the scheduled restart |
| `KEEP_SNAPSHOTS` | | `14` | Database snapshots kept in `/backups` |
| `FPS_ALERT_MIN` | | `10` | Tick-rate alert threshold |
| `NOTIFY_PLAYERS` … `NOTIFY_MANUAL` | | `on` | Notification defaults (see [Notifications](#notifications)) |
| `DATA_DIR` | | `/data` | Where Discord-made settings are saved |
| `LOG_FILE` / `DB_FILE` | | `/conan/ConanSandbox/Saved/...` | Override if your layout differs |

### Volumes

| Container path | Mode | Contents |
|---|---|---|
| `/config` | ro | `.env` |
| `/conan` | ro | Game server directory (log tailing, DB snapshot source) |
| `/data` | rw | `notify.json` (settings changed from Discord) |
| `/backups` | rw | `game_0-<timestamp>.db` snapshots |

## Notifications

| Category | Posts |
|---|---|
| `players` | ➕ joined / ➖ left |
| `performance` | 🐢 low tick-rate alerts |
| `mods` | 🧩 mod-mismatch login rejections |
| `crashes` | ⚠️ unexpected stop / ▶️ running again |
| `scheduled` | 🔄 / ✅ for the daily restart |
| `manual` | 🔄 / ✅ for `/conan restart` |

- **Defaults** come from `NOTIFY_<CATEGORY>=on|off` (all on if unset).
- **`/conan notify set <category|all> <on|off>`** overrides a default and saves it to `/data/notify.json`, so it survives restarts. `/conan notify reset` goes back to the `.env` defaults.
- **Always posted:** ❌ restart failures can't be muted, and every change made with `/conan notify` is announced in the channel.

## Security notes

- Keep `.env` at `chmod 600`. It holds the bot token, RCON password and API key.
- Don't forward the RCON port to the internet.
- The bot never posts IP addresses or Steam IDs. Log excerpts are masked before posting.
- Unraid's API uses a self-signed certificate on the LAN, so the bot doesn't verify TLS for `UNRAID_URL`. Keep that URL on a trusted network.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in, then:
ENV_FILE=.env python bot.py

python -m pytest -q tests/
docker build -t conan-bot .
```
Pushes to `main` build and publish `ghcr.io/zbrisson/conan-bot` (`latest` and the short commit SHA). Tags `v*` add semver tags.
