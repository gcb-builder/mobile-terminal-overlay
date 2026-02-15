// Service Worker for Mobile Terminal PWA
const CACHE_NAME = 'terminal-v108';

// Install event - cache essential assets
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

// Activate event - clean up old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheName !== CACHE_NAME) {
            console.log('Deleting old cache:', cacheName);
            return caches.delete(cacheName);
          }
        })
      );
    }).then(() => clients.claim())
  );
});

// Fetch event - network first, no caching for dynamic terminal content
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Don't intercept API calls - let browser handle directly (no SW overhead)
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  // Don't intercept WebSocket connections
  if (url.pathname.startsWith('/ws/')) {
    return;
  }

  // Network first strategy for static assets only
  event.respondWith(
    fetch(event.request).catch(() => {
      // Offline fallback for static assets only
      return caches.match(event.request);
    })
  );
});
