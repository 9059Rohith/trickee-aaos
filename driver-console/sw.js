const CACHE_PREFIX = 'trickee-driver-shell-';
const CACHE = CACHE_PREFIX + 'v10';
const SHELL = ['/driver', '/driver/app.js', '/driver/styles.css', '/driver/manifest.webmanifest', '/assets/trickee-logo.png', '/assets/manrope-latin.woff2', '/assets/syne-latin.woff2', '/assets/michroma-latin.woff2', '/assets/leaflet/leaflet.css', '/assets/leaflet/leaflet.js'];
SHELL.push('/assets/workspace.css', '/assets/workspace.js');
SHELL.push('/assets/personal.css', '/assets/personal.js');
SHELL.push('/assets/navigation.css', '/assets/navigation.js', '/assets/files.css', '/assets/files.js');
self.addEventListener('install', event => event.waitUntil(
  caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE)
      .map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin
      || url.search || !SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(request).then(response => {
    if (response.ok && !response.redirected
        && !/\bno-store\b/i.test(response.headers.get('Cache-Control') || '')) {
      const copy = response.clone();
      event.waitUntil(caches.open(CACHE).then(cache => cache.put(request, copy)).catch(() => {}));
    }
    return response;
  }).catch(() => caches.open(CACHE).then(cache => cache.match(request))));
});
