// Service Worker for Mobile Terminal PWA
// __BASE_PATH is injected by the server at the top of this file
const CACHE_NAME = 'terminal-v398';

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
      permission_id: data.permission_id || '',
    },
    actions: actions,
  };
  event.waitUntil(Promise.all([
    self.registration.showNotification(data.title || 'Terminal', options),
    // Wake any backgrounded tabs that are still in memory but whose WS
    // died at the NAT. Tab reacts on the message and forces a reconnect
    // before the user even taps the notification. No-op when the page
    // has been evicted (matchAll returns empty) — push tap then opens
    // a fresh client which reconnects normally.
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cls => {
      cls.forEach(c => {
        try { c.postMessage({ type: 'sw_wake', push_type: data.type }); }
        catch (e) { /* postMessage failure on a dead client — ignore */ }
      });
    }),
  ]));
});

// Notification click handler - per-type routing
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const notifData = event.notification.data || {};
  const bp = (typeof __BASE_PATH !== 'undefined') ? __BASE_PATH : '';

  if (event.action === 'allow' || event.action === 'deny') {
    // Permission response — include pane_id from the original push
    // payload so the page sends y/n to the pane that asked, not to
    // whatever pane is globally active (which could be a different
    // pane by the time the user taps the notification).
    event.waitUntil(
      clients.matchAll({ type: 'window' }).then(cls => {
        if (cls.length > 0) {
          cls[0].postMessage({
            type: 'permission_response',
            choice: event.action === 'allow' ? 'y' : 'n',
            pane_id: notifData.pane_id || '',
            permission_id: notifData.permission_id || '',
          });
          cls[0].focus();
        } else {
          // No client window — pass action via URL params so startup handler can send it
          const params = new URLSearchParams({
            action: event.action,
            pane_id: notifData.pane_id || '',
            session: notifData.session || '',
            permission_id: notifData.permission_id || '',
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
