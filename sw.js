const shellCache = 'halaqah-web-v2';
const shell = new URL('index.html', self.registration.scope).href;
self.addEventListener('install', event => {
  event.waitUntil(caches.open(shellCache).then(cache => cache.add(new Request(shell, {cache:'reload'}))).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
async function navigate(request) {
  const cache = await caches.open(shellCache);
  try {
    const response = await fetch(request);
    if (response.ok && response.headers.get('content-type')?.startsWith('text/html')) {
      await cache.put(shell, response.clone()).catch(() => {});
      return response;
    }
    return (await cache.match(shell)) || response;
  } catch {
    return (await cache.match(shell)) || Response.error();
  }
}
self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  const root = new URL(self.registration.scope);
  if (request.mode === 'navigate' && url.origin === root.origin &&
      (url.pathname === root.pathname || url.pathname === new URL(shell).pathname)) {
    event.respondWith(navigate(request));
    return;
  }
  if (request.method === 'GET' && request.url.startsWith(new URL('images/', self.registration.scope).href)) {
    event.respondWith(caches.open(shellCache).then(async cache => {
      const stored = await cache.match(request);
      if (stored) return stored;
      const response = await fetch(request);
      if (response.ok) await cache.put(request, response.clone()).catch(() => {});
      return response;
    }));
  }
});
