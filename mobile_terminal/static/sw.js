// Service Worker for Mobile Terminal PWA
// __BASE_PATH is injected by the server at the top of this file
const CACHE_NAME = 'terminal-v337';

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
  const bp = (typeof __BASE_PATH !== 'undefined') ? __BASE_PATH : '';

  // Don't intercept other apps on the same origin (e.g. /brain/, /code/)
  if (url.pathname.startsWith('/brain/') || url.pathname.startsWith('/code/') || url.pathname.startsWith('/paperless/')) {
    return;
  }

  // Don't intercept API calls - let browser handle directly (no SW overhead)
  if (url.pathname.startsWith(bp + '/api/')) {
    return;
  }

  // Don't intercept WebSocket connections
  if (url.pathname.startsWith(bp + '/ws/')) {
    return;
  }

  // Don't intercept static assets - server sets Cache-Control headers,
  // SW interception causes stale responses in PWA standalone mode
  if (url.pathname.startsWith(bp + '/static/')) {
    return;
  }
});

// Push notification handler - per-type actions
self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : {};
  const bp = (typeof __BASE_PATH !== 'undefined') ? __BASE_PATH : '';
  let actions = [];
  if (data.type === 'permission') {
    actions = [
      { action: 'allow', title: 'Allow' },
      { action: 'deny', title: 'Deny' },
    ];
  } else if (data.type === 'completed') {
    actions = [
      { action: 'open', title: 'Open' },
    ];
  } else if (data.type === 'crashed') {
    actions = [
      { action: 'respawn', title: 'Respawn' },
      { action: 'open', title: 'Open' },
    ];
  }
  const options = {
    body: data.body || 'Agent needs your attention',
    icon: bp + '/static/apple-touch-icon.png',
    badge: bp + '/static/apple-touch-icon.png',
    vibrate: [200, 100, 200],
    data: {
      type: data.type,
      url: bp + '/',
      session: data.session || '',
      pane_id: data.pane_id || '',
    },
    actions: actions,
  };
  event.waitUntil(
    self.registration.showNotification(data.title || 'Terminal', options)
  );
});

// Notification click handler - per-type routing
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const notifData = event.notification.data || {};
  const bp = (typeof __BASE_PATH !== 'undefined') ? __BASE_PATH : '';

  if (event.action === 'allow' || event.action === 'deny') {
    // Permission response
    event.waitUntil(
      clients.matchAll({ type: 'window' }).then(cls => {
        if (cls.length > 0) {
          cls[0].postMessage({
            type: 'permission_response',
            choice: event.action === 'allow' ? 'y' : 'n'
          });
          cls[0].focus();
        } else {
          // No client window — pass action via URL params so startup handler can send it
          const params = new URLSearchParams({
            action: event.action,
            pane_id: notifData.pane_id || '',
            session: notifData.session || '',
          });
          clients.openWindow(bp + '/?' + params.toString());
        }
      })
    );
  } else if (event.action === 'respawn') {
    // Respawn agent in the target pane
    event.waitUntil(
      clients.matchAll({ type: 'window' }).then(cls => {
        if (cls.length > 0) {
          cls[0].postMessage({
            type: 'respawn_agent',
            session: notifData.session,
            pane_id: notifData.pane_id,
          });
          cls[0].focus();
        } else {
          // No client window open - open with respawn action params
          const params = new URLSearchParams({
            action: 'respawn',
            pane_id: notifData.pane_id || '',
            session: notifData.session || '',
          });
          clients.openWindow(bp + '/?' + params.toString());
        }
      })
    );
  } else {
    // Default tap or 'open' action
    event.waitUntil(clients.openWindow(bp + '/'));
  }
});
