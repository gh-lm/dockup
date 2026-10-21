# Changelog

Each release lists the changes that matter most rather than every small fix
along the way.

## 1.2.1 - 2026-10-21

Hardening and accessibility, from a second independent review.

- **Two-factor secrets are encrypted at rest**, like every other secret.
  Secrets saved by an earlier version are encrypted the first time Dockup
  starts
- **Builds are reproducible.** Every dependency is fixed to an exact version
  and checked by hash, the base image and the helper image are fixed by
  digest, and the build workflow's actions are fixed to exact commits
- **The helpers that restart Dockup or install the companion no longer
  download anything.** They run in a private folder with a name nobody can
  guess, and the Tailscale installer is downloaded to a file first and its
  checksum shown
- **Safer deletes and prunes.** A folder that another stack also mounts is
  never offered for deletion with a stack's data, and unused volumes are
  removed only from the list that was shown
- **Restoring one stack's backup is safer.** It works out where the data goes
  from the compose file being restored, refuses system folders and other
  stacks' folders, shows the person the list before writing anything, needs
  the stack stopped, and keeps the old compose file and .env beside the new
  ones. Backups and restores of one stack no longer run at the same time as
  anything else touching it
- **A runaway dump is stopped** instead of filling the disk, and dump files
  are readable only by their owner
- **Settings that are paths or ports are checked** when saved, and a request
  that is not what it should be gets a plain answer instead of an error page
- **Easier for everyone to use.** Failures are announced to screen readers
  and stay up long enough to read; tabs, the stack list and the colour
  choices work from the keyboard; stack names are real links; a stopped stack
  is grey rather than red and every state is also written in words; the
  terminal can be left with the keyboard; the highlight colours all keep
  readable text; and the page title follows the screen
- The unused service worker is gone

## 1.2.0 - 2026-10-19

The release that makes Dockup safe to hand to somebody else, and a pleasure
to use.

- **Dockup is one of its own stacks.** It lives in the stacks folder beside
  everything it manages, shows up in its own list, and is opened, read and
  updated exactly like any other container. The one thing a container cannot
  do to itself, replacing its own container, is handed to a short-lived
  helper, so the page goes quiet for a few seconds and comes back on its own
- **Dumps before a backup.** A database copied while it is being written to
  often will not restore. Dockup can now ask an application for a clean copy
  of itself, early in the morning, ahead of whatever copies the machine
  overnight, and writes a status file saying when each one was made and
  whether it worked. A dump that fails never replaces a good one
- **Backup and restore you can trust.** A backup never contains the earlier
  backups, refuses to be kept if it suddenly doubles in size, and only one
  runs at a time. Restore refuses while stacks are running, swaps each stack
  folder instead of deleting it, keeps the old one beside it, never touches
  Dockup's own folder, and asks again if the backup had to leave files out.
  Export streams from disk instead of holding everything in memory
- **Safer by default.** Dockup will not start with a missing, short or
  example secret key, and says why in one line. It cannot be stopped, taken
  down or deleted from its own screen. A backup retention of zero can no
  longer delete every backup. Settings that are numbers are checked when
  saved, and a mistake is shown under the box it belongs to
- **Stacks stop asking to be updated for ever.** On a stack with more than
  one service, Dockup compared each service against another service's image.
  Each service is now compared with its own
- **A new look, from the first page to the last.** A charcoal page with
  panels that stand clear of it, every screen opening with one plain
  sentence about how things stand, the label on a button appearing as a small
  pill, switches and our own dropdowns instead of the browser's, tables as
  raised rows, a two-panel sign-in page, and a colour picker made of the
  palette itself. Settings save themselves and the box glows briefly. There
  are no pop-up messages, no ticks and no Save button
- **Anything that cannot be undone asks twice.** The first click fills the
  button, a second within three seconds does it
- Installation is the same on any server: a compose file with an obvious
  secret key to replace, and nothing in it that belongs to one machine

## 1.1.0 - 2026-09-30

The release that turns a stack editor into something you can run a home lab
from.

- **Per-stack backup and restore, including the real data.** Bind-mounted
  folders and named volumes are archived with the stack's compose file and
  settings, and put back exactly where they came from
- **Updates.** Dockup checks every stack for a newer image every thirty
  minutes, marks the ones that have one, and can update them all in one go.
  Redeploy recreates a stuck stack from its compose file without pulling
- **A proper editor.** The compose file is edited with live validation, and
  creating or editing a stack warns when a port is already taken
- **Adopt what already runs.** Existing compose projects and loose containers
  come under management in one pass, with nothing restarted, and a first-run
  prompt offers it
- **Private registries.** Sign-in details are entered once in Settings,
  stored encrypted, and used by every pull
- **An optional helper on the machine itself** for checking and applying
  operating system updates and for switching Tailscale Serve on a stack's
  ports, installed from Settings in one click
- **Archive instead of delete.** A stack with nothing running can be put
  away with its configuration kept, or removed for good with its images
- **Hardened container.** It runs as an ordinary user, drops every privilege
  it does not need, binds to the local machine only, and keys its login
  lockout on the real connecting address rather than one a client can invent
- Every screen works on a phone, with buttons that grow to be easy to tap

## 1.0.0 - 2026-08-12

The first release: one window for every stack on one machine.

- A dashboard of stacks, a compose file editor, and a converter from a
  `docker run` command to a compose file
- Start, stop, restart, update and delete for each stack, with a
  confirmation before anything destructive
- Live logs with errors and warnings picked out, and a terminal inside any
  container
- Cleaning up unused images, containers, networks and build cache, one at a
  time or together, with the volumes that would go shown first
- A running activity log, and optional email when something breaks
- A real sign-in: a signed session cookie, a lockout after repeated failures,
  and optional two-factor codes
- Settings with encrypted secrets and a button to test the email setup
- Daily backups of Dockup's own settings and history, a restore that can
  itself be undone, and a download of everything
- Installable as an app from the browser
