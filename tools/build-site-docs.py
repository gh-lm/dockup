#!/usr/bin/env python3
"""Turn the project's markdown documents into pages on the website.

Dockup's image is built by GitHub and its website lives on Cloudflare Pages.
Neither of those serves a browsable repository, so the documents people
need - the README, the runbook, the changelog, the security notes and the
licence - have to be pages on the site itself, and the source has to be a
download. This script builds all of that, so a release only ever means
re-running it rather than editing HTML by hand.

Run from the repository root:

    python3 tools/build-site-docs.py

No third-party libraries: the markdown subset these documents actually use
(headings, paragraphs, bullet and numbered lists, fenced code, one table,
block quotes, inline code, bold and links) is small enough to render here,
and a build step that works with nothing installed is worth more than a
complete markdown implementation.
"""

import html
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITES = ROOT / "sites"
SITE_URL = "https://dockup.lightmorphic.com"

# Everything that belongs in the source download. The licence requires the
# source that corresponds to the image, so this is the real tree, comments
# and all, not a stripped copy.
SOURCE_PATHS = [
    "app", "companion", "tests", "tools",
    "CHANGELOG.md", "Dockerfile", "LICENSE", "README.md", "SECURITY.md",
    "compose.yaml", "dev_run.py", "docker-entrypoint.sh", "requirements.txt",
    "run.py", "runbook.md",
]


# ---- markdown ----------------------------------------------------------

# A document that links to another document by its file name has to come
# out pointing at that document's page here. Anything relative that is not
# in this map is a link with nowhere to go, and the build stops rather
# than quietly shipping it.
LINK_MAP = {
    "README.md": "documentation.html",
    "runbook.md": "runbook.html",
    "CHANGELOG.md": "whats-new.html",
    "SECURITY.md": "security.html",
    "LICENSE": "licence.html",
    "LICENCE": "licence.html",
}
# Link text taken from the file name reads as a file name ("LICENSE"), so
# each one gets the name of the page it now is.
LINK_TEXT = {
    "README.md": "the documentation",
    "runbook.md": "the runbook",
    "CHANGELOG.md": "what's new",
    "SECURITY.md": "the security page",
    "LICENSE": "the licence",
    "LICENCE": "the licence",
}


def _link(label, href):
    """One markdown link, with relative targets mapped onto this site."""
    if re.match(r"^(https?:|mailto:|#)", href):
        return f'<a href="{href}">{label}</a>'
    target, _, fragment = href.partition("#")
    if target == "compose.yaml":
        return f'<a href="compose.yaml">{label}</a>'
    if target not in LINK_MAP:
        raise SystemExit(
            f"link to '{href}' has nowhere to go on the site: add it to LINK_MAP "
            "in tools/build-site-docs.py, or change the link in the markdown")
    if label == target:
        label = LINK_TEXT[target]
    return f'<a href="{LINK_MAP[target]}{"#" + fragment if fragment else ""}">{label}</a>'


def slug(text):
    """A heading turned into something that can be linked to."""
    out = re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>", "", text).lower()).strip("-")
    return out or "section"


def inline(text):
    """Inline markdown, with code spans protected from everything else."""
    spans = []

    def stash(m):
        spans.append(html.escape(m.group(1)))
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = html.escape(text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\((\S+?)\)",
                  lambda m: _link(m.group(1), m.group(2)), text)
    # Bare addresses in the prose, left clickable. The host has to have a
    # dot in it, or an escaped quotation mark on the end of a sentence
    # gets swallowed into the address.
    text = re.sub(r"(?<![\"'>=])(https?://[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}[^\s<)]*[^\s<).,;:&])",
                  r'<a href="\1">\1</a>', text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{spans[int(m.group(1))]}</code>", text)
    return text


def render(md):
    """Render the markdown subset these documents use. Returns HTML."""
    lines = md.split("\n")
    out, i = [], 0
    seen_title = False
    # Open list elements, as (indent, tag) - so a nested bullet closes at
    # the right depth rather than guessing on the way out.
    stack = []

    def close_lists(to_indent=-1):
        while stack and stack[-1][0] > to_indent:
            out.append(f"</li></{stack.pop()[1]}>")

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            close_lists()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre><code>" + "\n".join(code) + "</code></pre>")
            continue

        if stripped.startswith("<"):
            # Pictures and the tables that lay them out belong to the README
            # on the code host. The site's pages carry the words only.
            close_lists()
            closer = "</table>" if stripped.startswith("<table") else "</p>"
            while i < len(lines) and closer not in lines[i]:
                i += 1
            i += 1
            continue

        if stripped.startswith("|"):
            close_lists()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            if len(rows) > 1 and all(set(c) <= set("-: ") for c in rows[1]):
                head, body = rows[0], rows[2:]
            else:
                head, body = None, rows
            out.append('<div class="table-scroll"><table>')
            if head:
                out.append("<thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead>")
            out.append("<tbody>")
            for row in body:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
            out.append("</tbody></table></div>")
            continue

        if stripped.startswith(">"):
            close_lists()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote><p>" + inline(" ".join(quote)) + "</p></blockquote>")
            continue

        heading = re.match(r"(#{1,4})\s+(.*)", stripped)
        if heading:
            close_lists()
            level = len(heading.group(1))
            if level == 1 and not seen_title:
                # The document's own title is already the page's h1.
                seen_title = True
                i += 1
                continue
            tag = f"h{min(level + 1, 6)}"
            text = inline(heading.group(2))
            if level == 2:
                # Each ## starts a section, and every section is a card of
                # its own rather than another paragraph in one endless
                # wall of text. The anchor is what the contents list at
                # the top of the page links to.
                out.append(f'[[SECTION:{slug(heading.group(2))}:{text}]]')
            else:
                out.append(f"<{tag}>{text}</{tag}>")
            i += 1
            continue

        bullet = re.match(r"( *)([-*]|\d+\.)\s+(.*)", line)
        if bullet:
            indent = len(bullet.group(1))
            tag = "ol" if bullet.group(2)[0].isdigit() else "ul"
            while stack and stack[-1][0] > indent:
                out.append(f"</li></{stack.pop()[1]}>")
            if stack and stack[-1][0] == indent:
                out.append("</li><li>")
            else:
                out.append(f"<{tag}><li>")
                stack.append((indent, tag))
            out.append(inline(bullet.group(3)))
            i += 1
            continue

        if not stripped:
            if stack:
                # A blank line inside a list is spacing, not the end of it:
                # the next line's indent decides.
                nxt = next((l for l in lines[i + 1:] if l.strip()), "")
                if not re.match(r" *([-*]|\d+\.)\s+", nxt) and not nxt.startswith("  "):
                    close_lists()
            i += 1
            continue

        if stack:
            # Wrapped text belonging to the list item above.
            out.append(" " + inline(stripped))
            i += 1
            continue

        para = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
                r"( *([-*]|\d+\.)\s+|#{1,4}\s|```|\||>)", lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + inline(" ".join(para)) + "</p>")

    close_lists()
    return "\n".join(out)


# ---- the page shell ----------------------------------------------------

BRAND_SVG = ('<svg width="30" height="30" viewBox="0 0 48 48" aria-hidden="true">'
             '<rect width="48" height="48" rx="10" fill="#FBC711"/>'
             '<rect x="7" y="26" width="15" height="15" rx="3" fill="#ffffff"/>'
             '<rect x="26" y="26" width="15" height="15" rx="3" fill="#fff"/>'
             '<rect x="16.5" y="7" width="15" height="15" rx="3" fill="#fff"/></svg>')
FOOT_SVG = BRAND_SVG.replace('width="30" height="30" viewBox="0 0 48 48"',
                             'viewBox="0 0 48 48" width="26" height="26"')

CSP = ("default-src 'self'; script-src 'self' https://apps.lightmorphic.com; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data: https://apps.lightmorphic.com; "
       "font-src 'self'; connect-src 'self' https://apps.lightmorphic.com; "
       "form-action 'none'; base-uri 'none'; object-src 'none'; upgrade-insecure-requests")

# The strip of links to the source, directly above the legal footer on every
# page. These are the only links to the code host on the site.
REPO_URL = "https://github.com/gh-lm/dockup"
_GITHUB_MARK = ('<path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.012 8.012 0 0 0 16 8c0-4.42-3.58-8-8-8z"/>')
_REPO_LINKS = [
    ("Repository", REPO_URL),
    ("Documentation", REPO_URL + "/blob/main/README.md"),
    ("Security", REPO_URL + "/security/policy"),
    ("Licence", REPO_URL + "/blob/main/LICENSE"),
    ("Changelog", REPO_URL + "/blob/main/CHANGELOG.md"),
    ("Issues", REPO_URL + "/issues"),
    ("Releases", REPO_URL + "/releases"),
]


def oss_bar() -> str:
    links = "\n".join(
        f'    <a href="{href}" target="_blank" rel="noopener noreferrer" '
        f'aria-label="{text} on GitHub (opens in a new tab)">{text}</a>'
        for text, href in _REPO_LINKS)
    return f'''<div class="wrap">
<div class="oss-bar">
  <div class="marks">
    <a href="{REPO_URL}" target="_blank" rel="noopener noreferrer"
       aria-label="Dockup on GitHub (opens in a new tab)">
      <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">{_GITHUB_MARK}</svg>
    </a>
    <a href="https://opensource.org" target="_blank" rel="noopener noreferrer"
       aria-label="Open Source Initiative (opens in a new tab)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <circle cx="12" cy="5" r="2.6"/>
        <circle cx="5.2" cy="17" r="2.6"/>
        <circle cx="18.8" cy="17" r="2.6"/>
        <path d="M10.7 7.3 6.5 14.7M13.3 7.3l4.2 7.4M7.8 17h8.4"/>
      </svg>
    </a>
  </div>
  <nav aria-label="Source code">
{links}
  </nav>
</div>
</div>
'''


# Pages written by hand rather than generated. The strip is added to them
# here, not pasted in page by page, and the check makes it safe to run twice.
HAND_PAGES = ["index.html", "404.html", "accessibility.html", "complaints.html",
              "cookies.html", "privacy.html", "terms.html"]


def add_strip_to_hand_pages():
    strip = oss_bar()
    for name in HAND_PAGES:
        path = SITES / name
        text = path.read_text()
        if 'class="oss-bar"' in text:
            continue
        marker = '<div class="legal">'
        if marker not in text:
            raise SystemExit(f"{name} has no legal footer to put the strip above")
        path.write_text(text.replace(marker, strip + "\n" + marker, 1))
        print(f"  strip added to {name}")


# The footer's middle column is the project itself, the right-hand one is
# the pages of this site. Keep these in step with sites/index.html.
PROJECT_LINKS = [
    ("whats-new.html", "What's new"),
    ("security.html", "Security"),
    ("licence.html", "Licence"),
]
PAGE_LINKS = [
    ("index.html#features", "Features"),
    ("index.html#install", "Install"),
    ("index.html#faq", "Questions"),
    ("documentation.html", "Documentation"),
    ("runbook.html", "Runbook"),
]

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{title} - Dockup</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{site}/{slug}">
<link rel="icon" href="dockup.svg" type="image/svg+xml">
<meta property="og:title" content="{title} - Dockup">
<meta property="og:description" content="{description}">
<meta property="og:type" content="website">
<meta property="og:url" content="{site}/{slug}">
<meta property="og:image" content="{site}/social-card.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title} - Dockup">
<meta name="twitter:description" content="{description}">
<meta name="twitter:image" content="{site}/social-card.png">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<link rel="stylesheet" href="legal.css">
</head>
<body>
<nav class="topnav" aria-label="Main">
  <div class="topnav-inner">
    <a class="topnav-brand" href="index.html">{brand}<span>Dockup</span></a>
    <ul class="topnav-links">
      <li><a href="index.html#features">Features</a></li>
      <li><a href="index.html#install">Install</a></li>
      <li><a href="index.html#faq">Questions</a></li>
    </ul>
    <div id="all-apps"></div>
  </div>
</nav>
<header class="page-head">
  <div class="wrap">
    <h1>{title}</h1>
    <p class="updated">{standfirst}</p>
  </div>
</header>
<main class="wrap">

  <div class="doc-layout">
    {body}
  </div>

</main>

<footer class="foot">
  <div class="wrap foot-inner">

    <div class="foot-brand">
      <a class="foot-lockup" href="index.html">{foot_brand}Dockup</a>
      <p class="foot-tagline">A self-hosted Docker Compose stack manager for home labs - every stack,
      its logs and its terminal in one window, with a real login and two-factor. Made by
      <a href="https://lightmorphic.com">Lightmorphic</a> and given away under the GPL.</p>
      <p class="foot-note">No cookies and no tracking. The only thing loaded from anywhere else is
      the Lightmorphic app launcher in the menu bar.</p>
    </div>

    <nav class="foot-nav foot-mid" aria-label="The project">
      <p class="foot-nav-title">The project</p>
      <ul class="foot-nav-links">
{project_links}
      </ul>
    </nav>

    <nav class="foot-nav foot-end" aria-label="Pages">
      <p class="foot-nav-title">Pages</p>
      <ul class="foot-nav-links">
{page_links}
      </ul>
    </nav>

  </div>
</footer>

{oss_bar}
<div class="legal">
  <div class="wrap legal-inner">
    <p class="legal-text">
      &copy; Lightmorphic Ltd. Registered in England and Wales, company number 17423646.<br>
      Registered office: 82A James Carter Road, Mildenhall, IP28 7DE.<br>
      Page last updated: <time datetime="{built_iso}">{built}</time>.
    </p>
    <nav class="legal-links" aria-label="Legal">
      <a href="privacy.html">Privacy</a>
      <a href="cookies.html">Cookies</a>
      <a href="terms.html">Terms</a>
      <a href="accessibility.html">Accessibility</a>
      <a href="complaints.html">Complaints</a>
    </nav>
  </div>
</div>

<script src="https://apps.lightmorphic.com/launcher.js" defer></script>
</body>
</html>
"""

DOCS = [
    ("documentation.html", "README.md", "Documentation",
     "What Dockup does, how to install it and how it is licensed.",
     "Everything Dockup does, and how to get it running."),
    ("runbook.html", "runbook.md", "Runbook",
     "Install, restore, rollback, and what to do when something is wrong.",
     "The day-to-day jobs in plain language, and what to do when something breaks."),
    ("whats-new.html", "CHANGELOG.md", "What's new",
     "Every Dockup release and what changed in it.",
     "Every release so far, newest first."),
    ("security.html", "SECURITY.md", "Security",
     "How to report a security problem in Dockup, and what is already in place.",
     "How to report a problem, and what Dockup already does to protect itself."),
    ("licence.html", "LICENSE", "Licence",
     "Dockup is free software under the GNU General Public License version 3.",
     "Dockup is free software under the GNU General Public License version 3."),
]


# The licence itself is 674 lines of legal text that almost nobody reads.
# A plain-English summary above it says what it actually means, and says
# plainly that the licence, not the summary, is the thing that counts.
LICENCE_INTRO = """<section class="licence-plain">
<h2>In plain words</h2>
<p>The licence below is the one that counts, and where this summary and the licence
disagree, the licence wins. In short, it gives you four things:</p>
<ul>
<li>Run Dockup for any purpose, on as many machines as you like, for nothing.</li>
<li>Read the source. A copy of it ships inside the image, under <code>/app</code>.</li>
<li>Change it to suit yourself.</li>
<li>Pass it on, changed or not.</li>
</ul>
<p>Two things come with that. If you give out a changed copy, it has to carry the same
licence and the source has to go with it, so the next person has the same four freedoms
you had. And because nobody is being paid for it, it comes with no warranty of any kind:
if a stack goes down or a backup turns out to be no good, that is on you, not on us. Keep
your own copies of anything you cannot lose.</p>
<p>Dockup manages other people's containers, and those have licences of their own. Nothing
here says anything about them.</p>
<p>The full text follows, word for word as the Free Software Foundation publishes it.</p>
</section>
"""


def as_sections(body):
    """Turn the rendered markdown into one card per section, with a list
    of contents beside them.

    A reference document read in one unbroken column is the wall of
    standing text the house style exists to prevent: on the runbook that
    was a single card eight thousand pixels tall, with every section
    heading looking like a line of bold text. Each section gets its own
    card and its own place in the contents list instead, so the page can
    be scanned and a section can be linked to.
    """
    parts = re.split(r"\[\[SECTION:([a-z0-9-]+):(.*?)\]\]", body)
    intro = parts[0].strip()
    out, index = [], []
    if intro:
        out.append(f'<section class="panel doc">\n{intro}\n</section>')
    for anchor, heading, content in zip(parts[1::3], parts[2::3], parts[3::3]):
        index.append(f'        <li><a href="#{anchor}">{heading}</a></li>')
        out.append(f'<section class="panel doc" id="{anchor}">\n<h2>{heading}</h2>\n'
                   f'{content.strip()}\n</section>')
    if not index:
        return "\n".join(out) or body
    return (f'<nav class="doc-index" aria-label="Contents">\n      <h2>Contents</h2>\n'
            f'      <ul>\n' + "\n".join(index) + '\n      </ul>\n    </nav>\n'
            f'    <div class="doc-body">\n' + "\n".join(out) + '\n    </div>')


def build_pages():
    built = date.today().strftime("%-d %B %Y")
    project_links = "\n".join(
        f'        <li><a href="{href}">{text}</a></li>' for href, text in PROJECT_LINKS)
    page_links = "\n".join(
        f'        <li><a href="{href}">{text}</a></li>' for href, text in PAGE_LINKS)
    for slug, source, title, description, standfirst in DOCS:
        text = (ROOT / source).read_text()
        if source == "LICENSE":
            body = ('<div class="doc-body">' + LICENCE_INTRO
                    + '<section class="panel doc"><pre class="licence">'
                    + html.escape(text) + "</pre></section></div>")
        else:
            body = as_sections(render(text))
        (SITES / slug).write_text(PAGE.format(
            title=title, description=description, standfirst=standfirst,
            slug=slug, site=SITE_URL, csp=CSP, brand=BRAND_SVG,
            foot_brand=FOOT_SVG, project_links=project_links, page_links=page_links,
            body=body, built=built, built_iso=date.today().isoformat(),
            oss_bar=oss_bar()))
        print(f"  {slug}")


def build_downloads():
    (SITES / "compose.yaml").write_text((ROOT / "compose.yaml").read_text())
    print("  compose.yaml")


def build_sitemap():
    pages = ["", "documentation.html", "runbook.html", "whats-new.html",
             "security.html", "licence.html", "privacy.html", "cookies.html",
             "terms.html", "accessibility.html", "complaints.html"]
    today = date.today().isoformat()
    urls = "\n".join(
        f"  <url><loc>{SITE_URL}/{p}</loc><lastmod>{today}</lastmod></url>" for p in pages)
    (SITES / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n</urlset>\n")
    print("  sitemap.xml")


if __name__ == "__main__":
    if not (ROOT / "compose.yaml").exists():
        sys.exit("run this from the repository root")
    print("pages:")
    build_pages()
    print("hand-written pages:")
    add_strip_to_hand_pages()
    print("downloads:")
    build_downloads()
    print("sitemap:")
    build_sitemap()
