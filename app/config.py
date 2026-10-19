import os
import sys
from pathlib import Path

DATA_DIR = Path(os.environ.get("DOCKUP_DATA", "/app/data"))
STACKS_DIR = Path(os.environ.get("DOCKUP_STACKS", "/opt/stacks"))
MOCK_MODE = os.environ.get("DOCKUP_MOCK") == "1"

DB_PATH = DATA_DIR / "dockup.db"
BACKUP_DIR = DATA_DIR / "backups"
STACK_BACKUP_DIR = DATA_DIR / "stack-backups"

# Where Dockup writes the docker config.json holding private-registry
# credentials (see registries.py). Deliberately in the container's own
# /tmp rather than DATA_DIR: it holds decrypted tokens, so it should not
# sit on the persistent volume or be anywhere near a backup. It is
# rebuilt from the encrypted copies in the database at every start.
DOCKER_CONFIG_DIR = Path(os.environ.get("DOCKUP_DOCKER_CONFIG_DIR", "/tmp/dockup-docker-config"))

# The real host-filesystem path of DATA_DIR, needed when Dockup asks the
# Docker socket to start a helper container with a bind mount destined
# for this folder - the daemon resolves that against the host, not
# Dockup's own container, so a container-internal path won't do.
DATA_HOST_PATH = os.environ.get("DOCKUP_DATA_HOST_PATH", "")

SECRET_KEY = os.environ.get("SECRET_KEY", "")

STACK_NAME_RE = r"^[a-z0-9][a-z0-9_-]{0,62}$"
COMPOSE_FILENAMES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")

# The only variables of Dockup's own environment that reach `docker compose`.
# Everything else is dropped on purpose: compose lets the environment it runs
# in win over a stack's .env, so anything passed here silently becomes that
# stack's value (Dockup's SECRET_KEY or TZ would override the stack's own).
# These few are what the docker CLI itself needs to reach the daemon, find
# its config, or get out through a proxy.
# PATH is deliberately absent: the docker binary is resolved to an absolute
# path up front, and a stack that sets its own PATH must not have Dockup's
# shadow it.
COMPOSE_PASSTHROUGH = (
    "HOME",
    "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)

# Dockup's own version, shown in the top bar next to the update dot.
# Kept in step with the top entry in CHANGELOG.md by hand - one number,
# one place. tools/build-image.sh reads this same line to tag the
# published image.
VERSION = "1.2.0"

SESSION_DAYS = 7
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_MIN = 15


def validate():
    if not SECRET_KEY:
        sys.exit("SECRET_KEY is not set. Generate one with:\n"
                 '  python3 -c "import secrets;print(secrets.token_urlsafe(48))"')
    if len(SECRET_KEY) < 32:
        sys.exit("SECRET_KEY is too short - use at least 32 characters.")


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    STACK_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    STACKS_DIR.mkdir(parents=True, exist_ok=True)
