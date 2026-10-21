#!/bin/sh
# Starts as root (needed once, to read the mounted socket's group and
# create the data dirs), then drops to the unprivileged dockup user for
# the actual app process. This is the standard pattern for containers
# that need to match a host-specific group at runtime - the docker.sock
# group ID varies per host and can't be baked into the image.
set -e

# The one secret Dockup needs, checked here rather than left to the app
# inside gunicorn. A worker that exits takes gunicorn's restart loop with
# it: the container goes on saying it is running, requests hang instead
# of failing, and the one line that says what is wrong scrolls past
# among the restart messages. Checking first means the container stops,
# and the reason is the last thing in the log.
case "$SECRET_KEY" in
  "" | *CHANGE-ME*)
    echo "Dockup cannot start: SECRET_KEY has not been set." >&2
    echo "It signs your login, so it has to be yours alone - an install left on" >&2
    echo "the example value could be signed into by anyone who has read it." >&2
    echo "Put a long random one in a .env file beside compose.yaml, then start" >&2
    echo "Dockup again:" >&2
    echo "  echo \"SECRET_KEY=\$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')\" > .env" >&2
    exit 1
    ;;
esac
if [ "${#SECRET_KEY}" -lt 32 ]; then
  echo "Dockup cannot start: SECRET_KEY is too short - use at least 32 characters." >&2
  exit 1
fi

mkdir -p "$DOCKUP_DATA" "$DOCKUP_STACKS"
chown dockup:dockup "$DOCKUP_DATA" 2>/dev/null || true

# Join the group that owns each mounted socket, whatever GID it happens
# to have on this host - same trick for docker.sock and the optional
# dockup-companion.sock (see companion/install.sh, only mounted in if
# you've set that up).
for SOCK in /var/run/docker.sock /run/dockup-companion.sock; do
  if [ -S "$SOCK" ]; then
    SOCK_GID=$(stat -c '%g' "$SOCK")
    # Group 0 owns far more than a socket, so Dockup is never added to it.
    if [ "$SOCK_GID" = "0" ]; then
      echo "Warning: $SOCK belongs to group root. Dockup will not join that group;" >&2
      echo "give the socket its own group, or add group_add to compose.yaml." >&2
      continue
    fi
    if ! getent group "$SOCK_GID" >/dev/null 2>&1; then
      addgroup -g "$SOCK_GID" "grp$SOCK_GID"
    fi
    GROUP_NAME=$(getent group "$SOCK_GID" | cut -d: -f1)
    adduser dockup "$GROUP_NAME" >/dev/null 2>&1 || true
  fi
done

exec su-exec dockup "$@"
