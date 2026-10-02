# conan-bot + ConanExiles_jons runbook

Run everything below as **root on the Unraid server** (Unraid 7.3.2). Each step: back up → look → change → verify.

> **Never start `ConanExiles` (the legacy container).** Its first start runs steamcmd and upgrades the pre-engine-upgrade world you want to merge later. It also shares the same ports.

## Part A: server fixes

### A1. Archive the legacy world (read-only on the source)
```bash
SRC=/mnt/cache/appdata/conanexiles/ConanSandbox/Saved/
DST=/mnt/user/backup/conan/legacy-pre-ue-$(date +%F)/
mkdir -p "$DST" && rsync -a "$SRC" "$DST"
du -sh "$SRC" "$DST"                        # sizes should match
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' ConanExiles   # expect "no"
```

### A2. Mods: download both, then load them
```bash
T=/boot/config/plugins/dockerMan/templates-user/my-ConanExiles_jons.xml
ls -la "$T" && cp "$T" "$T.bak-$(date +%F)"
grep -nE 'WS_CONTENT|VALIDATE' "$T"                                   # look first
sed -i 's#\(Target="VALIDATE"[^>]*>\)[^<]*<#\1true<#' "$T"            # VALIDATE=true
grep -n 'Target="VALIDATE"' "$T"
ls /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container \
  && /usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container ConanExiles_jons
# (no script → GUI: Docker → ConanExiles_jons → Edit → Apply)
```
Wait until both folders exist, then write `modlist.txt` with container paths:
```bash
J=/mnt/cache/appdata/conanexiles_jons
ls -d $J/steamapps/workshop/content/440900/{3718655125,3719513784}/
M=$J/ConanSandbox/Mods/modlist.txt; cp "$M" "$M.bak-$(date +%F)"
for id in 3718655125 3719513784; do
  find $J/steamapps/workshop/content/440900/$id -name '*.pak' | sed "s#^$J#/serverdata/serverfiles#"
done > "$M"
chown 99:100 "$M"; cat "$M"                                            # expect 2 lines
sed -i 's#\(Target="VALIDATE"[^>]*>\)[^<]*<#\1<#' "$T"                 # VALIDATE back to empty
/usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container ConanExiles_jons
```
Verify (from your workstation, while `openssh-server` is running):
`ssh unraid-ro 'grep -iE "Mounting|modlist|ServerHasNoMods" /appdata/conanexiles_jons/ConanSandbox/Saved/Logs/ConanSandbox.log | tail'`

**Mod compatibility (checked 2026-10-02):** both mods are by Xevyr, tagged *Enhanced* and updated 2026-09-15 (the day Update 2.2.0 shipped). The author says they rarely need updates when the game patches. **Recheck after "Legacy of the Giant-Kings: Part Two" (Oct 6):**
```bash
curl -s -d 'itemcount=2&publishedfileids[0]=3718655125&publishedfileids[1]=3719513784' \
  https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/ \
  | python3 -c "import json,sys,datetime as d;[print(f['title'],d.datetime.fromtimestamp(f['time_updated']).date()) for f in json.load(sys.stdin)['response']['publishedfiledetails']]"
```

### A3. RCON on the LAN only
```bash
C=/mnt/cache/appdata/conanexiles_jons/ConanSandbox/Saved/Config/LinuxServer
ls -la $C; test -f $C/Game.ini && cp $C/Game.ini $C/Game.ini.bak-$(date +%F)
PW=$(openssl rand -hex 16); echo "RCON password (put in conan-bot .env): $PW"
printf '[RconPlugin]\nRconEnabled=1\nRconPassword=%s\nRconPort=25575\nRconMaxKarma=60\n' "$PW" >> $C/Game.ini
chown 99:100 $C/Game.ini; grep -v Password $C/Game.ini
# publish 25575/tcp on the host (do NOT forward it on the router)
grep -q 'Target="25575"' "$T" || sed -i 's#</Container>#  <Config Name="RCON" Target="25575" Default="25575" Mode="tcp" Description="LAN only" Type="Port" Display="always" Required="false" Mask="false">25575</Config>\n</Container>#' "$T"
/usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container ConanExiles_jons
docker port ConanExiles_jons | grep 25575
```

### A4. Tidy restore leftovers (move, don't delete)
```bash
J=/mnt/cache/appdata/conanexiles_jons/ConanSandbox/Saved/Config
mkdir -p /mnt/user/backup/conan/restore-leftovers
mv $J/WindowsServer $J/LinuxServer.default-bak /mnt/user/backup/conan/restore-leftovers/
ls /mnt/user/backup/conan/restore-leftovers/
```

### A5. Backups: make sure appdata.backup doesn't collide with 05:00
```bash
grep -iE 'cron|schedule|exclude|conan' /boot/config/plugins/appdata.backup/config.json
```

### A6. Security clean-up
```bash
unraid-api apikey --help          # list/delete the VIEWER key "read only key", create a new one
```
On your workstation: `chmod 600` any local files that hold Unraid API keys or passwords.

## Part B: conan-bot

### B1. Discord (has to be done in the browser)
A channel **webhook URL is not enough**: slash commands and buttons need a bot. Collect four things: **bot token**, **application ID**, **server (guild) ID**, **channel ID**. The token goes only into `/mnt/user/appdata/conan-bot/.env`, never into chat or git.
1. https://discord.com/developers/applications → New Application → Bot → copy the token. No privileged intents needed.
2. OAuth2 URL Generator: scopes `bot` + `applications.commands`; permissions *Send Messages*, *Embed Links*. Invite it to your server.
3. Create a role **Conan Admin** and give it to the people who may restart.
4. Copy the server ID and the feed channel ID (Developer Mode → right-click → Copy ID).

### B2. Scoped Unraid API key
```bash
unraid-api apikey --help                                   # confirm flags on 7.3.2
unraid-api apikey --create --name conan-bot --description "restart ConanExiles_jons" \
  --permissions "DOCKER:READ_ANY,DOCKER:UPDATE_ANY"
```
If the API refuses container start/stop with that key, use the fallback: a `tecnativa/docker-socket-proxy` container on port 2375 (LAN), env `CONTAINERS=1 POST=1 ALLOW_START=1 ALLOW_STOP=1`, everything else off, and set `CONTROL_MODE=proxy`. **Never mount the raw Docker socket into the bot.**

### B3. Install (image `ghcr.io/zbrisson/conan-bot`, built by GitHub Actions)
The container runs as `99:100` (nobody:users) and reads its secrets from `/config/.env`.
```bash
A=/mnt/user/appdata/conan-bot; mkdir -p $A /mnt/user/backup/conan/snapshots
# from your workstation: scp .env.example my-conan-bot.xml root@192.168.10.10:/mnt/user/appdata/conan-bot/
cp $A/.env.example $A/.env && nano $A/.env                           # fill in the secrets
chown -R 99:100 $A /mnt/user/backup/conan/snapshots && chmod 600 $A/.env
cp $A/my-conan-bot.xml /boot/config/plugins/dockerMan/templates-user/my-conan-bot.xml
docker run -d --name conan-bot --restart unless-stopped -e TZ=America/New_York \
  -v $A:/config:ro -v /mnt/cache/appdata/conanexiles_jons:/conan:ro \
  -v /mnt/user/backup/conan/snapshots:/backups \
  ghcr.io/zbrisson/conan-bot:latest
docker logs -f conan-bot          # expect "ready as ... daily restart at 05:00"
```
If the GHCR package is private, either make it public (Package settings → visibility) or run `docker login ghcr.io` on Unraid with a read-only `read:packages` token. The template in `templates-user/` has the same name, so Unraid manages it like any other container and its update check works. Add `conan-bot` to the Games autostart sequence after `ConanExiles_jons` (FolderView `autostart.json`).

## Verification
| Check | How |
|---|---|
| Status | `/conan status` shows RUNNING, players, tick rate, build |
| RCON | `/conan players` works; 25575 closed from outside (phone hotspot: `nc -vz <your-public-ip> 25575`) |
| Restart | `/conan restart` with nobody on → ✅ post, new snapshot in `snapshots/`, new `Build:` line in the log |
| Allowlist | `TARGET_CONTAINER` is hard-coded; the bot has no code path for any other container |
| Schedule | set `RESTART_TIME` 2 min ahead, `docker restart conan-bot`, watch one run, set back to `05:00` |
| Events | join and leave once → ➕/➖ posts; `docker kill ConanExiles_jons` → ⚠️ unexpected-stop post |
