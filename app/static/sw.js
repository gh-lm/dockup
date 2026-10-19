/* Dockup service worker: keep a copy of the static shell so the app still
   opens if the network blips. Network first, so updates always win;
   API calls never touch the cache - stale container states would mislead. */
const CACHE = "dockup-static-v5";
const ASSETS = [
  "/static/css/app.css",
  "/static/css/editor.css",
  "/static/js/app.js",
  "/static/js/auth.js",
  "/static/vendor/xterm.js",
  "/static/vendor/xterm.css",
  "/static/vendor/codemirror6/codemirror6.bundle.js",
  "/static/fonts/TrindleSans-Variable.woff2",
  "/static/icons/dockup.svg",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method === "GET" && url.pathname.startsWith("/static/")) {
    e.respondWith(
      fetch(e.request, { cache: "no-store" })
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
  }
});
