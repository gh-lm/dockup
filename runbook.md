# Dockup runbook

Plain-language guide for running Dockup. Everything here works from the
UI or a couple of copy-paste commands - no deep terminal knowledge needed.

## Install (Docker host)

Dockup is a normal pre-built image (`ghcr.io/gh-lm/dockup`, built by GitHub for every
release) - nothing to clone or build, just its compose file:

```bash
mkdir -p /opt/dockup /opt/stacks && cd /opt/dockup
curl -fsSL -o compose.yaml https://dockup.lightmorphic.com/compose.yaml
echo "SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" > .env
docker compose up -d
```

Dockup runs as a non-root user with UID 1000 inside its container, matching
the usual first-user account on a fresh Linux install. If your own user
isn't UID 1000 (check with `id -u`), the container still starts fine but
can't write to `data/` - fix it once:

```bash
sudo chown -R 1000:1000 /opt/dockup/data /opt/stacks
```

Open `http://<server-ip>:4000` and create the admin account.
Keep a copy of the `.env` file's SECRET_KEY somewhere safe (password
manager). It encrypts your saved settings - lose it and saved passwords
in Settings have to be re-entered.

## Optional: the dockup-companion (Tailscale Serve + OS updates)

Everything above is all Dockup needs to run. This second, separate step
is only for two extra features - checking/applying host OS updates, and
turning Tailscale Serve on/off per stack - and it's the one part of
Dockup that needs root on the actual server, not just Docker access.
Skip it entirely if you don't want those two features; everything else
works the same either way.

**One click**: Settings → Host OS & Tailscale → "Install companion". This installs
the systemd service on the host for you (a short-lived, one-time
privileged action - nothing standing afterward beyond the service
itself), then tells you the one remaining manual step below.

**Manually**, if you'd rather. The companion's three files are inside the
image, so taking them from there always gives you the version you are
actually running:

```bash
mkdir -p /tmp/dockup-companion
cid=$(docker create ghcr.io/gh-lm/dockup:latest)
docker cp "$cid:/app/companion" /tmp/dockup-companion
docker rm "$cid"
cd /tmp/dockup-companion/companion && sudo sh install.sh
```

Either way, finish by giving Dockup the companion socket in
`compose.override.yaml` (see the per-machine settings section below):

```yaml
services:
  dockup:
    volumes:
      - /run/dockup-companion.sock:/run/dockup-companion.sock
```

and restart Dockup (`docker compose up -d`). Settings → Host OS & Tailscale and each
stack's Tailscale tab go from "not set up" to working once that's done.

Also worth knowing: once the companion is installed, Dockup
automatically pauses any Tailscale Serve rule that's using a port a
stack is about to bind to, and restores it right after - this is what
prevents the classic "address already in use" failure when a stack you
deleted and recreated tries to grab a port Tailscale Serve is still
holding open. Without the companion, Dockup can't do this for you and
will just explain what happened and how to fix it by hand.

## First steps after install

1. **Settings → Email alerts**: fill in SMTP and press *Send test email*.
   Until this works, error alerts only show in Activity.
2. **Settings → Account**: consider switching on 2FA.
3. **Dashboard**: if you already had things running, an "adopt" panel
   lists them - adopt what you want Dockup to manage.

## Daily use

Everything is a button: start/stop/restart/update per stack, logs and
terminal per stack, pruning under Maintenance, backups under Backups.
Errors show in red in Activity, and email you if SMTP is set up.

## Restore a backup

Backups run daily (hour and retention set in Settings) and cover the
stacks folder plus Dockup's own database.

- **From the UI**: Backups → pick one → Restore (click twice). The
  current files are kept at `data/pre-restore-stacks` first, so a restore
  can itself be undone by copying that folder back.
- Restored stacks aren't restarted automatically - press play on each
  when you're ready.

## A file keeps being left out of the backup

Activity says something like "The backup left out 1 file(s) it couldn't
read", and names the file. The backup itself worked: everything except
the named files is in the archive.

Dockup runs as an ordinary user (UID 1000) inside its container, never
as root, so a file owned by root with permissions that shut everyone
else out can't be read. The usual ones are leftovers such as
`.env.before-move` and database files a container wrote as root.

Fix it on the Docker host, either way round. Delete the leftover if you
don't need it:

```
sudo rm /opt/stacks/<stack>/.env.before-move
```

Or let Dockup read it:

```
sudo chown 1000:1000 /opt/stacks/<stack>/<file>
```

Then press **Back up now** in Backups. The warning stops as soon as the
file can be read, and no alert email is ever sent for this - the backup
succeeded, so it's a warning, not an error.

## Back up one stack, including its real data

Open a stack → **Backup** tab → **Back up now**. Unlike the daily backup
above (which only covers compose files), this also archives the stack's
actual data - bind-mounted folders and named volumes, read straight from
wherever they already live. Restoring puts everything back to exactly
the same place, nothing gets relocated.

This needs Dockup to know its own real path on the host, since it asks
Docker to start small helper containers that mount both the stack's data
and Dockup's own backup folder side by side. If you installed Dockup
somewhere other than `/opt/dockup`, set this in `.env`:

```
DOCKUP_DATA_HOST_PATH=/wherever/you/put/dockup/data
```

If it's missing or wrong, per-stack backups fail with a clear error
telling you to set it - the daily/global backup above doesn't need it.

## Roll back Dockup itself

Every release is published as its own image tag. To go back to a
previous version, pin the tag in `compose.override.yaml`:

```yaml
services:
  dockup:
    image: ghcr.io/gh-lm/dockup:1.1.0
```

then `docker compose up -d`. Remove the override and pull to move
forward again. Your data (stacks, database, backups) is untouched by
rollbacks - it lives in `/opt/stacks` and `/opt/dockup/data`, outside
the image.

## Restart / update Dockup

Dockup is a container like any other, and it sits in the stacks folder
alongside everything it manages, so it appears in its own list. Open it
and press Update: it pulls the newer image and brings itself back, the
same as any other stack. From a terminal it is the same two commands as
anything else:

```bash
cd /opt/stacks/dockup
docker compose pull && docker compose up -d
```

There is one thing Dockup cannot do to itself. `compose up` replaces
Dockup's container, which kills the process running the command before
it can start anything again. So that one step is handed to a short-lived
helper container that is not the one being replaced - the same approach
the companion installer already uses. That is why the page goes quiet
for a few seconds and then comes back on its own: the container serving
it has just been replaced.

If Dockup's folder is somewhere other than the stacks folder, its card
says so and it cannot be updated from the screen. Move that folder into
the stacks folder (keeping compose.yaml, .env and data together) and it
behaves like every other stack.

## Pull from a private registry

For images that need a login - your own Forgejo or GitLab registry, a
private image repository. Nothing to edit on the server.

1. Create a token on the registry that can only read packages (pull).
2. In Dockup: Settings -> Private registries. Enter the registry
   address with its port if it has one (for example
   `registry.example.com:5000`), the username, and the token.
3. Press Test. "Signed in" means it worked. Then Save registry.

From then on the Update button, the scheduled check for new images and
Update all sign in to that registry by themselves. To rotate a token,
enter the same address and username with the new token and save - it
replaces the old one.

## Per-machine settings: compose.override.yaml

Anything specific to one server - the companion socket mount, a
different published port, an extra volume - belongs in
`compose.override.yaml` next to `compose.yaml`, not in `compose.yaml`
itself. Compose reads and merges it automatically, so a newer
`compose.yaml` (fetched by hand, or by a future Dockup) never collides
with your local edits.

```yaml
services:
  dockup:
    volumes:
      - /run/dockup-companion.sock:/run/dockup-companion.sock
```

## Dumps before a backup

A database copied while it is being written to often will not restore.
Whatever copies this machine overnight copies live files, so anything
that cannot be snapshotted safely has to be dumped first by something
that understands the application.

Settings has a panel for it. Each entry names the container to run in,
the command that writes a clean copy to its output, and the file name to
save it as. For Forgejo, which holds the git repositories:

```json
[
  {
    "name": "forgejo",
    "container": "forgejo",
    "command": ["forgejo", "dump", "--type", "zip", "--file", "-"],
    "file": "forgejo-dump.zip"
  }
]
```

They run at 2am, well before an overnight copy that starts later, and
land in `data/pre-backup` inside Dockup's own folder, which gets swept
up with everything else. Beside them is `status.json` saying when they
were written, whether each worked, and the size and checksum of each
file, so whatever reads it can tell a fresh dump from a stale one.

A dump that fails leaves the last good one exactly where it is and says
so, with its age. Overwriting a known good archive with an empty one is
the one thing this must never do.

Press *Run them now* in Settings to see it work rather than waiting
until the morning.

## If Dockup is down

```bash
cd /opt/dockup && docker compose ps      # is it running?
docker compose logs --tail 50 dockup     # what happened?
docker compose up -d                     # start it again
```

The container restarts itself (`restart: unless-stopped`) and has a
health check, so a crash normally self-heals within a minute.

## A stack says it's running but nothing can reach it

After the Docker daemon restarts, a container can come back up reporting
Up and healthy while its ports were never published. Dockup flags this
now: the stack shows as a problem and names the port. A restart does not
fix it - press **Redeploy**, which recreates the container and
re-establishes the port.

To see it by hand:

```bash
docker port <container-name>
```

Empty output on a running container that should publish something is the
fault.

## Tailscale Serve gets no certificate for a port

If a stack publishes a port on every interface (`4050:80` rather than
`127.0.0.1:4050:80`) and Tailscale Serve fronts that same port, Docker's
binding covers the tailnet address too and Tailscale can never bind it.
Serve still lists the rule and plain HTTP still works, so it looks fine,
but HTTPS to that address fails.

This was tested directly on a scratch port: with a Serve rule in place,
a container publishing on every interface **cannot start at all**, and a
container publishing on loopback starts cleanly and serves over HTTPS
while Serve holds the tailnet address at the same time. So the choice
per stack is:

- **Reached over Tailscale** (the usual case): publish on `127.0.0.1`.
  Serve fronts it, HTTPS works, and stopping or restarting the stack
  never fights Tailscale for the port.
- **Reached directly from your network as well**: publish on every
  interface and accept that Serve gets no certificate for that port, and
  that the stack may refuse to start while a Serve rule for it exists.

Dockup warns about this on the stack's page. To take the first option,
in the stack's Compose tab:

```yaml
    ports:
      - "127.0.0.1:4050:80"
```

then Redeploy. Tailscale Serve reaches it on 127.0.0.1 exactly as before,
and it stops the reverse problem too - Tailscale grabbing a container's
port while the container is down, which leaves the container unable to
start.

## When a stack can't be checked for an update

A stack's dot cannot tell you a newer image is waiting if Dockup cannot
reach the registry at all. The usual cause is DNS: Docker gives each
container its name lookups, and on this machine it has nothing usable to
pass on. Other containers will be affected too, not just Dockup -
anything that checks a website, sends email or pulls an image.

Check it from the host:

```bash
docker exec dockup getent hosts ghcr.io || echo "no DNS in the container"
```

If that fails, point the Docker daemon at a name server that works by
putting this in `/etc/docker/daemon.json` and restarting Docker:

```json
{ "dns": ["1.1.1.1", "8.8.8.8"] }
```

A private registry needs its sign-in details in Settings before Dockup
can ask it anything.

## Uptime check

Dockup answers on `/health` without a login. Point any LAN uptime tool
(Uptime Kuma, etc.) at `http://<server-ip>:4000/health` and alert on
non-200. That way you hear about it even if Dockup itself is the thing
that's down.

## Full export / moving house

Backups → *Download everything (zip)* gives you the stacks folder and
Dockup's database in one file. The stacks folder alone is enough to run
everything with plain `docker compose` anywhere - no lock-in.

## Publishing a new version

GitHub builds and publishes the image; nothing needs building on your own
machine. Put the new number in `VERSION` in `app/config.py` and on top of
`CHANGELOG.md`, commit, then tag that commit with a `v` in front of the
number and push the tag:

```bash
git tag v1.2.0
git push origin v1.2.0
```

The workflow in `.github/workflows/image.yml` checks the tag matches
`VERSION`, builds for amd64 and arm64, and publishes
`ghcr.io/gh-lm/dockup:1.2.0` and `:latest`. The first time, set the package's
visibility to public in the repository's package settings on GitHub so that
anyone can pull it without signing in.

To try an image on your own machine before tagging, `tools/build-image.sh`
builds it locally as `dockup:<version>` without pushing anything.

Once it is published, every install sees it the same way it sees a newer
image for any other stack: Dockup's own card shows an update is waiting at
the next check, and pressing Update pulls it.
