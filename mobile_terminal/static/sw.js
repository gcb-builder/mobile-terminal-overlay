// Service Worker for Mobile Terminal PWA
const CACHE_NAME = 'terminal-v114';

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

// Push notification handler
self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : {};
  const options = {
    body: data.body || 'Claude needs your attention',
    icon: '/static/apple-touch-icon.png',
    badge: '/static/apple-touch-icon.png',
    vibrate: [200, 100, 200],
    data: { type: data.type, url: '/' },
    actions: data.type === 'permission' ? [
      { action: 'allow', title: 'Allow' },
      { action: 'deny', title: 'Deny' },
    ] : [],
  };
  event.waitUntil(
    self.registration.showNotification(data.title || 'Terminal', options)
  );
});

// Notification click handler
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  if (event.action === 'allow' || event.action === 'deny') {
    event.waitUntil(
      clients.matchAll({ type: 'window' }).then(cls => {
        if (cls.length > 0) {
          cls[0].postMessage({
            type: 'permission_response',
            choice: event.action === 'allow' ? 'y' : 'n'
          });
          cls[0].focus();
        } else {
          clients.openWindow('/');
        }
      })
    );
  } else {
    event.waitUntil(clients.openWindow('/'));
  }
});
