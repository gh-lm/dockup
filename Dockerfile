# The base image is pinned by digest so every build starts from the same
# bytes. To take a newer one, change the digest on purpose.
# ---- Build stage: install Python dependencies ----
FROM python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.txt

# ---- Final stage: only the runtime, no build tooling ----
FROM python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a

# docker-cli + compose plugin talk to whichever socket is mounted
# - Dockup itself never needs a daemon of its own.
# su-exec drops from root to the dockup user after the entrypoint's
# one-time setup. apk upgrade patches OS packages at build time rather
# than trusting whatever was baked into the base image when published.
RUN apk update && apk upgrade --no-cache \
    && apk add --no-cache docker-cli docker-cli-compose tzdata su-exec

COPY --from=build /install /usr/local

# Remove pip/setuptools/wheel from the final image - they're only
# needed to install dependencies, not to run the app, and shipping them
# means shipping their own occasional CVEs for no runtime benefit.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip* \
           /usr/local/lib/python3.13/site-packages/setuptools* \
           /usr/local/lib/python3.13/site-packages/wheel* \
           /usr/local/bin/pip*

# Fixed UID 1000 matches the common single-user Linux default (and the
# PUID=1000 convention already used by containers like linuxserver.io's).
# If your host user isn't UID 1000, chown the bind-mounted data/stacks
# folders to match - see the runbook.
RUN addgroup -g 1000 dockup && adduser -u 1000 -G dockup -D dockup

# Where the source of this image lives.
LABEL org.opencontainers.image.source="https://github.com/gh-lm/dockup"

WORKDIR /app
COPY app ./app
COPY run.py .
# Bundled so the one-click "Install companion" button (Settings → Host OS & Tailscale)
# can stage these onto the host without needing a separate download -
# the same three files as a manual `companion/install.sh` run.
COPY companion ./companion
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV DOCKUP_DATA=/app/data \
    DOCKUP_STACKS=/opt/stacks \
    PYTHONUNBUFFERED=1

EXPOSE 5001

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5001/health',timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "-w", "1", "--threads", "32", "-b", "0.0.0.0:5001", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "--access-logformat", "%(t)s %(h)s \"%(r)s\" %(s)s rt=%(L)s", \
     "run:app"]
