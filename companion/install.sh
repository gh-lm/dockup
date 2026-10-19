#!/bin/sh
# Installs dockup-companion as a systemd service directly on the host (not
# in a container). Needs root. Safe to re-run.
set -e

if [ "$(id -u)" != "0" ]; then
  echo "Run this as root: sudo sh install.sh"
  exit 1
fi

mkdir -p /opt/dockup-companion
cp "$(dirname "$0")/dockup-companion.py" /opt/dockup-companion/dockup-companion.py
cp "$(dirname "$0")/dockup-companion.service" /etc/systemd/system/dockup-companion.service

# A dedicated group, not the docker group - this socket only reaches
# the narrow OS-update/Tailscale command set, nothing Docker-related,
# so it doesn't need or want docker-group members to have it by default.
getent group dockup-companion >/dev/null || groupadd --system dockup-companion

systemctl daemon-reload
systemctl enable --now dockup-companion

# The socket is created fresh on each start with group dockup-companion -
# fix the group here too in case something recreated it before the
# service unit's own permissions applied.
sleep 1
[ -S /run/dockup-companion.sock ] && chgrp dockup-companion /run/dockup-companion.sock

echo "dockup-companion installed and running."
echo "Group GID for compose.yaml/entrypoint reference: $(getent group dockup-companion | cut -d: -f3)"
echo "Add Dockup's container to this group (compose.override.yaml or matching entrypoint logic) so it can reach /run/dockup-companion.sock."
