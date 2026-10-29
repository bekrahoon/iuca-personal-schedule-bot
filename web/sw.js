// Кэш оболочки приложения: открывается и без интернета (данные — из localStorage).
const CACHE = "muca-v4";
const SHELL = ["/app", "/app/manifest.webmanifest", "/app/icon-192.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  // сначала сеть (чтобы обновления приходили сразу), без сети — кэш
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok && (url.origin === location.origin || url.hostname.endsWith("gstatic.com") || url.hostname.endsWith("googleapis.com"))) {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return r;
    }).catch(() => caches.match(e.request, { ignoreSearch: true }).then(r => r || caches.match("/app")))
  );
});

// ---------- будильник и напоминания ----------
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { title: e.data && e.data.text() }; }
  const alarm = !!d.alarm;
  e.waitUntil(self.registration.showNotification(d.title || "Расписание", {
    body: d.body || "",
    tag: d.tag || "muca",
    renotify: true,
    requireInteraction: alarm,          // будильник висит, пока не смахнёшь
    vibrate: alarm ? [600, 200, 600, 200, 600, 200, 1000] : [300, 100, 300],
    icon: "/app/icon-192.png",
    badge: "/app/icon-192.png",
    data: { url: d.url || "/app" },
  }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/app";
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(ws => {
    for (const w of ws) if (w.url.includes("/app")) { w.navigate(url).catch(() => {}); return w.focus(); }
    return clients.openWindow(url);
  }));
});
