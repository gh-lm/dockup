# Security policy

Dockup is built for home labs on a private network, not for public
internet exposure. It holds the Docker socket, which is effectively root on
the machine it runs on: treat access to Dockup itself as equivalent to root
access to that machine.

It is a single-maintainer project, not a company or a team with a dedicated
security function. This page sets realistic expectations rather than
promising something that is not true.

## Supported versions

Only the latest release is supported, which is the `latest` tag and the most
recent tagged commit. There is no long-term-support branch and no
backporting. If a vulnerability is found, the fix lands in the next release
and you are expected to update to it.

| Version | Supported |
|---|---|
| 1.2.x | Supported |
| 1.1.x and earlier | Not supported |

## Reporting a vulnerability

**Please do not put the details of a security vulnerability in a public
issue.** A public report discloses the problem to everyone, including anyone
running an unpatched copy, before a fix exists.

Use GitHub's private vulnerability reporting on this repository, under
**Security**, then **Report a vulnerability**. It goes straight to the
maintainer and nobody else can see it.

If you would rather not use GitHub, write to <security@lightmorphic.com> with
"Dockup security" in the subject line and say only that you have a security
issue to report. Leave the details out of that first message, and a private
channel will be arranged for the rest.

## What to expect

This is maintained in whatever time is available outside other commitments,
not against a service level agreement. Best effort, not guaranteed:

- Acknowledgement of a report: a few days is the aim
- A fix or a mitigation: depends on severity and complexity. It could be
  same-day for something simple and serious, or longer for something subtle

## Scope

This policy covers the Dockup application itself: the backend, the front end,
the Dockerfile and the optional companion. It does not cover:

- Vulnerabilities in dependencies. Report those to the dependency's own
  maintainers
- Your own deployment: reverse proxy configuration, network exposure, the
  Docker daemon and the host operating system are outside this project's
  control

## What's already in place

- Signed session cookies, rate-limited login, optional TOTP 2FA. Signing out
  clears the cookie in the browser; a stolen cookie stays valid until it
  expires (seven days), or until Dockup's `SECRET_KEY` changes
- Passwords hashed with Werkzeug's scrypt-based hasher, never stored in plain text
- All other secrets (SMTP credentials, the two-factor secret, etc.) encrypted
  at rest with a key derived from `SECRET_KEY`, and never echoed back to the
  browser
- Private-registry tokens (Settings -> Private registries) are encrypted
  at rest in the database and never returned to the browser. Docker only
  reads credentials from a config.json, so Dockup writes the decrypted
  tokens to one at `/tmp/dockup-docker-config/config.json` inside its own
  container - directory mode 700, file mode 600, rebuilt from the
  database at every start, and outside the data volume so it is never
  included in a backup or export. Use a read-only (pull) token
- CSRF token required on every state-changing request once signed in. The
  sign-in and first-run setup forms are protected by the session cookie's
  `SameSite` setting instead
- Parameterised SQL throughout - no string-built queries
- A Content-Security-Policy that allows scripts and styles only from Dockup
  itself (plus the one launcher script below) and no inline scripts, with
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and a
  restrictive `Permissions-Policy`
- Backup files are readable only by their owner, and a restore never touches
  Dockup's own folder
- No analytics, no CDNs, no cookies beyond the session cookie needed to
  be logged in at all
- One external request by design: the Lightmorphic app launcher in the
  top bar loads a script and icons from `apps.lightmorphic.com`, which
  the Content-Security-Policy names explicitly. That script runs with
  the same rights as the rest of the page, so it is a real trust
  decision - remove those two entries from the policy in
  `app/__init__.py`, and the `all-apps` div and script tag from
  `app/templates/app.html`, to drop it
