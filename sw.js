const shellCache = 'halaqah-web-v2';
const shell = new URL('index.html', self.registration.scope).href;
self.addEventListener('install', event => {
  event.waitUntil(caches.open(shellCache).then(cache => cache.add(shell)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.mode === 'navigate' && request.url.startsWith(self.registration.scope)) {
    event.respondWith(fetch(request).catch(() => caches.match(shell)));
    return;
  }
  if (request.method === 'GET' && request.url.startsWith(new URL('images/', self.registration.scope).href)) {
    event.respondWith(caches.open(shellCache).then(async cache => {
      const stored = await cache.match(request);
      if (stored) return stored;
      const response = await fetch(request);
      if (response.ok) await cache.put(request, response.clone());
      return response;
    }));
  }
});
