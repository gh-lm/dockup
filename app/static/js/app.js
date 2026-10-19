/* Dockup front end - one window, hash routing, no frameworks. */
"use strict";

const CSRF = document.querySelector('meta[name="csrf"]').content;
const content = document.getElementById("content");
const versionsEl = document.getElementById("appFooter");

let stacksCache = [];
let liveSockets = [];
let dashboardDnsName = "";
// Dockup's own compose folder, as the host sees it. Empty unless the
// install put it somewhere the stacks folder cannot reach.
let dockupSelfDir = "";
// Set when a dashboard card's update dot is clicked: the update itself
// runs on the stack's own page so its output streams into the panel
// there, exactly like pressing Update, instead of happening silently
// behind a spinner on the card.
let pendingStackAction = null;

/* ---------- helpers ---------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", "X-CSRF": CSRF },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("signed out"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `Request failed (${res.status})`);
    err.confirm = !!data.confirm;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function closeLiveSockets() {
  liveSockets.forEach(ws => { try { ws.close(); } catch (e) {} });
  liveSockets = [];
}

function classifyLogLine(line) {
  if (/\b(error|err!|fatal|panic|exception|traceback|failed|failure)\b/i.test(line)) return "log-error";
  if (/\b(warn|warning|deprecated)\b/i.test(line)) return "log-warn";
  return "";
}

function appendLog(view, line) {
  const div = document.createElement("div");
  const cls = classifyLogLine(line);
  if (cls) div.className = cls;
  div.textContent = line;
  const stick = view.scrollTop + view.clientHeight >= view.scrollHeight - 40;
  view.appendChild(div);
  while (view.childElementCount > 2000) view.firstElementChild.remove();
  if (stick) view.scrollTop = view.scrollHeight;
}

/* Copy button for a log panel. Reading a log is one thing; getting it
   out of the browser to paste into a bug report meant dragging a
   selection over hundreds of scrolling lines. The button sits in the
   panel's own top-right corner rather than on a row above it, so it
   stays put while the log scrolls and costs no height. */
function attachLogCopy(view, what = "log") {
  const wrap = document.createElement("div");
  wrap.className = "log-wrap";
  view.parentNode.insertBefore(wrap, view);
  wrap.appendChild(view);
  const btn = el(`<button class="btn log-copy" type="button"
    data-tip="Copy the whole ${what} to the clipboard"
    aria-label="Copy ${what}">${ICONS.copy}<span>Copy</span></button>`);
  wrap.appendChild(btn);
  btn.addEventListener("click", async () => {
    const lines = Array.from(view.children, n => n.textContent);
    if (!lines.length) return popAlert(btn, "Nothing to copy yet.", "warning");
    try {
      await copyText(lines.join("\n"));
      popAlert(btn, `Copied ${lines.length} line${lines.length === 1 ? "" : "s"}.`, "success");
    } catch (err) {
      popAlert(btn, "The browser wouldn't allow copying.", "danger");
    }
  });
  return btn;
}

function copyText(text) {
  // navigator.clipboard only exists in a secure context - true over
  // Tailscale Serve's HTTPS, false on a plain-http LAN address, where
  // the old hidden-textarea trick is still the only way.
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  return new Promise((resolve, reject) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.readOnly = true;
    ta.className = "copy-helper";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    ok ? resolve() : reject(new Error("copy blocked"));
  });
}

/* A specific, actionable explanation for Docker's "address already in
   use" error when the real cause is Tailscale Serve still holding a
   port from a deleted-and-recreated stack - rendered as a distinct
   alert box instead of leaving the user to decode raw stderr. */
function renderPortConflictHint(view, port, companionAvailable) {
  const box = el(`<div class="alert alert-warning port-conflict-hint">
    <p><strong>Port ${port} is stuck.</strong> Tailscale Serve is still holding it open from a
    previous version of this stack, so Docker can't bind it.</p>
    ${companionAvailable
      ? `<p>The dockup-companion should clear this automatically - if you're seeing this anyway,
         check <a href="#/settings">Settings → Host OS & Tailscale</a> that it's still running.</p>`
      : `<p>Fix it now: <code>sudo tailscale serve --https=${port} off</code> on the host, then try
         again. Restore it afterward with
         <code>sudo tailscale serve --bg --https=${port} http://127.0.0.1:${port}</code>.</p>
         <p>Or install the <a href="#/settings">dockup-companion</a> once and Dockup handles this
         automatically from now on.</p>`}
  </div>`);
  view.appendChild(box);
  view.scrollTop = view.scrollHeight;
}

/* A small, dismissible floating panel for narrating a long-running
   background action (e.g. installing the companion) - pinned to the
   viewport, closable at any time via the X, independent of whatever
   else is on screen. Only one at a time; a new one replaces the last. */
function openProgressPanel(title) {
  document.querySelectorAll(".progress-panel").forEach(p => p.remove());
  const panel = el(`<div class="progress-panel">
    <div class="progress-panel-head">
      <span class="status-dot warning" id="progressDot"></span>
      <span class="title">${esc(title)}</span>
      <button class="icon-btn" id="progressClose" aria-label="Dismiss" title="Dismiss">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>
      </button>
    </div>
    <div class="progress-panel-body log-view" id="progressBody"></div>
  </div>`);
  document.body.appendChild(panel);
  panel.querySelector("#progressClose").addEventListener("click", () => panel.remove());
  const body = panel.querySelector("#progressBody");
  const dot = panel.querySelector("#progressDot");
  return {
    line(text) { if (panel.isConnected) appendLog(body, text); },
    done(ok) { if (panel.isConnected) dot.className = "status-dot " + (ok ? "running" : ""); },
    closed() { return !panel.isConnected; },
    close() { panel.remove(); },
  };
}

/* Anything that cannot be undone asks twice instead of opening a
   window: the first click arms the button, which fills with the
   highlight colour and shows a tick, and a second click within three
   seconds does it. No second click and it stands back down. The tick
   is the question, not the answer: it asks "are you sure", it does not
   say it worked.

   navigate: false keeps you on the page after a success - for removing
   one row from a list, like a saved registry on Settings, rather than
   deleting the thing the page is about. */
function armedAction(btn, run, label, { navigate = true } = {}) {
  let armed = null;
  const resting = btn.innerHTML;
  const standDown = () => {
    btn.classList.remove("confirming");
    btn.innerHTML = resting;
    btn.dataset.tip = btn.dataset.tipOrig;
    armed = null;
  };
  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    if (!armed) {
      btn.classList.add("confirming");
      btn.innerHTML = ICONS.tick;
      btn.dataset.tip = `Click again to ${label}`;
      armed = setTimeout(standDown, 3000);
      return;
    }
    clearTimeout(armed); armed = null;
    btn.disabled = true;
    try {
      await run();
      // It stays filled: that is the state, not a message.
      if (navigate) setTimeout(() => location.hash = "#/", 700);
    } catch (e) {
      // A failure goes red, then back to resting so it can be tried
      // again.
      btn.disabled = false;
      btn.classList.remove("confirming");
      btn.classList.add("failed");
      setTimeout(() => { btn.classList.remove("failed"); standDown(); }, 1200);
      popAlert(btn, e.message, "danger");
    }
  });
}

/* ---------- labels on pointing ----------
   Any element with a data-tip attribute gets a small label on hover, on
   keyboard focus, or on tap: a word or two naming the thing, never an
   explanation. One shared element lives on the body and is placed by
   measuring the target: just below it by default, flipped above when
   there is no room, and shifted sideways so it never leaves the
   viewport. A pseudo-element per button couldn't do any of that.
   It floats over whatever is underneath, neighbouring buttons
   included, and never pushes anything aside. */
const tip = { el: null, target: null, timer: null, life: null, observer: null };

function tipEl() {
  if (!tip.el) {
    tip.el = document.createElement("div");
    tip.el.className = "tip-bubble";
    tip.el.setAttribute("role", "tooltip");
    document.body.appendChild(tip.el);
  }
  return tip.el;
}

function placeTip() {
  const target = tip.target;
  if (!target || !document.contains(target)) return hideTip();
  const b = tipEl();
  const r = target.getBoundingClientRect();
  // clientWidth, not innerWidth: the latter includes a scrollbar the
  // bubble can't actually sit on top of
  const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
  const margin = 8, gap = 10;
  // Measure from the top-left corner, where the bubble has the whole
  // viewport to lay itself out in, so the width is its natural one.
  b.style.left = "0px";
  b.style.top = "0px";
  const w = b.offsetWidth, h = b.offsetHeight;
  // Below the button by default; above only when there is no room
  // below and there is room above.
  let below = !(r.bottom + gap + h > vh - margin && r.top - gap - h >= margin);
  const top = below ? r.bottom + gap : r.top - gap - h;
  const centre = r.left + r.width / 2;
  const left = Math.max(margin, Math.min(centre - w / 2, vw - w - margin));
  b.style.left = `${Math.round(left)}px`;
  b.style.top = `${Math.round(top)}px`;
}

function showTip(target, life = TIP_LIFE) {
  clearTimeout(tip.timer);
  clearTimeout(tip.life);
  const text = target?.dataset.tip;
  if (!text) return hideTip();
  const b = tipEl();
  if (tip.observer) tip.observer.disconnect();
  tip.target = target;
  b.textContent = text;
  // A label is a word or two and stays on one line. Anything longer is
  // a message, so it wraps and keeps the softer corners.
  b.classList.toggle("wrap", text.length > 28);
  placeTip();
  b.classList.add("show");
  // It goes after a second and a half even while the pointer stays
  // put. The full name is always in aria-label, so a screen reader
  // loses nothing.
  if (life > 0) tip.life = setTimeout(hideTip, life);
  // A target whose text changes while it's showing (the update dot
  // moving through its states, an alert replacing hover help) keeps
  // the bubble current.
  tip.observer = new MutationObserver(() => {
    if (tip.target === target) {
      if (!target.dataset.tip) return hideTip();
      b.textContent = target.dataset.tip;
      placeTip();
    }
  });
  tip.observer.observe(target, { attributes: true, attributeFilter: ["data-tip"] });
}

function hideTip() {
  clearTimeout(tip.timer);
  clearTimeout(tip.life);
  tip.timer = null;
  tip.life = null;
  if (tip.observer) tip.observer.disconnect();
  tip.observer = null;
  tip.target = null;
  if (tip.el) tip.el.classList.remove("show");
}

// It appears once the pointer has rested on the button, so it does not
// flash up while sweeping across, and goes again shortly after.
const TIP_DELAY = 400, TIP_LIFE = 1500;
document.addEventListener("mouseover", (e) => {
  const target = e.target.closest?.("[data-tip]");
  if (!target || target === tip.target) return;
  if (target._alertUntil > Date.now()) return;
  clearTimeout(tip.timer);
  tip.timer = setTimeout(() => showTip(target), TIP_DELAY);
});
document.addEventListener("mouseout", (e) => {
  const target = e.target.closest?.("[data-tip]");
  if (!target || target.contains(e.relatedTarget)) return;
  if (target._alertUntil > Date.now()) return;
  if (tip.target === target) hideTip(); else clearTimeout(tip.timer);
});
document.addEventListener("focusin", (e) => {
  const target = e.target.closest?.("[data-tip]");
  if (target && target.matches(":focus-visible")) showTip(target);
});
document.addEventListener("focusout", (e) => {
  const target = e.target.closest?.("[data-tip]");
  if (target && tip.target === target && !(target._alertUntil > Date.now())) hideTip();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTip(); });
document.addEventListener("scroll", () => { if (tip.target) placeTip(); }, true);
window.addEventListener("resize", () => { if (tip.target) placeTip(); });

/* A save that fails says so under the box it belongs to, in the
   server's own words, and the box's edge turns red until it is put
   right. Nothing is said when it works: the glow is the answer. */
function fieldError(box, message) {
  const field = box.closest(".field") || box.parentElement;
  let line = field.querySelector(".field-error");
  if (!message) {
    line?.remove();
    box.classList.remove("has-error");
    box.removeAttribute("aria-invalid");
    return;
  }
  if (!line) {
    line = document.createElement("p");
    line.className = "field-error";
    line.setAttribute("role", "alert");
    field.appendChild(line);
  }
  line.textContent = message;
  box.classList.add("has-error");
  box.setAttribute("aria-invalid", "true");
}

/* A save is a glow on the thing that was saved, and nothing else: no
   tick, no word, no banner, nothing that pushes the page about. Call
   it on the box that was edited where there is one, or on the panel
   holding what was written. */
function glow(el) {
  if (!el) return;
  const field = el.matches?.("input, textarea, select");
  const cls = field ? "just-saved" : "just-saved-plain";
  el.classList.remove(cls);
  // Reading offsetWidth restarts the animation when the same box is
  // saved twice in a row; without it the class is already there and
  // nothing happens the second time.
  void el.offsetWidth;
  el.classList.add(cls);
  clearTimeout(el._glowTimer);
  el._glowTimer = setTimeout(() => el.classList.remove(cls), 1700);
}

/* The one mechanism for contextual alerts: the label, forced open on the
   element the message is about, with the message in place of its usual
   hover text, which is restored when the alert fades. Never a toast lost
   at the bottom of the page. */
function popAlert(el, message, kind = "info", ms = 3500) {
  if (!el) return;
  // kind (danger/success/warning/info) is accepted so callers stay
  // self-documenting, but does not change the label's colour: it is the
  // same dark pill whatever the news, so it reads over anything.
  // Capture the element's resting tooltip (its normal hover text, or "")
  // only on the first call of a sequence, so a caller that changes the
  // resting tooltip mid-flow isn't clobbered back to a stale value when
  // this fades.
  if (!el._tipTimer) el._tipRestingValue = el.dataset.tip ?? "";
  el.dataset.tip = message;
  el._alertUntil = Date.now() + ms;
  // Its own life, not a label's second and a half.
  showTip(el, ms);
  clearTimeout(el._tipTimer);
  el._tipTimer = setTimeout(() => {
    el.dataset.tip = el._tipRestingValue;
    el._tipTimer = null;
    el._alertUntil = 0;
    if (tip.target === el) hideTip();
  }, ms);
}

/* Code editor: wraps a plain textarea with CodeMirror so every text
   field in the app (compose YAML, .env) shares the same look - dark,
   monospace, line numbers. YAML mode additionally debounces calls to
   the server's own compose validator (the single source of truth for
   "is this valid compose", already used on save) for inline feedback. */
function attachCodeEditor(textareaEl, { mode = null, validate = false, getName = null, getEnv = null } = {}) {
  const frame = document.createElement("div");
  frame.className = "editor-frame";
  textareaEl.parentNode.insertBefore(frame, textareaEl);
  frame.appendChild(textareaEl);

  // The CM6 bundle (static/vendor/codemirror6) is built once with esbuild
  // from the real npm packages - see LICENSES.txt for versions. onChange is
  // wired up below rather than passed in here, so `check` (defined further
  // down, and needing `cm` for getValue()) can close over it.
  let onChangeCb = null;
  const view = DockupEditor.makeEditor(textareaEl, { mode, onChange: () => onChangeCb && onChangeCb() });
  const cm = {
    getValue: () => view.state.doc.toString(),
    setValue: (text) => view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } }),
    on: (event, cb) => { if (event === "change") onChangeCb = cb; },
  };

  if (!validate) return cm;

  cm.recheck = () => check();

  const status = document.createElement("div");
  status.className = "editor-status";
  status.setAttribute("role", "status");
  status.innerHTML = '<span class="dot"></span><span class="msg">Checking…</span>';
  frame.appendChild(status);

  let timer, requestSeq = 0;
  const setStatus = (cls, msg) => {
    status.className = "editor-status " + cls;
    status.querySelector(".msg").textContent = msg;
  };
  const check = async () => {
    const seq = ++requestSeq;
    const text = cm.getValue();
    if (!text.trim()) { setStatus("", "Empty"); return; }
    const body = { compose: text };
    if (getName) body.name = getName();
    if (getEnv) body.env = getEnv();
    try {
      const res = await api("/api/validate", { method: "POST", body });
      if (seq !== requestSeq) return; // a newer edit already superseded this check
      if (!res.ok) { setStatus("bad", res.error); return; }
      if (res.portConflicts && res.portConflicts.length) {
        const msg = res.portConflicts
          .map(c => `port ${c.port} is already used by '${c.with}'`).join("; ");
        setStatus("warn", `Looks good, but ${msg}`);
      } else {
        setStatus("ok", "Looks good");
      }
    } catch (e) {
      if (seq === requestSeq) setStatus("", "Couldn't check right now");
    }
  };
  cm.on("change", () => { clearTimeout(timer); timer = setTimeout(check, 600); });
  check();
  return cm;
}

function attachYamlEditor(textareaEl, opts = {}) {
  return attachCodeEditor(textareaEl, { mode: "yaml", validate: true, ...opts });
}

const ICONS = {
  play: '<svg viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="6.5" y="6.5" width="11" height="11" rx="1.5" fill="currentColor"/></svg>',
  restart: '<svg viewBox="0 0 24 24"><path d="M19 12a7 7 0 1 1-2.05-4.95M19 4v4h-4" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  redeploy: '<svg viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="2.5" stroke="currentColor" stroke-width="2" fill="none"/><path d="M9.5 12a2.7 2.7 0 0 1 4.5-2M14.5 12a2.7 2.7 0 0 1-4.5 2" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/></svg>',
  update: '<svg viewBox="0 0 24 24"><path d="M12 4v9m0 0l-3.5-3.5M12 13l3.5-3.5M5 17v1a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-1" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2" fill="none"/><path d="M12 11v5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="7.75" r="1.1" fill="currentColor"/></svg>',
  archive: '<svg viewBox="0 0 24 24"><path d="M3 7h18v3H3zM5 10v9h14v-9M10 14h4" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  bin: '<svg viewBox="0 0 24 24"><path d="M5 7h14M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m2 0l-.8 12a2 2 0 0 1-2 1.9H9.8a2 2 0 0 1-2-1.9L7 7m3 4v6m4-6v6" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="M4 9l8 7 8-7" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  tick: '<svg viewBox="0 0 24 24"><path d="M5 13l4.5 4.5L19 8" stroke="currentColor" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  external: '<svg viewBox="0 0 24 24"><path d="M14 5h5v5M19 5l-8 8M8 5H6a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2" stroke="currentColor" stroke-width="1.8" fill="none"/><path d="M15 5.5A1.5 1.5 0 0 0 13.5 4H6a2 2 0 0 0-2 2v7.5A1.5 1.5 0 0 0 5.5 15" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round"/></svg>',
  checkUpdate: '<svg viewBox="0 0 24 24"><path d="M7 18h9.5a3.5 3.5 0 0 0 .5-6.96 5 5 0 0 0-9.71-1.79A4 4 0 0 0 7 18Z" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linejoin="round"/><path d="M9 12.5l1.8 1.8L15 10" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

/* ---------- engine ---------- */

async function refreshStacks() {
  try {
    const data = await api("/api/stacks");
    stacksCache = data.stacks;
    dashboardDnsName = data.dnsName || "";
    dockupSelfDir = data.selfDir || "";
  } catch (e) {
    // Callers treat this as best-effort; an unreachable engine is
    // reported via the footer's Docker row instead.
  }
}

/* The footer: a Lightmorphic badge, then the version numbers (Dockup's
   and Docker's). A number that is printed is the number, so there is no
   tick beside it. */
async function renderVersions() {
  if (!versionsEl) return;
  let v;
  try { v = await api("/api/system/versions"); }
  catch (e) {
    versionsEl.innerHTML = `<div class="version-row bad">${esc(e.message)}</div>`;
    return;
  }
  const rows = [];

  // Whether there is a newer Dockup is answered the same way as for
  // every other stack: on Dockup's own card in the stack list.
  rows.push(`<a class="lm-badge" href="https://lightmorphic.com" target="_blank" rel="noopener"
    data-tip="Lightmorphic - the studio behind Dockup">
    <span class="lm-badge-label">Created by</span>
    <svg class="lm-badge-mark" viewBox="0 0 1000 1000" aria-hidden="true">
      <g transform="scale(1.3986014)">
        <rect x="0" y="0" width="715" height="715" rx="157" ry="157" fill="#fbc711"/>
        <path fill="#000000" d="m 405,82 -4,1 -7,27 -12,25 -34,59 -16,41 -6,28 -1,34 3,18 6,15 -1,3 -22,-24 -19,-36 -11,-43 -9,-85 -7,-23 -11,-21 -4,-1 -53,30 18,42 14,62 8,25 16,34 19,27 15,15 15,11 19,9 v 3 h -14 l -22,-4 -29,-10 -40,-23 -68,-51 -19,-10 -18,-6 -13,-1 -16,55 1,5 35,11 85,47 36,13 32,6 h 26 l 21,-4 12,-5 3,1 -15,16 -13,10 -39,20 -31,9 -85,15 -24,9 -17,10 -4,6 32,52 30,-17 79,-25 49,-24 16,-11 22,-21 15,-21 7,-17 h 3 l -3,33 -10,31 -23,41 -50,66 -10,18 -9,26 -1,7 2,2 55,16 5,-2 1,-9 9,-24 38,-67 21,-51 7,-36 v -26 l -3,-18 -6,-15 1,-3 22,24 18,34 12,48 9,83 5,18 10,21 6,6 54,-30 -17,-40 -17,-72 -18,-46 -15,-24 -19,-21 -22,-16 -17,-7 v -3 l 28,2 34,10 47,27 62,47 18,10 27,9 h 8 l 16,-55 -1,-5 -28,-8 -64,-36 -32,-16 -27,-10 -27,-6 -36,-1 -36,8 25,-24 39,-21 34,-10 82,-14 23,-8 18,-10 7,-8 -32,-52 -34,19 -77,24 -49,24 -19,14 -19,19 -12,17 -8,19 h -3 l 2,-28 12,-38 22,-39 48,-63 14,-27 7,-28 z"/>
      </g>
    </svg>
    <span class="lm-badge-name">Lightmorphic</span>
    <span class="lm-badge-ext">${ICONS.external}</span></a>`);
  rows.push('<span class="spacer"></span>');

  // The version numbers, along the foot: Dockup's own, then the engine
  // it is talking to. No tick beside either - a number that is printed
  // is the number, and a tick saying so adds nothing.
  rows.push(`<div class="version-row">
    <span class="version-name">Dockup</span>
    <span class="version-num">${esc(v.dockup?.version || "")}</span></div>`);
  const k = v.docker || {};
  rows.push(`<div class="version-row ${k.ok ? "" : "bad"}" data-tip="${esc(k.ok ? "Connected" : (k.error || "Engine unreachable"))}">
    <span class="version-name">${esc(k.engine || "Docker")}</span>
    <span class="version-num">${esc(k.version || "unreachable")}</span></div>`);

  versionsEl.innerHTML = rows.join("");
}

/* ---------- router ---------- */

const routes = [
  [/^#?\/?$/, viewDashboard],
  [/^#\/new$/, viewNewStack],
  [/^#\/stack\/([^/]+)$/, (m) => viewStack(decodeURIComponent(m[1]))],
  [/^#\/maintenance$/, viewMaintenance],
  [/^#\/activity$/, viewActivity],
  [/^#\/backups$/, viewBackups],
  [/^#\/settings$/, viewSettings],
  [/^#\/help$/, viewHelp],
];

async function route() {
  closeLiveSockets();
  hideTip();
  document.querySelectorAll(".topbar-navlink").forEach(a =>
    a.classList.toggle("active", location.hash.startsWith(a.getAttribute("href"))));
  const hash = location.hash || "#/";
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) { await fn(m); content.focus({ preventScroll: true }); return; }
  }
  location.hash = "#/";
}

/* Every screen opens with one plain sentence saying how things stand,
   so somebody who reads nothing else has still been told. The dot
   carries the colour, with the same meanings as the update dot: green
   all well, the highlight colour for something waiting, blue for ready,
   red for a failure. A page that only explains what it is has no news
   to colour, so it shows its own icon in a circle instead and the
   sentence is normal size. */
function headlineHtml(text, { dot = null, icon = null } = {}) {
  // The colour comes from a class, not a style attribute: the app's
  // content security policy blocks inline styles outright.
  const mark = dot
    ? `<span class="dot ${dot}"></span>`
    : `<span class="head-icon">${icon || ICONS.info}</span>`;
  return `<section class="panel headline${dot ? "" : " plain"}" aria-live="polite">
    ${mark}<p>${esc(text)}</p></section>`;
}

function headlineEl(text, opts = {}) {
  return el(headlineHtml(text, opts));
}

/* The same words again do nothing. New words fade over the old ones so
   the sentence never flickers. */
function setHeadline(box, text) {
  const p = box?.querySelector("p");
  if (!p || p.textContent === text) return;
  p.textContent = text;
  p.classList.remove("fading");
  void p.offsetWidth;
  p.classList.add("fading");
}

/* How the stacks are doing, in one sentence. Updates waiting come
   first: they are the one thing somebody has to do something about. */
function dashboardHeadline() {
  const managed = stacksCache.filter(s => s.managed || s.isSelf);
  const waiting = managed.filter(s => s.updateAvailable).length;
  const running = managed.filter(s => s.status === "running").length;
  const stopped = managed.length - running;
  if (!managed.length) {
    return headlineEl("No stacks yet. Press New stack to make your first one.");
  }
  if (waiting) {
    return headlineEl(
      `${waiting} stack${waiting === 1 ? " has" : "s have"} a newer image waiting.`,
      { dot: "waiting" });
  }
  if (stopped) {
    return headlineEl(
      stopped === managed.length
        ? `Nothing is running: all ${managed.length} stacks are stopped.`
        : `${stopped} of ${managed.length} stacks ${stopped === 1 ? "is" : "are"} stopped.`,
      { dot: stopped === managed.length ? "quiet" : "busy" });
  }
  return headlineEl(
    managed.length === 1 ? "The one stack is running." : `All ${managed.length} stacks are running.`,
    { dot: "good" });
}

/* ---------- our own dropdown ----------
   Never the browser's own open list: that one is drawn by the system
   and cannot be styled, so it arrives as a white strip of system font
   in the middle of a dark page. The real <select> stays in the page,
   hidden from sight but not from forms or screen readers, and keeps
   being the thing that holds the value and fires "change" - so
   autosave and everything else carry on working untouched. */
function enhanceSelect(sel) {
  if (!sel || sel.dataset.enhanced) return;
  sel.dataset.enhanced = "1";
  const wrap = document.createElement("div");
  wrap.className = "picker";
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  sel.classList.add("picker-native");

  const box = el(`<button type="button" class="picker-box" aria-haspopup="listbox" aria-expanded="false">
    <span class="picker-value"></span>
    <svg class="picker-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"
      stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
  </button>`);
  const list = el('<div class="picker-list" role="listbox" hidden></div>');
  wrap.append(box, list);
  // The box's accessible name is its label's text. (A reference to an id
  // the label never had gave it no name at all.)
  const label = sel.id ? document.querySelector(`label[for="${sel.id}"]`) : null;
  box.setAttribute("aria-label", label ? label.textContent.trim() : sel.getAttribute("aria-label") || "");

  const options = [...sel.options];
  options.forEach((o, i) => {
    const row = el(`<div class="picker-option" role="option" tabindex="-1">${esc(o.textContent)}</div>`);
    row.addEventListener("click", () => { pick(i); close(); box.focus(); });
    list.appendChild(row);
  });
  const rows = [...list.children];

  function paint() {
    box.querySelector(".picker-value").textContent = sel.options[sel.selectedIndex]?.textContent ?? "";
    rows.forEach((r, i) => {
      const on = i === sel.selectedIndex;
      r.classList.toggle("chosen", on);
      r.setAttribute("aria-selected", String(on));
    });
  }
  function pick(i) {
    if (i < 0 || i >= options.length || i === sel.selectedIndex) return paint();
    sel.selectedIndex = i;
    paint();
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function open() {
    list.hidden = false;
    box.setAttribute("aria-expanded", "true");
    rows[sel.selectedIndex]?.focus();
  }
  function close() {
    list.hidden = true;
    box.setAttribute("aria-expanded", "false");
  }

  box.addEventListener("click", () => (list.hidden ? open() : close()));
  // Arrows move, Enter picks, Escape closes and focus goes back to the
  // box, which is what a native select does and what a keyboard user
  // will try first.
  list.addEventListener("keydown", (e) => {
    const here = rows.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      rows[Math.min(rows.length - 1, Math.max(0, here + (e.key === "ArrowDown" ? 1 : -1)))]?.focus();
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault(); pick(here); close(); box.focus();
    } else if (e.key === "Escape") {
      e.preventDefault(); close(); box.focus();
    }
  });
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) close(); });
  sel.addEventListener("change", paint);
  paint();
}

/* ---------- views ---------- */

async function viewDashboard() {
  content.innerHTML = `<div class="panel-head"><h1>Stacks</h1></div>`;
  await refreshStacks();
  content.appendChild(dashboardHeadline());

  content.appendChild(el(`<div class="btn-row dash-actions">
    <a class="icon-btn" href="#/" data-tip="All stacks" aria-label="All stacks: back to the dashboard">
      <svg viewBox="0 0 24 24"><path d="M12 3 3 8l9 5 9-5-9-5Z M3 12l9 5 9-5 M3 16l9 5 9-5" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </a>
    <a class="btn btn-primary" href="#/new" id="newStackBtn">
      <svg viewBox="0 0 24 24" class="btn-ico"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>
      New stack
    </a>
    <span class="spacer"></span>
    <button class="icon-btn" id="checkUpdatesBtn" data-tip="Check for updates"
      aria-label="Check every stack for a newer image right now, instead of waiting for the next automatic pass">${ICONS.checkUpdate}</button>
  </div>`));
  document.getElementById("checkUpdatesBtn")?.addEventListener("click", checkUpdatesNow);

  let discovered = { projects: [], standalone: [] };
  try { discovered = await api("/api/discover"); } catch (e) { /* non-fatal */ }

  const managed = stacksCache.filter(s => s.managed || s.isSelf);
  const unmanagedCount = discovered.projects.length + discovered.standalone.length;
  if (!managed.length && !unmanagedCount) {
    content.appendChild(el(`<div class="panel"><h3>Nothing here yet</h3>
      <p>Create your first stack with <strong>New stack</strong>.</p></div>`));
    return;
  }

  const updatable = managed.filter(s => s.updateAvailable);
  if (updatable.length) {
    const panel = el(`<div class="panel"><div class="panel-head mb-0">
      <h3>${updatable.length} update${updatable.length === 1 ? "" : "s"} available</h3>
      <span class="hint" data-tip="Pulls the newest image and redeploys each one, one after another">${esc(updatable.map(s => s.name).join(", "))}</span>
      <span class="spacer"></span>
      <button class="btn btn-primary" id="updateAllBtn">Update all</button>
    </div></div>`);
    content.appendChild(panel);
    panel.querySelector("#updateAllBtn").addEventListener("click", updateAll);
  }

  let onboarding = { offerBulkAdopt: false };
  try { onboarding = await api("/api/onboarding"); } catch (e) { /* non-fatal */ }

  if (onboarding.offerBulkAdopt && unmanagedCount > 0) {
    content.appendChild(renderAdoptPanel({ firstRun: true, count: unmanagedCount }));
  } else if (unmanagedCount > 1) {
    content.appendChild(renderAdoptPanel({ firstRun: false, count: unmanagedCount }));
  }

  const grid = el('<div class="stack-grid" id="grid"></div>');
  content.appendChild(grid);
  const statusByName = Object.fromEntries(stacksCache.map(s => [s.name, s.status]));
  for (const s of managed) grid.appendChild(managedCard(s));
  for (const p of discovered.projects) grid.appendChild(unmanagedCard(p, statusByName[p.name] || "running"));
  for (const c of discovered.standalone) grid.appendChild(standaloneCard(c));

  await renderArchivedSection();
}

async function renderArchivedSection() {
  let archived = { stacks: [] };
  try { archived = await api("/api/archived"); } catch (e) { /* non-fatal */ }
  if (!archived.stacks.length) return;

  const panel = el(`<div class="panel">
    <div class="panel-head"><h3>Archived (${archived.stacks.length})</h3><span class="spacer"></span>
      <button class="btn" id="archivedToggle">Show</button></div>
    <div id="archivedBody" class="hidden"></div>
  </div>`);
  content.appendChild(panel);
  const toggle = panel.querySelector("#archivedToggle");
  const body = panel.querySelector("#archivedBody");
  toggle.addEventListener("click", () => {
    const showing = !body.classList.contains("hidden");
    body.classList.toggle("hidden", showing);
    toggle.textContent = showing ? "Show" : "Hide";
  });

  for (const name of archived.stacks) {
    const row = el(`<div class="check-row archived-row">
      <span>${esc(name)}</span><span class="spacer"></span>
      <button class="icon-btn" id="restoreBtn" data-tip="Restore" aria-label="Restore this stack">${ICONS.restart}</button>
      <button class="icon-btn" id="purgeBtn" data-tip="Delete" aria-label="Delete this archived stack - asks twice">${ICONS.bin}</button>
    </div>`);
    body.appendChild(row);
    row.querySelector("#restoreBtn").addEventListener("click", async (e) => {
      e.target.disabled = true; e.target.classList.add("busy");
      try {
        await api(`/api/archived/${encodeURIComponent(name)}/restore`, { method: "POST", body: {} });
        popAlert(e.target, `'${name}' restored.`, "success");
        await refreshStacks();
        await new Promise(r => setTimeout(r, 900)); // let the confirmation actually be seen before the view changes
        viewDashboard();
      } catch (err) { popAlert(e.target, err.message, "danger"); e.target.disabled = false; e.target.classList.remove("busy"); }
    });
    const purgeBtn = row.querySelector("#purgeBtn");
    purgeBtn.dataset.tipOrig = "Delete";
    armedAction(purgeBtn, async () => {
      const res = await api(`/api/archived/${encodeURIComponent(name)}/purge`, { method: "POST", body: {} });
      popAlert(purgeBtn, res.removedImages.length ? `'${name}' deleted, image(s) removed.` : `'${name}' deleted.`, "success");
      await new Promise(r => setTimeout(r, 900));
      viewDashboard();
    }, "permanently delete this archived stack and its image - nothing kept");
  }
}

async function checkUpdatesNow() {
  const btn = document.getElementById("checkUpdatesBtn");
  btn.disabled = true; btn.classList.add("busy");
  try {
    const res = await api("/api/stacks/check-updates", { method: "POST", body: {} });
    if (!res.started) {
      popAlert(btn, res.message || "A check is already running.", "info");
      btn.disabled = false; btn.classList.remove("busy");
      return;
    }
    // poll until the background check finishes, then reload with fresh badges
    for (;;) {
      await new Promise(r => setTimeout(r, 2000));
      const status = await api("/api/stacks/check-updates/status");
      if (!status.checking) break;
    }
    if ((location.hash || "#/") === "#/") viewDashboard();
  } catch (e) {
    popAlert(btn, e.message, "danger");
    btn.disabled = false; btn.classList.remove("busy");
  }
}

async function updateAll() {
  const btn = document.getElementById("updateAllBtn");
  btn.disabled = true; btn.textContent = "Updating…";
  try {
    const res = await api("/api/stacks/update-all", { method: "POST", body: {} });
    popAlert(btn, `Updated ${res.updated} of ${res.total}.`, res.updated === res.total ? "success" : "warning");
    await refreshStacks();
    viewDashboard();
  } catch (e) {
    btn.disabled = false; btn.textContent = "Update all";
    popAlert(btn, e.message, "danger");
  }
}

function renderAdoptPanel({ firstRun, count }) {
  const heading = firstRun ? "Welcome to Dockup" : `${count} thing${count === 1 ? "" : "s"} not adopted yet`;
  const blurb = firstRun
    ? `Found ${count} thing${count === 1 ? "" : "s"} already running on this system. Adopting copies each one's
       setup into the stacks folder so Dockup can manage it - nothing running is restarted or changed.`
    : `Copies each one's setup into the stacks folder so Dockup can manage it - nothing running is restarted.`;
  const panel = el(`<div class="panel">
    <div class="panel-head"><h3>${esc(heading)}</h3><span class="spacer"></span>
      <button class="btn btn-primary" id="adoptAllBtn">Adopt all</button>
      ${firstRun ? '<button class="btn" id="skipAdoptBtn">Not now</button>' : ""}</div>
    <p class="hint">${blurb} Skip anything another manager (like Arcane) already looks after - running two
    Docker managers over the same containers can fight over the same files.</p></div>`);
  panel.querySelector("#adoptAllBtn").addEventListener("click", () => adoptAll(firstRun));
  if (firstRun) {
    panel.querySelector("#skipAdoptBtn").addEventListener("click", async () => {
      try { await api("/api/onboarding/dismiss", { method: "POST", body: {} }); } catch (e) { /* ignore */ }
      viewDashboard();
    });
  }
  return panel;
}

async function adoptAll(dismissOnboarding) {
  const btn = document.getElementById("adoptAllBtn");
  btn.disabled = true; btn.textContent = "Adopting…";
  try {
    const res = await api("/api/adopt/all", { method: "POST", body: {} });
    if (dismissOnboarding) {
      try { await api("/api/onboarding/dismiss", { method: "POST", body: {} }); } catch (e) { /* ignore */ }
    }
    popAlert(btn, `Adopted ${res.adopted} of ${res.total}.`, res.adopted === res.total ? "success" : "warning");
    await refreshStacks();
    await new Promise(r => setTimeout(r, 900));
    location.hash = "#/";
    viewDashboard();
  } catch (e) {
    btn.disabled = false; btn.textContent = "Adopt all";
    popAlert(btn, e.message, "danger");
  }
}

const STATUS_TIPS = {
  running: "Container is running",
  update: "Update available - click to update",
  warning: "Warnings - check the log",
  partial: "Some containers are down",
  stopped: "Container is down",
  exited: "Container is down",
  inactive: "No container - ready to archive or delete",
};
// Three colors on a dashboard card: green (good), yellow (update
// ready - click it), red (everything else that isn't simply running,
// including health warnings - a single "something needs attention"
// signal rather than a separate shade for each cause). "inactive" (no
// container at all) is the one exception - not a problem, its own
// neutral gray.
const STATUS_DOT_CLASS = { running: "running", update: "update", inactive: "inactive" };

function cardDot(status) {
  const cls = STATUS_DOT_CLASS[status] || "";
  const tip = STATUS_TIPS[status] || "Container is down";
  return `<span class="status-dot ${cls}" data-tip="${esc(tip)}" tabindex="0" aria-label="${esc(tip)}"></span>`;
}

function managedCard(s) {
  const updateReady = !!s.updateAvailable;
  const effectiveStatus = updateReady ? "update" : s.status;
  const unpublished = s.unpublished || [];
  const dotTip = unpublished.length
    ? `Running, but port ${unpublished.join(", ")} isn't published - press Redeploy`
    : (STATUS_TIPS[effectiveStatus] || "Container is down");
  const port = s.ports && s.ports.length ? s.ports[0] : null;
  // Only offer to open it when something's actually running to answer -
  // the port is real either way (straight off the compose file), but a
  // link to a dead container is just a broken tab.
  const reachable = s.status === "running" || s.status === "partial";
  const webUrl = port && reachable
    ? (s.served && s.served.includes(port) && dashboardDnsName
        ? `https://${dashboardDnsName}:${port}`
        : `http://${location.hostname}:${port}`)
    : null;
  const inactive = s.status === "inactive";
  const label = `Open stack ${s.name} - ${dotTip}`;
  const card = el(`<div class="panel stack-card" role="link" tabindex="0" aria-label="${esc(label)}">
    <h3><span class="status-dot ${STATUS_DOT_CLASS[effectiveStatus] || ""}" data-tip="${esc(dotTip)}"
      ${updateReady ? 'role="button"' : ""} tabindex="0" aria-label="${esc(dotTip)}"></span><span class="card-name">${esc(s.name)}</span>${
      webUrl ? `<a class="icon-btn web-link" href="${esc(webUrl)}" target="_blank" rel="noopener" data-tip="Open web UI" aria-label="Open web UI">${ICONS.external}</a>` : ""}</h3>
    <span class="hint">${s.containers.length} container${s.containers.length === 1 ? "" : "s"}${port ? ` · port ${port}` : ""}${
      s.isSelf ? " · this is Dockup" : ""}</span>${
      s.isSelf && !s.managed ? `<p class="note">Dockup's own compose file is at
        <code>${esc(dockupSelfDir || "an unknown folder")}</code>, outside the stacks folder, so it
        cannot be read or changed from here. Move that folder into the stacks folder to manage
        Dockup like any other stack.</p>` : ""}
    ${inactive ? `<div class="btn-row card-actions">
      <button class="icon-btn" id="archiveBtn" data-tip="Archive" aria-label="Archive this stack">${ICONS.archive}</button>
      <button class="icon-btn" id="purgeBtn" data-tip="Delete" aria-label="Delete this stack and its containers - asks twice">${ICONS.bin}</button>
    </div>` : ""}
  </div>`);
  // The whole card is clickable and also holds nested interactive
  // elements (the web-UI link, Archive/Delete). Relying on those children
  // to call stopPropagation() is fragile, so check the actual click target
  // here: a click that began on an interactive child never opens the
  // stack page.
  const open = (e) => {
    if (e && e.target.closest('a, button, [role="button"]')) return;
    location.hash = `#/stack/${encodeURIComponent(s.name)}`;
  };
  card.addEventListener("click", open);
  card.addEventListener("keydown", e => {
    if (e.target.closest('a, button, [role="button"]')) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
  });

  const webLink = card.querySelector("a.icon-btn.web-link");
  if (webLink) {
    webLink.addEventListener("click", e => e.stopPropagation());
    webLink.addEventListener("keydown", e => e.stopPropagation());
  }

  if (updateReady) {
    const dot = card.querySelector(".status-dot");
    const runUpdate = (e) => {
      e.stopPropagation();
      dot.classList.add("busy");
      dot.dataset.tip = "Updating…";
      // The card has nowhere to show a pull's output, and an update is
      // exactly the thing you want to watch - hand off to the stack's
      // page, which starts the same streaming update on arrival.
      pendingStackAction = { name: s.name, action: "update" };
      location.hash = `#/stack/${encodeURIComponent(s.name)}`;
    };
    dot.addEventListener("click", runUpdate);
    dot.addEventListener("keydown", e => {
      e.stopPropagation();
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); runUpdate(e); }
    });
  }

  if (inactive) {
    const archiveBtn = card.querySelector("#archiveBtn");
    const purgeBtn = card.querySelector("#purgeBtn");
    // Both buttons sit inside the card's own "open on click/Enter/Space"
    // region - stop these events reaching that handler, or using them
    // navigates to the stack page instead of (or as well as) archiving/
    // deleting it.
    [archiveBtn, purgeBtn].forEach(b => b.addEventListener("keydown", e => e.stopPropagation()));
    archiveBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      archiveBtn.disabled = true; archiveBtn.textContent = "Archiving…";
      try {
        await api(`/api/stacks/${encodeURIComponent(s.name)}/archive`, { method: "POST", body: {} });
        popAlert(archiveBtn, `'${s.name}' archived.`, "success");
        await refreshStacks();
        await new Promise(res => setTimeout(res, 900));
        if ((location.hash || "#/") === "#/") viewDashboard();
      } catch (err) {
        archiveBtn.disabled = false; archiveBtn.textContent = "Archive";
        popAlert(archiveBtn, err.message, "danger");
      }
    });
    purgeBtn.dataset.tipOrig = "Delete";
    purgeBtn.addEventListener("click", e => e.stopPropagation());
    armedAction(purgeBtn, async () => {
      const res = await api(`/api/stacks/${encodeURIComponent(s.name)}/purge`, { method: "POST", body: {} });
      popAlert(purgeBtn, res.removedImages.length ? `'${s.name}' deleted, image(s) removed.` : `'${s.name}' deleted.`, "success");
      await refreshStacks();
      await new Promise(res => setTimeout(res, 900));
      if ((location.hash || "#/") === "#/") viewDashboard();
    }, "permanently delete this stack and its image - nothing kept");
  }
  return card;
}

function unmanagedCard(p, status) {
  const n = p.containers.length;
  const card = el(`<div class="panel stack-card">
    <h3>${cardDot(status)}<span class="card-name">${esc(p.name)}</span></h3>
    <span class="hint">${n} container${n === 1 ? "" : "s"}, not adopted</span>
    <button class="btn btn-block adopt-btn">Adopt</button>
  </div>`);
  card.querySelector(".adopt-btn").addEventListener("click", () => adopt({ kind: "project", ...p }, card));
  return card;
}

function standaloneCard(c) {
  const card = el(`<div class="panel stack-card">
    <h3>${cardDot(c.state)}<span class="card-name">${esc(c.name)}</span></h3>
    <span class="hint">${esc(c.image)}, not adopted</span>
    <button class="btn btn-block adopt-btn">Adopt</button>
  </div>`);
  card.querySelector(".adopt-btn").addEventListener("click", () => adopt({ kind: "container", name: c.name }, card));
  return card;
}

async function adopt(payload, card) {
  const btn = card.querySelector(".adopt-btn");
  btn.disabled = true; btn.textContent = "Adopting…";
  try {
    const res = await api("/api/adopt", { method: "POST", body: payload });
    popAlert(btn, `Adopted '${res.name}'. ${res.note}.`, "success");
    await refreshStacks();
    await new Promise(r => setTimeout(r, 900));
    location.hash = `#/stack/${encodeURIComponent(res.name)}`;
  } catch (e) {
    btn.disabled = false; btn.textContent = "Adopt";
    popAlert(btn, e.message, "danger");
  }
}

/* ---- new stack ---- */

const COMPOSE_TEMPLATE = `services:
  app:
    image: nginx:alpine
    container_name: my-app
    restart: unless-stopped
    ports:
      - "8001:80"
`;

function viewNewStack() {
  content.innerHTML = `${headlineHtml("Nothing is created until you press Create, so it is safe to paste and look.")}
    <div class="panel"><h1>New stack</h1>
    <div class="form-grid">
      <div class="field"><label for="stackName">Stack name</label>
        <input id="stackName" placeholder="e.g. jellyfin" autocomplete="off"
          pattern="[a-z0-9][a-z0-9_-]*">
        <span class="hint">Lowercase letters, numbers, dashes and underscores. This becomes the folder name in the stacks directory.</span></div>
      <div class="field"><label for="composeText">Compose file</label>
        <textarea id="composeText" class="code-editor" spellcheck="false">${esc(COMPOSE_TEMPLATE)}</textarea></div>
      <div class="field"><label for="envText">.env file <span class="hint">(optional)</span></label>
        <textarea id="envText" spellcheck="false" placeholder="KEY=value"></textarea></div>
      <button class="btn btn-primary" id="createBtn">Create stack</button>
    </div></div>
    <div class="panel"><h3>Convert a docker run command</h3>
    <p>Paste a <code>docker run …</code> command and it becomes compose YAML in the editor above.</p>
    <div class="form-grid">
      <div class="field"><label for="runCmd">docker run command</label>
        <textarea id="runCmd" spellcheck="false" placeholder="docker run -d -p 8080:80 --name web nginx:alpine"></textarea></div>
      <button class="btn" id="convertBtn">Convert to compose</button>
    </div></div>`;

  let envCm; // assigned below; referenced by cm's live port-conflict check
  const nameInput = document.getElementById("stackName");
  const cm = attachYamlEditor(document.getElementById("composeText"), {
    getName: () => nameInput.value.trim(),
    getEnv: () => envCm ? envCm.getValue() : "",
  });
  envCm = attachCodeEditor(document.getElementById("envText"));
  let envTimer;
  envCm.on("change", () => { clearTimeout(envTimer); envTimer = setTimeout(() => cm.recheck(), 600); });

  // Block disallowed characters at the source instead of complaining
  // later: anything typed or pasted that isn't lowercase/digit/-/_ just
  // never appears in the box (uppercase letters are folded to lowercase
  // rather than dropped, so pasting "Jellyfin" still gives "jellyfin").
  nameInput.addEventListener("input", () => {
    const cleaned = nameInput.value.toLowerCase().replace(/[^a-z0-9_-]/g, "");
    if (nameInput.value !== cleaned) {
      const pos = nameInput.selectionStart - (nameInput.value.length - cleaned.length);
      nameInput.value = cleaned;
      nameInput.setSelectionRange(Math.max(0, pos), Math.max(0, pos));
    }
    cm.recheck();
  });

  const convertBtn = document.getElementById("convertBtn");
  convertBtn.addEventListener("click", async () => {
    try {
      const res = await api("/api/convert", { method: "POST", body: { command: document.getElementById("runCmd").value } });
      cm.setValue(res.compose);
      popAlert(convertBtn, "Converted - check the compose file, then create the stack.", "success");
    } catch (e) { popAlert(convertBtn, e.message, "danger"); }
  });
  const createBtn = document.getElementById("createBtn");
  createBtn.addEventListener("click", async () => {
    const name = document.getElementById("stackName").value.trim();
    try {
      await api("/api/stacks", { method: "POST", body: {
        name, compose: cm.getValue(),
        env: envCm.getValue() } });
      popAlert(createBtn, `Stack '${name}' created.`, "success");
      await refreshStacks();
      await new Promise(r => setTimeout(r, 900));
      location.hash = `#/stack/${encodeURIComponent(name)}`;
    } catch (e) { popAlert(createBtn, e.message, "danger"); }
  });
}

/* ---- stack detail ---- */

/* Prefers the real Tailscale Serve URL (a stack's port served there
   over HTTPS, using the tailnet's actual name) and falls back to
   whatever host the browser is already using to reach Dockup itself -
   its LAN IP or hostname, whichever got the user here - over plain
   HTTP on the stack's own published port. No port at all means
   nothing to open, so the button stays hidden. */
async function resolveWebUiLink(name, linkEl) {
  let status;
  try { status = await api(`/api/hostcompanion/stacks/${encodeURIComponent(name)}/serve`); }
  catch (e) { return; }
  if (!status.ports || !status.ports.length) return;
  const port = status.ports[0];
  let url = `http://${location.hostname}:${port}`;
  if (status.available && status.served.includes(port)) {
    try {
      const host = await api("/api/hostcompanion/status");
      if (host.tailscale?.dnsName) url = `https://${host.tailscale.dnsName}:${port}`;
    } catch (e) { /* fall back to the plain-IP URL already set */ }
  }
  linkEl.href = url;
  linkEl.classList.remove("hidden");
}

// The port advisories below the title are rebuilt on their own after a
// compose save. Save alone doesn't re-render the page, so without this
// a fixed port would keep showing its old warning until the user
// navigated away and back.
function portAlertsHtml(s) {
  const unpublished = s.unpublished || [];
  const wideBind = s.wideBind || [];
  // The news itself is in the page's headline sentence. What is left
  // here is what to do about it, as plain words rather than a tinted
  // message box.
  return `${unpublished.length ? `<p class="note-bad">A restart won't fix it. Press Redeploy, which
      recreates the containers and re-establishes port
      ${esc(unpublished.join(", "))}.</p>` : ""}${wideBind.length ? `<p class="note">Recommended: change port
      ${esc(wideBind.join(", "))} to
      <code>127.0.0.1:${esc(String(wideBind[0]))}:&lt;container port&gt;</code> in the Compose tab,
      then redeploy to apply it. Tailscale Serve already fronts this stack, and while the port is
      open on every interface Serve cannot get its certificate and the stack will not start again
      once stopped. Leave it as it is only if you also reach this stack directly on your
      network.</p>` : ""}`;
}

/* How this one stack is doing, in a sentence. A port nobody can reach
   comes first, then an update waiting, then a port open too wide. */
function stackHeadlineText(s) {
  const unpublished = s.unpublished || [];
  const wideBind = s.wideBind || [];
  if (unpublished.length) {
    return { text: `Every container is running, but port ${unpublished.join(", ")} isn't published,`
      + " so nothing outside can reach this stack.", dot: "bad" };
  }
  if (s.updateAvailable) {
    return { text: "A newer image is waiting for this stack.", dot: "waiting" };
  }
  if (wideBind.length) {
    return { text: `This compose file opens port ${wideBind.join(", ")} on every interface,`
      + " not just this machine.", dot: "busy" };
  }
  if (s.status === "running") {
    const n = (s.containers || []).length;
    if (s.isSelf) {
      return { text: "Running. This stack is Dockup itself, so stopping it takes this screen with it.",
        dot: "good" };
    }
    return { text: n === 1 ? "Running, one container." : `Running, ${n} containers.`,
      dot: "good" };
  }
  return { text: "Stopped. Press Start to bring it up.", dot: "quiet" };
}

function statusDotTip(s) {
  const unpublished = s.unpublished || [];
  if (unpublished.length) {
    return `Running, but port ${unpublished.join(", ")} isn't published - press Redeploy`;
  }
  return STATUS_TIPS[s.updateAvailable ? "update" : s.status] || "Container is down";
}

async function refreshPortAlerts(name) {
  const box = document.getElementById("portAlerts");
  if (!box) return;
  let s;
  try { s = await api(`/api/stacks/${encodeURIComponent(name)}`); } catch (e) { return; }
  box.innerHTML = portAlertsHtml(s);
  const hl = stackHeadlineText(s);
  const headline = document.querySelector("#content .headline");
  setHeadline(headline, hl.text);
  const headDot = headline?.querySelector(".dot");
  if (headDot) headDot.className = `dot ${hl.dot}`;
  const dot = document.getElementById("stackStatusDot");
  if (dot) {
    const tip = statusDotTip(s);
    dot.dataset.tip = tip;
    dot.setAttribute("aria-label", tip);
  }
}

async function viewStack(name) {
  let s;
  try { s = await api(`/api/stacks/${encodeURIComponent(name)}`); }
  catch (e) { content.innerHTML = `<p class="alert alert-danger">! ${esc(e.message)}</p>`; return; }
  // The sidebar renders from stacksCache, which this fresh per-stack
  // fetch doesn't touch - without this, the sidebar/dashboard dot can
  // sit stale (e.g. still green) while this page's own dot already
  // shows yellow, right after a background check flips the flag.
  refreshStacks();

  content.innerHTML = "";
  const effectiveStatus = s.updateAvailable ? "update" : s.status;
  const dotTip = statusDotTip(s);
  const hl = stackHeadlineText(s);
  content.appendChild(headlineEl(hl.text, { dot: hl.dot }));
  const head = el(`<div class="panel"><div class="stack-head">
      <div class="stack-title-row">
        <h1 class="stack-title">${esc(name)}</h1>
        <a class="icon-btn hidden" id="openWebBtn" data-tip="Open web UI" aria-label="Open web UI"
          target="_blank" rel="noopener" href="#">${ICONS.external}</a>
      </div>
      <div class="panel-head stack-actions">
        <span class="status-dot ${STATUS_DOT_CLASS[effectiveStatus] || ""}" id="stackStatusDot"
          role="button" data-tip="${esc(dotTip)}" tabindex="0" aria-label="${esc(dotTip)}"></span>
        <button class="icon-btn" id="actStart" data-tip="Start" aria-label="Start stack">${ICONS.play}</button>
        <button class="icon-btn" id="actStop" data-tip="Stop" aria-label="Stop stack">${ICONS.stop}</button>
        <button class="icon-btn" id="actRestart" data-tip="Restart" aria-label="Restart stack">${ICONS.restart}</button>
        <button class="icon-btn" id="actRedeploy" data-tip="Redeploy (recreate containers - fixes a stuck one without pulling a new image)" aria-label="Redeploy stack">${ICONS.redeploy}</button>
        <button class="icon-btn" id="actUpdate" data-tip="Update (pull newest images)" aria-label="Update stack">${ICONS.update}</button>
        <button class="icon-btn" id="actDown" data-tip="Down (stop and remove containers)" aria-label="Take stack down">${ICONS.down}</button>
        <button class="icon-btn" id="actDelete" data-tip="Delete stack" aria-label="Delete stack">${ICONS.bin}</button>
      </div>
    </div>
    <div id="portAlerts">${portAlertsHtml(s)}</div>
    <div class="log-view action-output" id="actionOut" aria-live="polite"></div>
  </div>`);
  content.appendChild(head);
  attachLogCopy(head.querySelector("#actionOut"), "output");
  resolveWebUiLink(name, head.querySelector("#openWebBtn"));

  const statusDot = head.querySelector("#stackStatusDot");
  const checkTipDefault = "Check this stack for an update right now - click again once ready to update";
  const updateTipReady = "Update available - click to update";
  statusDot.dataset.tip = s.updateAvailable ? updateTipReady : checkTipDefault;
  statusDot.setAttribute("aria-label", statusDot.dataset.tip);

  // The dot IS the update control - no separate button, same as the
  // dashboard card. Not ready yet: click checks right now. Ready:
  // click runs the same streaming update the Update button does, so
  // its output shows line by line instead of a one-word verdict.
  function setStackStatus(effectiveStatus) {
    const ready = effectiveStatus === "update";
    const tip = ready ? updateTipReady : (STATUS_TIPS[effectiveStatus] || checkTipDefault);
    statusDot.className = `status-dot ${STATUS_DOT_CLASS[effectiveStatus] || ""}`;
    statusDot.dataset.tip = tip;
    statusDot.setAttribute("aria-label", tip);
  }

  async function doCheck() {
    if (statusDot.classList.contains("busy")) return;
    statusDot.classList.add("busy");
    try {
      const res = await api(`/api/stacks/${encodeURIComponent(name)}/check-update`, { method: "POST", body: {} });
      s.updateAvailable = res.available;
      if (res.available) {
        setStackStatus("update");
        popAlert(statusDot, updateTipReady, "warning");
      } else {
        setStackStatus(s.status);
        popAlert(statusDot, "You're on the latest version", "success");
      }
      refreshStacks();
    } catch (e) {
      popAlert(statusDot, e.message, "danger", 4000);
    } finally {
      statusDot.classList.remove("busy");
    }
  }

  const onDotActivate = () => {
    if (statusDot.classList.contains("busy")) return;
    if (s.updateAvailable) runAction("update"); else doCheck();
  };
  statusDot.addEventListener("click", onDotActivate);
  statusDot.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onDotActivate(); }
  });

  const tabs = el(`<div class="panel">
    <div class="tabs" role="tablist">
      <button data-tab="overview" class="active">Overview</button>
      <button data-tab="compose">Compose</button>
      <button data-tab="logs">Logs</button>
      <button data-tab="terminal">Terminal</button>
      <button data-tab="backup">Backup</button>
      <button data-tab="serve">Tailscale</button>
    </div>
    <div id="tabBody"></div></div>`);
  content.appendChild(tabs);
  const tabBody = tabs.querySelector("#tabBody");

  const out = head.querySelector("#actionOut");
  const runAction = (action) => streamAction(name, action, out);
  head.querySelector("#actStart").addEventListener("click", () => runAction(s.status === "inactive" ? "up" : "start"));
  // Stopping, taking down or deleting Dockup would end this very screen
  // with nothing left to bring it back, so they are not offered. The
  // server refuses them too.
  if (s.isSelf) {
    for (const id of ["#actStop", "#actDown", "#actDelete"]) head.querySelector(id)?.remove();
  }
  head.querySelector("#actStop")?.addEventListener("click", () => runAction("stop"));
  head.querySelector("#actRestart").addEventListener("click", () => runAction("restart"));
  head.querySelector("#actRedeploy").addEventListener("click", () => runAction("redeploy"));
  head.querySelector("#actUpdate").addEventListener("click", () => runAction("update"));
  head.querySelector("#actDown")?.addEventListener("click", () => runAction("down"));
  const delBtn = head.querySelector("#actDelete") || document.createElement("button");
  delBtn.dataset.tip = "Delete stack";
  const dataPaths = s.dataPaths || [];
  const deletePanel = el(`<div class="panel hidden" id="deletePanel">
    <p class="alert alert-danger">! Permanently delete '${esc(name)}'? This removes its containers, compose
      file, and Docker image(s). This cannot be undone.</p>
    ${dataPaths.length ? `<div class="check-row"><input type="checkbox" id="deleteDataCheck">
      <label for="deleteDataCheck">Also permanently delete this stack's data</label></div>
      <ul class="hint hint-tight">${dataPaths.map(m => `<li>${m.type === "bind" ? "folder" : "volume"} <code>${esc(m.source)}</code></li>`).join("")}</ul>` : ""}
    <div class="btn-row">
      <button class="btn btn-danger" id="deleteConfirmBtn">Delete</button>
      <button class="btn" id="deleteCancelBtn">Cancel</button>
    </div>
  </div>`);
  head.insertAdjacentElement("afterend", deletePanel);
  delBtn.addEventListener("click", () => deletePanel.classList.remove("hidden"));
  deletePanel.querySelector("#deleteCancelBtn").addEventListener("click", () => deletePanel.classList.add("hidden"));
  deletePanel.querySelector("#deleteConfirmBtn").addEventListener("click", () => {
    deletePanel.classList.add("hidden");
    const deleteData = dataPaths.length && deletePanel.querySelector("#deleteDataCheck").checked;
    streamAction(name, "delete", out, true, deleteData);
  });

  const renderTab = {
    overview() {
      // Plain text here, not the usual coloured dot - this table is a
      // read-only report, not a control, and a green circle sitting
      // next to a row of buttons elsewhere in the app reads as
      // something you can click to do something. A running state is
      // just a quiet tick that blends with the rest of the text;
      // anything else still gets the real dot, since a wrong or down
      // container is worth the colour drawing the eye.
      const rows = (s.containers || []).map(c => `<tr>
        <td>${esc(c.name)}</td><td>${esc(c.service || "-")}</td><td>${esc(c.image)}</td>
        <td>${c.state === "running"
          ? `<span class="state-ok" data-tip="${esc(STATUS_TIPS.running)}">&#10003; running</span>`
          : cardDot(c.state)}</td>
        <td class="hint">${esc(c.status)}</td></tr>`).join("");
      tabBody.innerHTML = `<div class="table-wrap"><table>
        <caption>Containers in this stack</caption>
        <thead><tr><th>Container</th><th>Service</th><th>Image</th><th>State</th><th>Detail</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" class="hint">No containers - the stack isn’t deployed yet. Press play to bring it up.</td></tr>'}</tbody></table></div>`;
    },
    compose() {
      tabBody.innerHTML = "";
      const form = el(`<div class="form-grid">
        <div class="field"><label for="editCompose">${esc(s.composeFile)}</label>
          <textarea id="editCompose" class="code-editor" spellcheck="false"></textarea></div>
        <div class="field"><label for="editEnv">.env <span class="hint">(optional)</span></label>
          <textarea id="editEnv" spellcheck="false"></textarea></div>
        <div class="btn-row">
          <button class="btn btn-primary" id="saveBtn">Save</button>
          <button class="btn" id="saveUpBtn">Save &amp; redeploy</button>
        </div></div>`);
      tabBody.appendChild(form);
      form.querySelector("#editCompose").value = s.compose;
      form.querySelector("#editEnv").value = s.env;
      let envCm; // assigned below; referenced by cm's live port-conflict check
      const cm = attachYamlEditor(form.querySelector("#editCompose"), {
        getName: () => name,
        getEnv: () => envCm ? envCm.getValue() : "",
      });
      envCm = attachCodeEditor(form.querySelector("#editEnv"));
      let envTimer;
      envCm.on("change", () => { clearTimeout(envTimer); envTimer = setTimeout(() => cm.recheck(), 600); });
      const saveBtn = form.querySelector("#saveBtn");
      const saveUpBtn = form.querySelector("#saveUpBtn");
      const save = async () => {
        await api(`/api/stacks/${encodeURIComponent(name)}`, { method: "PUT", body: {
          compose: cm.getValue(),
          env: envCm.getValue() } });
        glow(form.querySelector(".editor-frame"));
        refreshPortAlerts(name);
      };
      saveBtn.addEventListener("click", () => save().catch(e => popAlert(saveBtn, e.message, "danger")));
      saveUpBtn.addEventListener("click", async () => {
        try { await save(); runAction("up"); } catch (e) { popAlert(saveUpBtn, e.message, "danger"); }
      });
    },
    logs() {
      tabBody.innerHTML = `<div class="log-view" id="liveLogs" aria-live="off"></div>
        <p class="hint hint-mt">Streaming live. Error lines show in red, warnings in amber.</p>`;
      const view = tabBody.querySelector("#liveLogs");
      attachLogCopy(view, "log");
      const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/logs/${encodeURIComponent(name)}`);
      ws.onmessage = (ev) => appendLog(view, ev.data);
      ws.onclose = () => appendLog(view, "-- log stream closed --");
      liveSockets.push(ws);
    },
    terminal() {
      const running = (s.containers || []).filter(c => c.state === "running");
      if (!running.length) {
        tabBody.innerHTML = '<p class="alert alert-warning">! The stack has no running containers to open a terminal into.</p>';
        return;
      }
      tabBody.innerHTML = "";
      const picker = el(`<div class="form-grid tight-below">
        <div class="field"><label for="termTarget">Container</label>
        <select id="termTarget">${running.map(c => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join("")}</select></div></div>`);
      const host = el('<div class="term-host"><div class="terminal" id="term"></div></div>');
      tabBody.appendChild(picker);
      tabBody.appendChild(host);
      let term, ws;
      const open = (target) => {
        if (ws) try { ws.close(); } catch (e) {}
        host.querySelector("#term").innerHTML = "";
        term = new Terminal({ fontSize: 13, convertEol: true, cursorBlink: true,
          theme: { background: "#101116" } });
        term.open(host.querySelector("#term"));
        ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/terminal/${encodeURIComponent(target)}`);
        ws.onopen = () => ws.send(`\x00resize:${term.cols}x${term.rows}`);
        ws.onmessage = (ev) => term.write(ev.data);
        ws.onclose = () => term.write("\r\n-- session ended --\r\n");
        term.onData(d => { if (ws.readyState === 1) ws.send(d); });
        term.onResize(({ cols, rows }) => { if (ws.readyState === 1) ws.send(`\x00resize:${cols}x${rows}`); });
        liveSockets.push(ws);
      };
      open(running[0].name);
      picker.querySelector("#termTarget").addEventListener("change", e => open(e.target.value));
    },
    async backup() {
      tabBody.innerHTML = `<div class="panel-head">
          <h3>Backups</h3><span class="spacer"></span>
          <label class="btn" for="bkUploadInput">Upload a backup</label>
          <input type="file" id="bkUploadInput" accept=".gz,.tar.gz" class="visually-hidden">
          <button class="btn btn-primary" id="backupNowBtn">Back up now</button></div>
        <p>Archives this stack's compose file, its .env, and its actual data - bind-mounted folders
        and named volumes read straight from where they already live, nothing moved. Restoring
        puts everything back to exactly the same place. Download a backup to keep a copy on your
        own drive, or upload one back in (from this machine or another) to restore from it.</p>
        <div class="table-wrap" id="stackBkWrap"><p class="hint">Loading…</p></div>`;
      const loadList = async () => {
        const wrap = tabBody.querySelector("#stackBkWrap");
        const d = await api(`/api/stacks/${encodeURIComponent(name)}/backups`);
        if (!d.backups.length) { wrap.innerHTML = '<p class="hint">No backups of this stack yet.</p>'; return; }
        wrap.innerHTML = `<table><caption>Backups of '${esc(name)}', newest first</caption>
          <thead><tr><th>Made</th><th>File</th><th>Size</th><th></th></tr></thead><tbody>
          ${d.backups.map(b => `<tr><td>${esc(b.made)}</td><td>${esc(b.name)}</td>
            <td>${(b.size / 1048576).toFixed(1)} MB</td>
            <td class="nowrap">
              <a class="btn" href="/api/stacks/${encodeURIComponent(name)}/backups/${encodeURIComponent(b.name)}/download">Download</a>
              <button class="btn" data-restore="${esc(b.name)}">Restore</button>
            </td></tr>`).join("")}</tbody></table>`;
        wrap.querySelectorAll("[data-restore]").forEach(btn => {
          let armed = false;
          btn.addEventListener("click", async () => {
            if (!armed) {
              armed = true; btn.textContent = "Really restore?"; btn.classList.add("btn-danger");
              setTimeout(() => { armed = false; btn.textContent = "Restore"; btn.classList.remove("btn-danger"); }, 5000);
              return;
            }
            btn.disabled = true;
            try {
              const res = await api(`/api/stacks/${encodeURIComponent(name)}/backups/${encodeURIComponent(btn.dataset.restore)}/restore`, { method: "POST", body: {} });
              popAlert(btn, res.message, "success");
            } catch (e) { popAlert(btn, e.message, "danger"); }
            btn.disabled = false; armed = false; btn.textContent = "Restore"; btn.classList.remove("btn-danger");
          });
        });
      };
      tabBody.querySelector("#backupNowBtn").addEventListener("click", async (e) => {
        e.target.disabled = true; e.target.textContent = "Backing up…";
        try {
          const r = await api(`/api/stacks/${encodeURIComponent(name)}/backups`, { method: "POST", body: {} });
          popAlert(e.target, `Backup made: ${r.name}`, "success");
          await loadList();
        } catch (err) { popAlert(e.target, err.message, "danger"); }
        e.target.disabled = false; e.target.textContent = "Back up now";
      });
      const uploadLabel = tabBody.querySelector('label[for="bkUploadInput"]');
      tabBody.querySelector("#bkUploadInput").addEventListener("change", async (e) => {
        const file = e.target.files[0];
        e.target.value = "";
        if (!file) return;
        const form = new FormData();
        form.append("file", file);
        try {
          const res = await fetch(`/api/stacks/${encodeURIComponent(name)}/backups/upload`, {
            method: "POST", headers: { "X-CSRF": CSRF }, body: form,
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(data.error || "Upload failed");
          popAlert(uploadLabel, `Uploaded ${data.name} - it's in the list below, ready to restore.`, "success");
          await loadList();
        } catch (err) { popAlert(uploadLabel, err.message, "danger"); }
      });
      await loadList();
    },
    async serve() {
      tabBody.innerHTML = '<p class="hint">Checking…</p>';
      let status;
      try { status = await api(`/api/hostcompanion/stacks/${encodeURIComponent(name)}/serve`); }
      catch (e) { status = { available: false }; }
      if (!status.available) {
        tabBody.innerHTML = `<p>Not set up. Exposing this stack's ports over Tailscale Serve needs the
          optional dockup-companion - see Settings → Host OS & Tailscale to install it with one click.</p>`;
        return;
      }
      if (!status.ports.length) {
        tabBody.innerHTML = '<p class="hint">This stack doesn\'t publish any ports, so there\'s nothing for Tailscale Serve to front.</p>';
        return;
      }
      let dnsName = "";
      try { dnsName = (await api("/api/hostcompanion/status")).tailscale?.dnsName || ""; } catch (e) {}

      tabBody.innerHTML = '<div id="servePorts"></div>';
      const host = tabBody.querySelector("#servePorts");
      // Each published port toggles independently, but reads as a plain
      // fact (what's live right now, and at what real address) rather
      // than a "pick one" prompt - most stacks publish exactly one port
      // worth reaching, so there's rarely an actual choice to make.
      for (const port of status.ports) {
        const on = status.served.includes(port);
        const url = dnsName ? `https://${esc(dnsName)}:${port}` : `port ${port}`;
        const row = el(`<label class="switch block">
          <input type="checkbox" role="switch" id="serve-${port}" ${on ? "checked" : ""}><span class="track"></span>
          <span>${on ? "Exposed at" : "Not exposed - would be"} <code>${url}</code></span></label>`);
        host.appendChild(row);
        row.querySelector("input").addEventListener("change", async (e) => {
          e.target.disabled = true;
          try {
            await api(`/api/hostcompanion/stacks/${encodeURIComponent(name)}/serve`, {
              method: "POST", body: { port, on: e.target.checked } });
            await new Promise(r => setTimeout(r, 900));
            renderTab.serve();
          } catch (err) { popAlert(e.target, err.message, "danger"); e.target.checked = !e.target.checked; e.target.disabled = false; }
        });
      }
    },
  };

  tabs.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
    closeLiveSockets();
    tabs.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("active", x === b));
    renderTab[b.dataset.tab]();
  }));
  renderTab.overview();

  if (pendingStackAction && pendingStackAction.name === name) {
    const { action } = pendingStackAction;
    pendingStackAction = null;
    runAction(action);
  }
}

/* Every one of Dockup's streaming actions speaks the same wire format:
   newline-delimited output plus a few control tokens. This reads one
   such response, calling onLine for each content line and onRestarting
   when the container serving the request is about to be replaced (after
   which the stream simply stops). Returns {ok, restarting}. */
async function readDockupStream(res, { onLine, onRestarting } = {}) {
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "", ok = true, restarting = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) {
      if (line === "[dockup-done:ok]") { ok = true; continue; }
      if (line === "[dockup-done:error]") { ok = false; continue; }
      if (line === "[dockup-restarting]") { restarting = true; if (onRestarting) onRestarting(); continue; }
      if (line) { if (onLine) onLine(line); }
    }
  }
  return { ok, restarting };
}

/* Dockup restarting itself ends the stream halfway through, because the
   container sending it has just been replaced. That is the job working,
   not a failure, so the page says so and waits rather than showing a
   network error nobody can act on. */
async function waitForSelfRestart(out) {
  appendLog(out, "Dockup is restarting itself. Waiting for it to come back…");
  for (let i = 0; i < 45; i++) {
    await new Promise(r => setTimeout(r, 2000));
    try {
      const res = await fetch("/health", { cache: "no-store" });
      if (res.ok) {
        appendLog(out, "Back. Reloading the page onto the new version…");
        // The service worker keeps a copy of the scripts and styles so
        // the app still opens if the network blips. It is network
        // first, but every one of those fetches failed while Dockup was
        // away, so the copy it holds is the version that has just been
        // replaced. Clear it, or the reload lands straight back on the
        // old page.
        try {
          for (const key of await caches.keys()) await caches.delete(key);
        } catch (e) { /* no cache storage here - nothing to clear */ }
        await new Promise(r => setTimeout(r, 800));
        location.reload();
        return true;
      }
    } catch (e) { /* still down - keep waiting */ }
  }
  appendLog(out, "Still not back after a minute and a half. Check the container "
    + "directly: docker ps, then docker logs dockup.");
  return false;
}

async function streamAction(name, action, out, isDelete = false, deleteData = false) {
  // Set the moment Dockup says it is about to replace its own
  // container, so a connection that dies straight afterwards is read as
  // that happening rather than as a fault. Anything else that goes
  // wrong is still reported as what it is.
  let sawRestart = false;
  out.innerHTML = "";
  appendLog(out, `$ ${action} ${name}`);
  document.querySelectorAll(".panel-head .icon-btn").forEach(b => b.disabled = true);
  try {
    const qs = deleteData ? "?deleteData=1" : "";
    const res = await fetch(`/api/stacks/${encodeURIComponent(name)}/action/${action}${qs}`, {
      method: "POST", headers: { "X-CSRF": CSRF },
    });
    const { ok, restarting } = await readDockupStream(res, {
      onRestarting: () => { sawRestart = true; },
      onLine: line => {
        const hint = line.match(/^\[dockup-hint:tailscale-port-conflict:(\d+):([01])\]$/);
        if (hint) { renderPortConflictHint(out, hint[1], hint[2] === "1"); return; }
        appendLog(out, line);
      }});
    // Dockup said it was about to replace its own container, so whatever
    // the stream did next, the right thing now is to wait for it and
    // come back on the new version.
    if (restarting || sawRestart) { await waitForSelfRestart(out); return; }
    if (ok) {
      popAlert(out, `${action} finished.`, "success");
    } else {
      appendLog(out, `ERROR: ${action} did not complete cleanly - see above.`);
      popAlert(out, `${action} failed - the output panel has the details.`, "danger");
    }
    if (isDelete && ok) {
      await refreshStacks();
      await new Promise(r => setTimeout(r, 900));
      location.hash = "#/";
      return;
    }
    await refreshStacks();
    if (!isDelete && location.hash === `#/stack/${encodeURIComponent(name)}`) {
      await new Promise(r => setTimeout(r, 900)); // let the finished/failed bubble be seen before the panel rebuilds
      const output = out.innerHTML;
      await viewStack(name);
      const fresh = document.getElementById("actionOut");
      if (fresh) { fresh.innerHTML = output; fresh.scrollTop = fresh.scrollHeight; }
    }
  } catch (e) {
    // The connection dropping after Dockup has said it is replacing its
    // own container is the expected end of this stream. Only then: an
    // error that arrives without that warning is a real one and is
    // shown as such.
    if (sawRestart) {
      await waitForSelfRestart(out);
      return;
    }
    appendLog(out, "ERROR: " + e.message);
    popAlert(out, e.message, "danger");
  } finally {
    document.querySelectorAll(".panel-head .icon-btn").forEach(b => b.disabled = false);
  }
}

/* ---- maintenance ---- */

const PRUNE_INFO = [
  ["images", "Unused images", "Removes images no container uses. They re-download if ever needed again."],
  ["containers", "Stopped containers", "Removes containers that have exited. Their images and volumes stay."],
  ["networks", "Unused networks", "Removes networks no container is attached to."],
  ["buildcache", "Build cache", "Clears layers cached from image builds."],
  ["volumes", "Unused volumes", "The only one that can lose data. \u201cUnused\u201d means no container is using it " +
    "<em>right now</em> \u2014 which also describes every volume belonging to a stack you have merely stopped. " +
    "Everything else on this page only removes things Docker can rebuild; this removes real data, permanently. " +
    "Dockup lists each volume and whose it is before anything is deleted."],
];

function helpTile(iconSvg, title, desc) {
  return `<div class="help-tile"><div class="help-tile-icon">${iconSvg}</div>
    <div><h4>${esc(title)}</h4><p>${esc(desc)}</p></div></div>`;
}

async function viewHelp() {
  const NAV_ICO = {
    allStacks: '<svg viewBox="0 0 24 24"><path d="M12 3 3 8l9 5 9-5-9-5Z M3 12l9 5 9-5 M3 16l9 5 9-5" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    newStack: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>',
    maintenance: '<svg viewBox="0 0 24 24"><path d="M4 17l6-6M14 4l-2.5 2.5a4 4 0 0 0 5.5 5.5L19.5 9.5A4 4 0 0 1 14 4zM4 17l3 3" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>',
    activity: '<svg viewBox="0 0 24 24"><path d="M4 12h4l2-7 4 14 2-7h4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>',
    backups: '<svg viewBox="0 0 24 24"><path d="M12 3v10m0 0l-4-4m4 4l4-4M5 17v2a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-2" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>',
    settings: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2" fill="none"/><path d="M19 12a7 7 0 0 0-.1-1.2l2-1.5-2-3.5-2.3 1a7 7 0 0 0-2-1.2L14.2 3h-4l-.4 2.6a7 7 0 0 0-2 1.2l-2.3-1-2 3.5 2 1.5a7 7 0 0 0 0 2.4l-2 1.5 2 3.5 2.3-1a7 7 0 0 0 2 1.2l.4 2.6h4l.4-2.6a7 7 0 0 0 2-1.2l2.3 1 2-3.5-2-1.5c.06-.4.1-.8.1-1.2z" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linejoin="round"/></svg>',
    signOut: '<svg viewBox="0 0 24 24"><path d="M9 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3M14 8l4 4-4 4M18 12H9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>',
    power: '<svg viewBox="0 0 24 24"><path d="M12 3v8" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/><path d="M7 5.5a8 8 0 1 0 10 0" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>',
  };

  content.innerHTML = `
    <h1>How Dockup works</h1>
    ${headlineHtml("What every part of the screen does, and what each button will do before you press it.")}

    <div class="panel">
      <h2>Getting around</h2>
      <p>The bar along the top, and what each part does.</p>
      <div class="help-grid">
        ${helpTile('<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="#4BAE4F"/></svg>', "The update dot", "Sits next to Maintenance in the top bar. Green: up to date - click it to check again right now. Amber: a new version is published - click to download it. Once downloaded it turns blue - click to restart. No separate check button anywhere; it also keeps itself current on its own in the background.")}
        ${helpTile(NAV_ICO.allStacks, "All stacks", "Just above the dashboard's stack cards - back to the dashboard, every stack, one glance.")}
        ${helpTile(NAV_ICO.newStack, "New stack", "Write or paste a compose file, or convert a docker run command.")}
        ${helpTile(NAV_ICO.maintenance, "Maintenance", "Disk usage, and pruning unused images/containers/networks/cache/volumes.")}
        ${helpTile(NAV_ICO.activity, "Activity", "A running log of everything Dockup has done and any errors along the way.")}
        ${helpTile(NAV_ICO.backups, "Backups", "Daily automatic backups of Dockup's own data, with one-click restore.")}
        ${helpTile(NAV_ICO.settings, "Settings", "Account, email alerts, and the optional host companion.")}
        ${helpTile(NAV_ICO.power, "Restart Docker / Reboot server", "Only shown once the host companion is installed - real host-level actions, not container ones.")}
        ${helpTile(NAV_ICO.signOut, "Sign out", "Ends your session immediately.")}
      </div>
    </div>

    <div class="panel">
      <h2>On a stack</h2>
      <p>The status dot alongside the action buttons is itself the update control - not ready, click it to check
        this stack for an update right now; ready, click it to pull and redeploy, streamed live. The icons next to it:</p>
      <div class="help-grid">
        ${helpTile(ICONS.external, "Open web UI", "Opens the stack's own address in a new tab - its Tailscale Serve address if set up, otherwise the host address you're already using.")}
        ${helpTile(ICONS.play, "Start", "Brings the stack up - creates containers the first time, starts them after.")}
        ${helpTile(ICONS.stop, "Stop", "Stops the containers. Nothing is removed; Start brings them straight back.")}
        ${helpTile(ICONS.restart, "Restart", "Restarts the same containers in place.")}
        ${helpTile(ICONS.redeploy, "Redeploy", "Recreates the containers from the compose file - fixes a stuck one without pulling a new image.")}
        ${helpTile(ICONS.update, "Update", "Pulls the newest images for every service, then redeploys.")}
        ${helpTile(ICONS.down, "Down", "Stops the containers and removes them. The compose file and any data stay exactly where they are.")}
        ${helpTile(ICONS.bin, "Delete", "Removes the stack for good: containers, compose file and image. Data can be deleted too, with an explicit opt-in.")}
      </div>
    </div>

    <div class="panel">
      <h2>Status dots</h2>
      <p>Deliberately just three colours, the same on every card and page - one glance is enough to know whether a
        stack needs you.</p>
      <ul class="help-dots">
        <li><span class="status-dot running"></span><span><strong>Green</strong> - running normally.</span></li>
        <li><span class="status-dot update"></span><span><strong>Amber</strong> - an update is waiting; click the
          dot to pull and redeploy.</span></li>
        <li><span class="status-dot"></span><span><strong>Red</strong> - anything else that isn't simply running:
          stopped, exited, restarting, or a health warning. One "needs attention" signal rather than a different
          shade for every cause - open the stack to see which.</span></li>
        <li><span class="status-dot inactive"></span><span><strong>Grey</strong> - no container at all, the one
          exception: not a problem, just ready to archive or delete.</span></li>
      </ul>
    </div>

    <div class="panel">
      <h2>Keeping stacks up to date</h2>
      <p>Dockup checks every managed, running stack for a newer image every 30 minutes, on its own - it only ever
        <strong>flags</strong> an update, never pulls one without you asking. <strong>Check for updates</strong> on
        the dashboard runs that same check right now instead of waiting. From there you can update one stack at a
        time (its cloud icon, or its own Update button) or everything that's flagged at once
        (<strong>Update all</strong>). Every update streams its real output, so a slow pull or a failure both show
        you exactly what happened rather than a single word.</p>
    </div>

    <div class="panel">
      <h2>Dockup itself</h2>
      <p>Dockup doesn't appear as a stack you can act on - stopping or deleting the very thing you're using to
        manage everything else is a mistake worth designing out, not just warning about. Its own update is the
        one control it offers: the update dot in the top bar, next to Maintenance. Green means up to date -
        click it any time to check again right now, rather than waiting for the next background check. Amber
        means a new version is published - click it to download the new image in the background while Dockup
        keeps running as it is. Once that finishes the same dot turns blue; click it again and the page goes away for
        a few seconds while Dockup replaces itself, then comes back on its own.</p>
    </div>

    <div class="panel">
      <h2>Backups</h2>
      <div class="help-cols">
        <div class="help-col"><h4>Backups page</h4><p>Automatic, daily, and covers Dockup's own data - the
          database, settings, activity log. One-click restore, plus a download-everything zip for keeping a copy
          yourself.</p></div>
        <div class="help-col"><h4>A stack's own Backup tab</h4><p>Archives that one stack's compose file, its
          .env, and its <strong>real data</strong> - bind-mounted folders and named volumes, read straight from
          where they already live. Download a backup to move a stack to another machine, or upload one back in to
          restore it.</p></div>
      </div>
    </div>

    <div class="panel">
      <h2>The host companion (optional)</h2>
      <p>Dockup's container access lets it manage every stack, but the server underneath - the operating system
        itself - is outside that box on purpose. The companion is a small helper that runs directly on the host so
        Dockup can reach the handful of things that need real root: host OS updates, and Tailscale Serve.</p>
      <ol class="help-steps">
        <li>Open <strong>Settings → Host OS & Tailscale</strong>.</li>
        <li>Click <strong>Install companion</strong>. It installs a systemd service on the host.</li>
        <li>Dockup reconnects to it automatically, restarting itself briefly as the last step.</li>
      </ol>
      <p class="hint">What it can do: exactly four things - check and apply host OS updates, install Tailscale, and
        turn Tailscale Serve on or off for a port. Every request is one of that fixed list; there is no "run this
        command" option, so nothing reaching that socket can ever ask it for anything else. Entirely optional -
        Dockup works without it, you just won't see the Host OS panel or per-stack Tailscale toggle.</p>
    </div>

    <div class="panel">
      <h2>Security, in short</h2>
      <p>Real server-side login with rate limiting and optional two-factor (TOTP) - no default password, no
        skipping the login screen. Every state-changing request is CSRF-checked, session cookies are HttpOnly and
        SameSite, and secrets like an SMTP password are encrypted at rest and never sent back to the browser.
        No analytics and no tracking, and everything Dockup itself needs is served by Dockup. The one exception
        is the Lightmorphic app launcher, the grid of dots at the end of the top bar: that loads a script and its
        icons from apps.lightmorphic.com. Take the two apps.lightmorphic.com entries out of the
        Content-Security-Policy in <code>app/__init__.py</code> and it stops loading.</p>
    </div>`;
}

async function viewMaintenance() {
  content.innerHTML = `<h1>Maintenance</h1>
    ${headlineHtml("What Docker is using on this disk, and what can safely be cleared.")}
    <div class="panel"><div class="panel-head"><h2>Disk usage</h2></div>
      <div class="table-wrap" id="dfWrap"><p class="hint">Loading…</p></div></div>
    <div class="panel"><div class="panel-head"><h2>Prune</h2>
      <span class="spacer"></span><button class="btn btn-primary" id="pruneAll">Prune everything</button></div>
      <p>Each cleans one kind of leftover. <strong>Prune everything</strong> runs the safe four together \u2014
      images, containers, networks and build cache are all rebuildable, so pruning them costs you nothing but
      a slower next build. Volumes hold real data and are never included; they stay a separate, deliberate step.</p>
      <div class="prune-grid" id="pruneGrid"></div></div>`;

  api("/api/system/df").then(d => {
    document.getElementById("dfWrap").innerHTML = `<table>
      <caption>What the engine is using on disk</caption>
      <thead><tr><th>Type</th><th class="num">Total</th><th class="num">Active</th>
        <th class="num">Size</th><th class="num">Reclaimable</th></tr></thead>
      <tbody>${d.usage.map(u => `<tr><td>${esc(u.type)}</td><td class="num">${esc(u.total)}</td>
        <td class="num">${esc(u.active)}</td><td class="num">${esc(u.size)}</td>
        <td class="num">${esc(u.reclaimable)}</td></tr>`).join("")}</tbody></table>`;
  }).catch(e => document.getElementById("dfWrap").innerHTML =
    `<p class="alert alert-danger">! ${esc(e.message)}</p>`);

  const grid = document.getElementById("pruneGrid");
  for (const [key, title, blurb] of PRUNE_INFO) {
    const card = el(`<div class="panel prune-card"><h3>${title}</h3>
      <p class="hint">${blurb}</p>
      <button class="btn ${key === "volumes" ? "btn-danger" : ""}" data-target="${key}">Prune ${key === "buildcache" ? "cache" : key}</button>
      <div class="prune-result" role="status"></div></div>`);
    const btn = card.querySelector("button");
    const result = card.querySelector(".prune-result");
    btn.addEventListener("click", async () => {
      if (key === "volumes") {
        try {
          const prev = await api("/api/system/prune/volumes/preview");
          if (!prev.volumes.length) { result.textContent = "No unused volumes."; result.className = "prune-result ok"; return; }
          if (!card.dataset.armed) {
            card.dataset.armed = "1";
            result.className = "prune-result bad";
            const order = { "in-use": 0, superseded: 1, orphaned: 2 };
            const rows = [...prev.volumes].sort((a, b) => order[a.verdict] - order[b.verdict])
              .map(v => `<li class="vol-${esc(v.verdict)}"><strong>${esc(v.name)}</strong>${v.size ? ` \u00b7 ${esc(v.size)}` : ""}
                <span class="hint">${esc(v.note || "")}</span></li>`).join("");
            const risky = prev.volumes.filter(v => v.verdict === "in-use").length;
            result.innerHTML = `<p>Will <strong>permanently delete</strong> these ${prev.volumes.length} volumes
              and the data in them:</p><ul class="vol-list">${rows}</ul>
              <p>${risky
                ? `<strong>${risky} of them belong to a stack that is only stopped.</strong> Start that stack again
                   and its data will be gone. Delete those only if you are finished with the stack.`
                : "None of them belong to a stack Dockup can see, so this should only reclaim genuine leftovers."}
              Click again to confirm.</p>`;
            setTimeout(() => {
              if (!card.dataset.armed) return;  // already confirmed - leave the outcome on screen
              delete card.dataset.armed;
              result.innerHTML = "";
            }, 15000);
            return;
          }
          delete card.dataset.armed;
        } catch (e) { result.textContent = e.message; result.className = "prune-result bad"; return; }
      }
      btn.disabled = true; result.textContent = "Working…"; result.className = "prune-result";
      try {
        const res = await api("/api/system/prune", { method: "POST", body: { targets: [key] } });
        const r = res.results[key];
        result.textContent = (r.ok ? "✓ " : "! ") + r.message;
        result.className = "prune-result " + (r.ok ? "ok" : "bad");
      } catch (e) { result.textContent = "! " + e.message; result.className = "prune-result bad"; }
      btn.disabled = false;
    });
    grid.appendChild(card);
  }

  document.getElementById("pruneAll").addEventListener("click", async (e) => {
    const btn = e.target;
    btn.disabled = true; btn.textContent = "Pruning…";
    try {
      const res = await api("/api/system/prune", { method: "POST",
        body: { targets: ["containers", "images", "networks", "buildcache"] } });
      const lines = Object.entries(res.results).map(([k, r]) => `${k}: ${r.message}`);
      popAlert(btn, lines.join(" · "), res.ok ? "success" : "danger");
      await new Promise(r => setTimeout(r, 900));
      viewMaintenance();
    } catch (err) {
      popAlert(btn, err.message, "danger");
      btn.disabled = false; btn.textContent = "Prune everything";
    }
  });
}

/* ---- activity ---- */

async function viewActivity() {
  content.innerHTML = `<h1>Activity</h1>
    ${headlineHtml("Everything Dockup has done, newest first.")}
    <div class="panel"><div class="panel-head">
      <h2>What Dockup has done</h2><span class="spacer"></span>
      <label class="switch">
        <input type="checkbox" role="switch" id="errOnly"><span class="track"></span>
        <span>Errors only</span></label></div>
      <div id="activityRows"><p class="hint">Loading…</p></div></div>`;
  const load = async () => {
    const errorsOnly = document.getElementById("errOnly").checked;
    const d = await api("/api/activity" + (errorsOnly ? "?errors=1" : ""));
    const host = document.getElementById("activityRows");
    if (!d.entries.length) { host.innerHTML = '<p class="hint">Nothing recorded yet.</p>'; return; }
    host.innerHTML = d.entries.map(a => `
      <div class="activity-row is-${esc(a.level)}">
        <span class="a-ts">${esc(a.ts)}</span>
        <span class="a-cat">${a.level === "error" ? "! " : ""}${esc(a.category)}</span>
        <span class="a-msg">${esc(a.message)}</span>
        ${a.detail ? `<span class="a-detail">${esc(a.detail)}</span>` : ""}
      </div>`).join("");
  };
  document.getElementById("errOnly").addEventListener("change", load);
  await load();
}

/* ---- backups ---- */

async function viewBackups() {
  content.innerHTML = `<h1>Backups</h1>
    ${headlineHtml("A copy of Dockup's own settings and history, taken every day.")}
    <div class="panel"><div class="panel-head"><h2>Backups</h2><span class="spacer"></span>
      <button class="btn btn-primary" id="backupNow">Back up now</button>
      <a class="btn" href="/api/backup/export">Download everything (zip)</a></div>
      <p>A backup is taken automatically every day and kept for the number of days set in Settings.
      Restoring puts the stack files back exactly as they were - and keeps the current files to one
      side first, so even a restore can be undone.</p>
      <div class="table-wrap" id="bkWrap"><p class="hint">Loading…</p></div></div>`;
  const load = async () => {
    const d = await api("/api/backup/list");
    const wrap = document.getElementById("bkWrap");
    if (!d.backups.length) { wrap.innerHTML = '<p class="hint">No backups yet - the first runs tonight, or press "Back up now".</p>'; return; }
    wrap.innerHTML = `<table><caption>Saved backups, newest first</caption>
      <thead><tr><th>Made</th><th>File</th><th>Size</th><th></th></tr></thead><tbody>
      ${d.backups.map(b => `<tr><td>${esc(b.made)}</td><td>${esc(b.name)}</td>
        <td>${(b.size / 1048576).toFixed(1)} MB</td>
        <td class="nowrap">
          <a class="btn" href="/api/backup/download/${encodeURIComponent(b.name)}">Download</a>
          <button class="btn" data-restore="${esc(b.name)}">Restore</button>
        </td></tr>`).join("")}</tbody></table>`;
    wrap.querySelectorAll("[data-restore]").forEach(btn => {
      let armed = false;
      btn.addEventListener("click", async () => {
        if (!armed) { armed = true; btn.textContent = "Really restore?"; btn.classList.add("btn-danger");
          setTimeout(() => { armed = false; btn.textContent = "Restore"; btn.classList.remove("btn-danger"); }, 5000); return; }
        btn.disabled = true;
        try {
          const res = await api("/api/backup/restore", { method: "POST",
            body: { name: btn.dataset.restore, force: btn.dataset.force === "1" } });
          popAlert(btn, res.message, "success");
          await refreshStacks();
        } catch (e) {
          if (e.confirm) {
            // The backup left files out. Say so, and keep the button
            // armed as "Restore anyway": a second deliberate click sends
            // the same request with force set.
            popAlert(btn, e.message, "warning", 12000);
            btn.disabled = false; armed = true; btn.dataset.force = "1";
            btn.textContent = "Restore anyway"; btn.classList.add("btn-danger");
            return;
          }
          popAlert(btn, e.message, "danger");
        }
        btn.disabled = false; armed = false; btn.textContent = "Restore"; btn.classList.remove("btn-danger");
      });
    });
  };
  document.getElementById("backupNow").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/backup/run", { method: "POST", body: {} });
      popAlert(e.target, r.message || `Backup made: ${r.name}`, r.skipped ? "warning" : "success",
               r.skipped ? 8000 : 3500);
      await load();
    }
    catch (err) { popAlert(e.target, err.message, "danger"); }
    e.target.disabled = false;
  });
  await load();
}

/* ---- settings ---- */

const ACCENTS = ["", "red", "pink", "purple", "deep_purple", "indigo", "blue", "light_blue",
  "cyan", "teal", "green", "light_green", "lime", "yellow", "amber", "orange",
  "deep_orange", "brown", "grey", "blue_grey"];

async function viewSettings() {
  let s;
  try { s = await api("/api/settings"); } catch (e) { content.innerHTML = `<p class="alert alert-danger">! ${esc(e.message)}</p>`; return; }
  content.innerHTML = `<h1>Settings</h1>
    ${s._smtp_ready
      ? headlineHtml("Everything about this copy of Dockup: your account, email alerts and the host companion.")
      : headlineHtml("Email isn't set up yet, so errors are recorded in Activity but not emailed to you.",
          { dot: "waiting" })}
    <div class="panel"><div class="panel-head"><h2>Engine</h2></div>
      <div class="form-grid">
        <div class="field"><label for="setSocket">Docker socket path</label>
          <input id="setSocket" spellcheck="false">
          <span class="hint">The default is /var/run/docker.sock, mounted into the Dockup container.</span></div>
        <div class="btn-row">
          <button class="btn" id="testRuntime">Test connection</button></div>
      </div></div>
    <div class="panel"><div class="panel-head"><h2>Private registries</h2></div>
      <p class="hint">For images that need a login to pull - your own Forgejo or GitLab registry, a
        private image repository. Public images need nothing here. Tokens are stored encrypted and never
        shown again once saved.</p>
      <div id="regList"></div>
      <div class="form-grid mt-lg">
        <div class="two-col">
          <div class="field"><label for="regHost">Registry address</label>
            <input id="regHost" spellcheck="false" autocomplete="off" placeholder="registry.example.com:5000"></div>
          <div class="field"><label for="regUser">Username</label>
            <input id="regUser" spellcheck="false" autocomplete="off"></div>
        </div>
        <div class="field"><label for="regToken">Token or password</label>
          <input id="regToken" type="password" autocomplete="new-password">
          <span class="hint">A read-only token is enough - Dockup only ever pulls. To change the username on a
            saved registry, leave this blank and the saved token is kept.</span></div>
        <div class="btn-row">
          <button class="btn" id="regTest">Test</button>
          <button class="btn btn-primary" id="regSave">Save registry</button></div>
      </div></div>
    <div class="panel"><div class="panel-head"><h2>Email alerts</h2><span class="spacer"></span>
      <span class="hint" data-tip="${s._smtp_ready ? "Host, sender, and recipient are all set" : "Fill in server, sender, and recipient below"}">${s._smtp_ready
        ? "Active" : "Not set up"}</span></div>
      <div class="form-grid">
        <div class="two-col">
          <div class="field"><label for="smtpHost">SMTP server</label><input id="smtpHost" spellcheck="false"></div>
          <div class="field"><label for="smtpPort">Port</label><input id="smtpPort" inputmode="numeric"></div>
        </div>
        <div class="field"><label for="smtpSec">Connection security</label>
          <select id="smtpSec"><option value="starttls">STARTTLS (usual, port 587)</option>
          <option value="tls">TLS (port 465)</option><option value="none">None (LAN relay only)</option></select></div>
        <div class="two-col">
          <div class="field"><label for="smtpUser">SMTP username</label><input id="smtpUser" autocomplete="off" spellcheck="false"></div>
          <div class="field"><label for="smtpPass">SMTP password</label><input id="smtpPass" type="password" autocomplete="new-password" placeholder="unchanged">
            <span class="hint">Stored encrypted. Leave blank to keep the saved one.${s["smtp.password"] ? " Password is set." : " Not set."}</span></div>
        </div>
        <div class="two-col">
          <div class="field"><label for="smtpFrom">Send from</label><input id="smtpFrom" spellcheck="false" placeholder="dockup@yourdomain"></div>
          <div class="field"><label for="alertTo">Send alerts to</label><input id="alertTo" spellcheck="false"></div>
        </div>
        <div class="field"><label class="switch">
          <input type="checkbox" role="switch" id="alertOn"><span class="track"></span>
          <span>Email me when an error happens</span></label></div>
        <div class="btn-row">
          <button class="btn" id="testSmtp">Send test email</button></div>
      </div></div>
    <div class="panel"><div class="panel-head"><h2>Backups</h2></div>
      <div class="two-col">
        <div class="field"><label for="bkHour">Daily backup hour (0-23)</label><input id="bkHour" inputmode="numeric"></div>
        <div class="field"><label for="bkKeep">Keep backups for (days)</label><input id="bkKeep" inputmode="numeric"></div>
      </div></div>
    <div class="panel"><div class="panel-head"><h2>Dumps before a backup</h2></div>
      <div class="form-grid">
        <p class="note">A database copied while it is being written to often will not restore. These
        run early in the morning, inside the container, and ask the application itself for a clean copy. Each one
        lands in <code>data/pre-backup</code>, which whatever copies this machine overnight picks up
        with everything else. A dump that fails leaves the last good one alone.</p>
        <div class="field"><label for="predumpHour">Start at this hour (0-23)</label>
          <input id="predumpHour" inputmode="numeric">
          <span class="hint">They have until half past to finish, so the rest of the hour is margin
          before whatever copies this machine afterwards.</span></div>
        <div class="field"><label for="predumpJobs">What to dump</label>
          <textarea id="predumpJobs" class="code-editor" spellcheck="false" rows="8"></textarea>
          <span class="hint">One entry per database, as a list. Each needs the container to run in,
          the command that writes a copy to its output, and the file name to save it as.</span></div>
        <div class="btn-row align-center">
          <button class="btn" id="predumpRun">Run them now</button>
          <span class="hint">Writes them straight away so you can see it work.</span></div>
      </div></div>
    <div class="panel"><div class="panel-head"><h2>Appearance</h2></div>
      <div class="form-grid">
        <div class="field"><label for="setAccent">Accent colour</label>
          <select id="setAccent" class="picker-native" aria-label="Highlight colour">${ACCENTS.map(a => `<option value="${a}">${a === "" ? "Brand yellow (default)" : a.replace("_", " ")}</option>`).join("")}</select>
          <div class="swatches" id="accentSwatches" role="radiogroup" aria-label="Highlight colour"></div>
          <p class="hint" id="accentName"></p></div>
      </div></div>
    <div class="panel"><div class="panel-head"><h2>Account</h2></div>
      <div class="form-grid">
        <div class="field"><label for="pwCurrent">Current password</label><input id="pwCurrent" type="password" autocomplete="current-password"></div>
        <div class="field"><label for="pwNew">New password <span class="hint">(at least 12 characters)</span></label><input id="pwNew" type="password" autocomplete="new-password"></div>
        <div><button class="btn" id="pwBtn">Change password</button></div>
        <div class="field"><span class="field-label">Two-factor authentication</span><div id="tfaHost"></div></div>
      </div></div>
    <p class="hint mt-lg">Built for home labs.</p>`;

  const f = (id) => document.getElementById(id);
  f("setSocket").value = s["runtime.socket"];
  f("smtpHost").value = s["smtp.host"];
  f("smtpPort").value = s["smtp.port"];
  f("smtpSec").value = s["smtp.security"];
  f("smtpUser").value = s["smtp.username"];
  f("smtpPass").value = "";
  f("smtpPass").placeholder = s["smtp.password"] ? "saved - leave blank to keep" : "not set";
  f("smtpFrom").value = s["smtp.from"];
  f("alertTo").value = s["alerts.email_to"];
  f("alertOn").checked = s["alerts.on_error"] === "1";
  f("bkHour").value = s["backup.hour"];
  f("bkKeep").value = s["backup.retention_days"];
  f("predumpJobs").value = s["predump.jobs"] || "[]";
  f("predumpHour").value = s["predump.hour"] || "2";
  f("setAccent").value = s["ui.accent"];

  // Our own dropdowns rather than the browser's, which is drawn by the
  // system and arrives as a white strip in the middle of a dark page.
  enhanceSelect(f("smtpSec"));
  renderAccentSwatches(f("setAccent"));

  // Every setting saves itself the moment you finish with it - on
  // change, which is leaving a box you have edited or flipping a
  // switch, never on every keystroke, which would be a request per
  // character racing itself. There is no Save button.
  const SETTING_KEYS = {
    setSocket: "runtime.socket",
    smtpHost: "smtp.host",
    smtpPort: "smtp.port",
    smtpSec: "smtp.security",
    smtpUser: "smtp.username",
    smtpPass: "smtp.password",
    smtpFrom: "smtp.from",
    alertTo: "alerts.email_to",
    alertOn: "alerts.on_error",
    bkHour: "backup.hour",
    bkKeep: "backup.retention_days",
    predumpJobs: "predump.jobs",
    predumpHour: "predump.hour",
    setAccent: "ui.accent",
  };
  for (const [id, key] of Object.entries(SETTING_KEYS)) {
    const box = f(id);
    if (!box) continue;
    box.addEventListener("change", async () => {
      const value = box.type === "checkbox" ? (box.checked ? "1" : "0") : box.value;
      // A blank password box means "keep the one already saved", so
      // there is nothing to send and nothing to glow about.
      if (key === "smtp.password" && !value) return;
      try {
        await api("/api/settings", { method: "POST", body: { [key]: value } });
        fieldError(box, "");
        if (key === "ui.accent") applyAccent(value);
        if (key === "smtp.password") {
          // The password itself never comes back from the server, so
          // the box empties and says it is held.
          box.value = "";
          box.placeholder = "saved - leave blank to keep";
        }
        glow(box);
      } catch (e) {
        fieldError(box, e.message);
      }
    });
  }

  const renderRegistries = async () => {
    const box = f("regList");
    if (!box) return;
    let regs;
    try { regs = await api("/api/settings/registries"); }
    catch (e) { box.innerHTML = `<p class="alert alert-danger">! ${esc(e.message)}</p>`; return; }
    box.innerHTML = regs.length ? "" : '<p class="hint">None saved yet.</p>';
    for (const r of regs) {
      const row = el(`<div class="check-row archived-row">
        <span><code>${esc(r.host)}</code> <span class="hint">as ${esc(r.username)}</span></span>
        <span class="spacer"></span>
        <button class="btn" data-act="test">Test</button>
        <button class="btn btn-danger" data-act="del">Remove</button></div>`);
      box.appendChild(row);
      const testBtn = row.querySelector('[data-act="test"]');
      testBtn.addEventListener("click", async () => {
        popAlert(testBtn, "Signing in…", "info");
        try {
          const res = await api("/api/settings/registries/test", { method: "POST",
            body: { host: r.host, username: r.username, token: "" } });
          popAlert(testBtn, res.message, "success");
        } catch (e) { popAlert(testBtn, e.message, "danger"); }
      });
      const delBtn = row.querySelector('[data-act="del"]');
      delBtn.dataset.tipOrig = "Remove";
      armedAction(delBtn, async () => {
        await api(`/api/settings/registries/${r.id}/delete`, { method: "POST", body: {} });
        await renderRegistries();
      }, `remove the saved login for ${r.host}`, { navigate: false });
    }
  };
  renderRegistries();

  f("regTest").addEventListener("click", async () => {
    popAlert(f("regTest"), "Signing in…", "info");
    try {
      const res = await api("/api/settings/registries/test", { method: "POST", body: {
        host: f("regHost").value, username: f("regUser").value, token: f("regToken").value } });
      popAlert(f("regTest"), res.message, "success");
    } catch (e) { popAlert(f("regTest"), e.message, "danger"); }
  });
  f("regSave").addEventListener("click", async () => {
    try {
      await api("/api/settings/registries", { method: "POST", body: {
        host: f("regHost").value, username: f("regUser").value, token: f("regToken").value } });
      f("regHost").value = f("regUser").value = f("regToken").value = "";
      glow(f("regSave").closest(".panel"));
      await renderRegistries();
    } catch (e) { popAlert(f("regSave"), e.message, "danger"); }
  });

  f("predumpRun").addEventListener("click", async (e) => {
    const btn = e.target;
    btn.disabled = true;
    try {
      const r = await api("/api/settings/predump/run", { method: "POST", body: {} });
      popAlert(btn, r.message, r.ok ? "success" : "warning", 6000);
    } catch (err) {
      popAlert(btn, err.message, "danger", 6000);
    } finally { btn.disabled = false; }
  });

  f("testRuntime").addEventListener("click", async () => {
    try {
      const r = await api("/api/settings/test-runtime", { method: "POST", body: {
        engine: "docker", socket: f("setSocket").value } });
      popAlert(f("testRuntime"), r.message, "success");
    } catch (e) { popAlert(f("testRuntime"), e.message, "danger"); }
  });
  f("testSmtp").addEventListener("click", async () => {
    popAlert(f("testSmtp"), "Sending test email…", "info");
    try {
      // Test whatever's actually on screen right now, not just
      // whatever was last saved - matches testRuntime below.
      const r = await api("/api/settings/test-smtp", { method: "POST", body: {
        "smtp.host": f("smtpHost").value,
        "smtp.port": f("smtpPort").value,
        "smtp.security": f("smtpSec").value,
        "smtp.username": f("smtpUser").value,
        "smtp.password": f("smtpPass").value,
        "smtp.from": f("smtpFrom").value,
        "alerts.email_to": f("alertTo").value,
      } });
      popAlert(f("testSmtp"), r.message, "success");
      // A successful test just saved these settings server-side (see
      // test_smtp) - let the success message show first, then refresh
      // the whole panel so the "Active"/password-set indicators and
      // every field reflect what's now actually saved, not just what
      // happened to still be sitting in the form.
      await new Promise(res => setTimeout(res, 1500));
      if ((location.hash || "#/") === "#/settings") viewSettings();
    } catch (e) { popAlert(f("testSmtp"), e.message, "danger"); }
  });
  f("pwBtn").addEventListener("click", async () => {
    try {
      await api("/api/account/password", { method: "POST", body: {
        current: f("pwCurrent").value, new: f("pwNew").value } });
      f("pwCurrent").value = f("pwNew").value = "";
      glow(f("pwBtn").closest(".panel"));
    } catch (e) { popAlert(f("pwBtn"), e.message, "danger"); }
  });

  renderTfa(document.getElementById("tfaHost"));
  await renderHostCompanionPanel();
}

/* Streams the install: stage + host install + (on success) edit
   compose.yaml and restart Dockup itself to reconnect. That last step
   tears down the very container serving this request, so the fetch
   stream ends abruptly right after "[dockup-restarting]" - expected,
   not a failure. Poll /health (no auth needed) until Dockup answers
   again, then refresh the panel in place. */
async function installCompanion(btn) {
  btn.disabled = true; btn.textContent = "Installing…";
  const panel = openProgressPanel("Installing dockup-companion");
  let restarting = false;
  try {
    const res = await fetch("/api/hostcompanion/install", { method: "POST", headers: { "X-CSRF": CSRF } });
    // restarting is set the moment the marker arrives, not when the
    // stream finishes - the restart it announces is what cuts the
    // stream off, so waiting for a clean end means this flag is still
    // false in the catch below and a successful install gets reported
    // as "ERROR: network error".
    const result = await readDockupStream(res, {
      onLine: line => panel.line(line),
      onRestarting: () => { restarting = true; panel.line("Reconnecting Dockup…"); },
    });
    restarting = restarting || result.restarting;
  } catch (e) {
    if (!restarting) {
      panel.line("ERROR: " + e.message);
      panel.done(false);
      btn.disabled = false; btn.textContent = "Install companion";
      return;
    }
    // else: expected - the connection dropped because Dockup restarted itself
  }
  if (restarting) {
    await waitForDockupBack(panel);
  } else {
    panel.done(true);
  }
  if (location.hash === "#/settings") await renderHostCompanionPanel();
}

async function waitForDockupBack(panel) {
  panel.line("Waiting for Dockup to come back…");
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 2000));
    if (panel.closed()) return; // dismissed - stop narrating, the restart itself still completes
    try {
      const res = await fetch("/health", { cache: "no-store" });
      if (res.ok) { panel.line("Reconnected."); panel.done(true); return; }
    } catch (e) { /* still down - keep waiting */ }
  }
  panel.line("Still not back after a minute - check the container directly (docker ps / docker logs dockup).");
  panel.done(false);
}

async function renderHostCompanionPanel() {
  document.querySelectorAll(".host-companion-panel").forEach(p => p.remove());
  const panel = el(`<div class="panel host-companion-panel"><div class="panel-head"><h2>Host OS &amp; Tailscale</h2></div>
    <div id="hostCompanionBody"><p class="hint">Checking…</p></div></div>`);
  content.appendChild(panel);
  const body = panel.querySelector("#hostCompanionBody");
  let status;
  try { status = await api("/api/hostcompanion/status"); } catch (e) { status = { available: false }; }

  if (!status.available) {
    body.innerHTML = `<p>Not set up. This is entirely optional and separate from everything else Dockup
      does - it's a small helper that runs directly on your server (not in a container, called the
      dockup-companion) so Dockup can check host OS updates and manage Tailscale Serve, neither of
      which the Docker connection alone can reach.</p>
      <div class="btn-row align-center">
        <button class="btn btn-primary" id="companionInstallBtn">Install companion</button>
        <span class="hint">Installs a systemd service on this host, then reconnects Dockup to it automatically - Dockup briefly restarts itself as the last step.</span>
      </div>`;
    body.querySelector("#companionInstallBtn").addEventListener("click", (e) => installCompanion(e.target));
    return;
  }

  const os = status.os || {};
  const ts = status.tailscale || {};
  body.innerHTML = `<div class="form-grid">
    <div>
      <h3>Host OS updates</h3>
      <p class="hint">${esc(os.name || "Unknown OS")}${os.supported ? "" : " - not Debian/Ubuntu, updates aren't available here"}</p>
      ${os.supported ? `<div class="btn-row align-center">
        <button class="btn btn-steady" id="osCheckBtn">Check for updates</button>
        <button class="btn btn-primary btn-steady" id="osApplyBtn" disabled>Apply updates</button>
        <span class="hint" id="osResult"></span></div>` : ""}
    </div>
    <div>
      <h3>Tailscale</h3>
      ${!ts.installed
        ? '<p class="hint">Not installed on this host.</p><button class="btn" id="tsInstallBtn">Install Tailscale</button>'
        : `<p class="hint">${ts.running ? "✓ Running" : "Installed, not running"}${ts.dnsName ? ` - <code>${esc(ts.dnsName)}</code>` : ""}</p>`}
    </div>
  </div>
  <p class="hint mt-lg">Restart Docker and Reboot server are up in the top bar, next to Sign out.</p>`;

  if (os.supported) {
    let checkedCount = 0;
    body.querySelector("#osCheckBtn").addEventListener("click", async (e) => {
      e.target.disabled = true; e.target.textContent = "Checking…";
      try {
        const r = await api("/api/hostcompanion/os-update-check", { method: "POST", body: {} });
        checkedCount = r.upgradable;
        body.querySelector("#osResult").textContent = r.upgradable
          ? `${r.upgradable} package(s) can be updated.` : "Everything is up to date.";
        body.querySelector("#osApplyBtn").disabled = r.upgradable === 0;
      } catch (err) { popAlert(e.target, err.message, "danger"); }
      e.target.disabled = false; e.target.textContent = "Check for updates";
    });
    body.querySelector("#osApplyBtn").addEventListener("click", async (e) => {
      e.target.disabled = true; e.target.textContent = "Applying…";
      try {
        await api("/api/hostcompanion/os-update-apply", { method: "POST", body: {} });
        popAlert(e.target, "Host packages updated.", "success");
        body.querySelector("#osResult").textContent = "Up to date.";
      } catch (err) { popAlert(e.target, err.message, "danger"); }
      e.target.disabled = true; e.target.textContent = "Apply updates";
    });
  }
  if (!ts.installed) {
    body.querySelector("#tsInstallBtn").addEventListener("click", async (e) => {
      e.target.disabled = true; e.target.textContent = "Installing…";
      try {
        const r = await api("/api/hostcompanion/tailscale/install", { method: "POST", body: {} });
        popAlert(e.target, r.message, "success");
        await new Promise(res => setTimeout(res, 900));
        panel.remove();
        await renderHostCompanionPanel();
      } catch (err) { popAlert(e.target, err.message, "danger"); e.target.disabled = false; e.target.textContent = "Install Tailscale"; }
    });
  }
}

/* Top bar's Restart Docker / Reboot server buttons - checked once at
   boot (not per-route, the top bar is static markup) and shown only
   if the companion is actually installed, since both need real host
   access the Docker socket alone can't reach. */
async function initHostPowerButtons() {
  let status;
  try { status = await api("/api/hostcompanion/status"); } catch (e) { return; }
  if (!status.available) return;

  const dockerRestartBtn = document.getElementById("topbarDockerRestartBtn");
  dockerRestartBtn.classList.remove("hidden");
  dockerRestartBtn.dataset.tipOrig = "Restart Docker";
  armedAction(dockerRestartBtn, async () => {
    const r = await api("/api/hostcompanion/docker-restart", { method: "POST", body: {} });
    popAlert(dockerRestartBtn, r.message, "success");
  }, "restart Docker on the host");

  const rebootBtn = document.getElementById("topbarRebootBtn");
  rebootBtn.classList.remove("hidden");
  rebootBtn.dataset.tipOrig = "Reboot server";
  armedAction(rebootBtn, async () => {
    const r = await api("/api/hostcompanion/reboot", { method: "POST", body: {} });
    popAlert(rebootBtn, r.message, "success");
  }, "reboot the whole server");
}

async function renderTfa(host) {
  // The settings payload doesn't say whether 2FA is on; ask the begin/disable
  // endpoints to drive the flow and show state from what succeeds.
  host.innerHTML = `
    <div class="btn-row align-center">
      <button class="btn" id="tfaStart">Set up 2FA</button>
      <button class="btn" id="tfaOff">Turn 2FA off</button>
      <span class="hint">Optional. Adds a six-digit code from an authenticator app at sign-in.</span>
    </div>
    <div id="tfaFlow" class="mt-lg"></div>`;
  host.querySelector("#tfaStart").addEventListener("click", async () => {
    try {
      const r = await api("/api/2fa/begin", { method: "POST", body: {} });
      const flow = host.querySelector("#tfaFlow");
      flow.innerHTML = `<div class="form-grid">
        <p>Scan this with your authenticator app, then type the six-digit code it shows.</p>
        <div class="qr-box">${r.qr_svg}</div><!-- real SVG markup, not text - segno-generated server-side from a freshly random secret, never user input, so left un-esc()'d -->
        <p class="hint">Or enter the key by hand: <code>${esc(r.secret)}</code></p>
        <div class="field"><label for="tfaCode">Six-digit code</label><input id="tfaCode" inputmode="numeric" autocomplete="one-time-code"></div>
        <button class="btn btn-primary" id="tfaConfirm">Switch 2FA on</button></div>`;
      const confirmBtn = flow.querySelector("#tfaConfirm");
      confirmBtn.addEventListener("click", async () => {
        try {
          await api("/api/2fa/enable", { method: "POST", body: { code: flow.querySelector("#tfaCode").value } });
          flow.innerHTML = '<p>Two-factor is on. You’ll need your app next time you sign in.</p>';
        } catch (e) { popAlert(confirmBtn, e.message, "danger"); }
      });
    } catch (e) { popAlert(host.querySelector("#tfaStart"), e.message, "danger"); }
  });
  host.querySelector("#tfaOff").addEventListener("click", async () => {
    const flow = host.querySelector("#tfaFlow");
    flow.innerHTML = `<div class="form-grid">
      <div class="field"><label for="tfaOffCode">Enter a current six-digit code to turn 2FA off</label>
      <input id="tfaOffCode" inputmode="numeric"></div>
      <button class="btn btn-danger" id="tfaOffConfirm">Turn off</button></div>`;
    const offConfirmBtn = flow.querySelector("#tfaOffConfirm");
    offConfirmBtn.addEventListener("click", async () => {
      try {
        await api("/api/2fa/disable", { method: "POST", body: { code: flow.querySelector("#tfaOffCode").value } });
        flow.innerHTML = '<p>Two-factor is off.</p>';
      } catch (e) { popAlert(offConfirmBtn, e.message, "danger"); }
    });
  });
}

/* ---------- accent ---------- */

/* The highlight colour is picked from the palette itself rather than
   from a list of its names: a swatch says what you are choosing, a word
   does not. The real select still holds the value and still fires
   change, so the autosave below needs to know nothing about this. */
function renderAccentSwatches(sel) {
  const box = document.getElementById("accentSwatches");
  const name = document.getElementById("accentName");
  if (!box || !sel) return;
  const label = (a) => (a === "" ? "Brand yellow (default)" : a.replace("_", " "));
  box.innerHTML = "";
  [...sel.options].forEach((o, i) => {
    const sw = el(`<button type="button" class="swatch${o.value ? ` accent-${o.value}` : ""}"
      role="radio" aria-checked="false" data-tip="${esc(label(o.value))}"
      aria-label="${esc(label(o.value))}"></button>`);
    sw.addEventListener("click", () => {
      if (sel.selectedIndex === i) return;
      sel.selectedIndex = i;
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      paint();
    });
    box.appendChild(sw);
  });
  function paint() {
    [...box.children].forEach((sw, i) => {
      const on = i === sel.selectedIndex;
      sw.classList.toggle("chosen", on);
      sw.setAttribute("aria-checked", String(on));
    });
    name.textContent = label(sel.value);
  }
  sel.addEventListener("change", paint);
  paint();
}

function applyAccent(name) {
  if (name) document.documentElement.dataset.accent = name;
  else delete document.documentElement.dataset.accent;
  localStorage.setItem("dockup-accent", name || "");
}

/* ---------- boot ---------- */

window.addEventListener("hashchange", route);
applyAccent(localStorage.getItem("dockup-accent") || "");
refreshStacks().then(route);
renderVersions();
setInterval(() => { if ((location.hash || "#/") === "#/") refreshStacks(); }, 15000);
setInterval(renderVersions, 60000);
initHostPowerButtons();

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/static/sw.js");
