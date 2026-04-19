/**
 * Command queue: persistent queue of commands to send when terminal is idle.
 *
 * Reads: ctx.token, ctx.currentSession, ctx.activeTarget
 * DOM owned: #queueList, #queueCount, #queueBadge, #queueTabBadge,
 *            #queuePauseBtn, #queueSendNext, #queueFlush
 * Timers: none (scheduling is done server-side in CommandQueue._process_loop)
 */
import ctx from '../context.js';
import { escapeHtml } from '../utils.js';

// Module-local state
let queueItems = [];
let queuePaused = false;
let queueAutoSend = sessionStorage.getItem('mto_queue_autosend') === 'true'; // default: off (manual)

// Whether the "Previous" (sent items) section is expanded. Persists across
// drawer opens within a tab session — collapsed by default so the active
// queue is what catches the eye.
let previousExpanded = sessionStorage.getItem('mto_queue_prev_expanded') === 'true';

// Queue persistence constants
const QUEUE_STORAGE_PREFIX = 'mto_queue_';
const QUEUE_SENDING_TIMEOUT_MS = 30000;
const SENT_RETAIN_MS = 60000; // Keep sent items visible for 60s

// DOM refs (set in initQueue)
let queueList, queueCount, queueBadge, queueTabBadge;
let queuePauseBtn, queueSendNext, queueFlush, queueAutoToggle;

// ── Helpers ──────────────────────────────────────────────────────────

function makeQueueId() {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
        return crypto.randomUUID();
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
}

function getQueueStorageKey(session) {
    // Match the server's keying: with a pane it's "session:pane", without
    // a pane it's just "session". The previous `'default'` fallback meant
    // a no-pane queue lived under "session:default" on the client and
    // "session" on the server — items written then never reconciled.
    const sessKey = session || 'default';
    return QUEUE_STORAGE_PREFIX + sessKey + (ctx.activeTarget ? ':' + ctx.activeTarget : '');
}

export function saveQueueToStorage() {
    if (!ctx.currentSession) return;
    try {
        const key = getQueueStorageKey(ctx.currentSession);
        const data = { items: queueItems, savedAt: Date.now() };
        localStorage.setItem(key, JSON.stringify(data));
    } catch (e) {
        console.warn('Failed to save queue to storage:', e);
    }
}

function loadQueueFromStorage() {
    if (!ctx.currentSession) return [];
    try {
        const key = getQueueStorageKey(ctx.currentSession);
        const raw = localStorage.getItem(key);
        if (!raw) return [];

        const data = JSON.parse(raw);
        const items = data.items || [];
        const now = Date.now();

        for (const item of items) {
            if (item.status === 'sending') {
                const age = now - (item.lastAttemptAt || item.createdAt || 0);
                if (age > QUEUE_SENDING_TIMEOUT_MS) {
                    item.status = 'queued';
                    item.attempts = (item.attempts || 0);
                }
            }
        }

        // Keep queued, sending, and recently-sent items
        return items.filter(i => {
            if (i.status === 'queued' || i.status === 'sending') return true;
            if (i.status === 'sent') return (now - (i.sentAt || 0)) < SENT_RETAIN_MS;
            return false;
        });
    } catch (e) {
        console.warn('Failed to load queue from storage:', e);
        return [];
    }
}

function updatePauseButton() {
    if (!queuePauseBtn) return;
    queuePauseBtn.textContent = queuePaused ? 'Resume' : 'Pause';
    queuePauseBtn.classList.toggle('paused', queuePaused);
}

function updateAutoToggle() {
    if (!queueAutoToggle) return;
    queueAutoToggle.textContent = queueAutoSend ? 'Auto' : 'Manual';
    queueAutoToggle.classList.toggle('active', queueAutoSend);
}

// ── Core functions ───────────────────────────────────────────────────

/**
 * Render a single queue-item row. Returns an HTML string built from
 * escapeHtml-sanitized inputs. Same construction pattern as the original
 * inline render — extracted so Active and Previous sections can share it.
 */
function renderQueueRowHtml(item) {
    const displayText = item.text.length > 60 ? item.text.slice(0, 60) + '...' : item.text;
    const escapedText = escapeHtml(displayText);
    const isQueued = item.status === 'queued';
    const isSent = item.status === 'sent';
    const eid = escapeHtml(String(item.id));

    let actions = '';
    if (isQueued) {
        actions += '<button class="queue-send-btn" data-id="' + eid + '">Send</button>';
        actions += '<button class="queue-edit-btn" data-id="' + eid + '">Edit</button>';
    } else if (isSent) {
        actions += '<button class="queue-item-requeue" data-id="' + eid + '" title="Re-queue">&#x21BA;</button>';
    }
    actions += '<button class="queue-item-remove" data-id="' + eid + '">&times;</button>';

    const dragHandle = isQueued ? '<span class="queue-drag-handle" data-id="' + eid + '">&#x2261;</span>' : '';

    return '<div class="queue-item" data-id="' + eid + '" data-status="' + escapeHtml(item.status) + '">'
        + dragHandle
        + '<span class="queue-item-status ' + escapeHtml(item.status) + '"></span>'
        + '<div class="queue-item-content">'
        + '<div class="queue-item-text">' + (escapedText || '(Enter)') + '</div>'
        + '</div>'
        + '<div class="queue-item-actions">' + actions + '</div>'
        + '</div>';
}

/**
 * Build the "Previous" section (collapsible header + body) using DOM
 * APIs so we can safely insert event handlers and avoid raw HTML for
 * the section chrome. The row HTML itself is delegated to
 * renderQueueRowHtml which uses escapeHtml.
 */
function buildPreviousSection(previousItems) {
    const section = document.createElement('div');
    section.className = 'queue-section queue-section-previous';

    const header = document.createElement('div');
    header.className = 'queue-section-header';
    header.id = 'queuePrevHeader';

    const toggle = document.createElement('span');
    toggle.className = 'queue-section-toggle';
    toggle.textContent = previousExpanded ? '\u25BC' : '\u25B6';
    header.appendChild(toggle);

    const title = document.createElement('span');
    title.className = 'queue-section-title';
    title.textContent = 'Previous';
    header.appendChild(title);

    const count = document.createElement('span');
    count.className = 'queue-section-count';
    count.textContent = String(previousItems.length);
    header.appendChild(count);

    const clearBtn = document.createElement('button');
    clearBtn.className = 'queue-section-clear';
    clearBtn.id = 'queuePrevClear';
    clearBtn.title = 'Clear all previous';
    clearBtn.textContent = 'Clear';
    header.appendChild(clearBtn);

    section.appendChild(header);

    if (previousExpanded) {
        const body = document.createElement('div');
        body.className = 'queue-section-body';
        // Newest first — last sent at the top.
        const sorted = previousItems.slice().sort((a, b) => (b.sentAt || 0) - (a.sentAt || 0));
        const rowsHtml = sorted.map(renderQueueRowHtml).join('');
        // Single innerHTML assign of escapeHtml-sanitized content (same
        // pattern as the existing active section render).
        body.innerHTML = rowsHtml;
        section.appendChild(body);
    }

    return section;
}

/**
 * Render queue items in the drawer.
 *
 * Two visual sections:
 *   1. Active — items with status === 'queued' or 'sending'. The thing
 *      the user is actively managing.
 *   2. Previous — items with status === 'sent'. Kept as recent history
 *      so the user can re-queue if a send landed wrong, but visually
 *      separated and collapsed by default. Auto-purged after
 *      SENT_RETAIN_MS regardless. User can also "Clear" all from the
 *      section header.
 *
 * When both sections are empty, shows a single "Queue is empty" hint.
 */
export function renderQueueList() {
    if (!queueList) return;

    const active = queueItems.filter(i => i.status === 'queued' || i.status === 'sending');
    const previous = queueItems.filter(i => i.status === 'sent');

    if (active.length === 0 && previous.length === 0) {
        queueList.innerHTML = '<div class="queue-empty">Queue is empty</div>';
        if (queueCount) queueCount.textContent = '0';
        if (queueSendNext) queueSendNext.classList.remove('primary');
        updateQueueBadge(0);
        const sidebarQueueCount = document.getElementById('sidebarQueueCount');
        if (sidebarQueueCount) sidebarQueueCount.classList.add('hidden');
        return;
    }

    // Render active section using the existing innerHTML pattern with
    // escapeHtml-sanitized rows. If there are no active items, drop in
    // a small "All caught up" hint so the section isn't visually empty
    // when only Previous has content.
    let activeHtml;
    if (active.length === 0) {
        activeHtml = '<div class="queue-empty queue-active-empty">All caught up</div>';
    } else {
        activeHtml = active.map(renderQueueRowHtml).join('');
    }
    queueList.innerHTML = activeHtml;

    // Append the Previous section via DOM APIs so the click handlers
    // are bound to live nodes without re-querying.
    if (previous.length > 0) {
        queueList.appendChild(buildPreviousSection(previous));
    }

    // Counts: queueCount shows ALL items (parity with old behavior).
    if (queueCount) queueCount.textContent = queueItems.length.toString();
    if (queueSendNext) queueSendNext.classList.toggle('primary', active.length > 0);
    updateQueueBadge(active.length);

    // Sidebar badge tracks active queue, not previous.
    const sidebarQueueCount = document.getElementById('sidebarQueueCount');
    if (sidebarQueueCount) {
        sidebarQueueCount.textContent = active.length.toString();
        sidebarQueueCount.classList.toggle('hidden', active.length === 0);
    }

    // Action handlers are bound ONCE in initQueue via event delegation
    // on queueList. No per-render querySelectorAll/addEventListener pass
    // — that was O(n) DOM work per render and a leak risk if a render
    // raced with another (handlers stacking on the same element).
    setupQueueDragReorder();
}

/**
 * Touch/mouse drag reorder for queue items.
 */
function setupQueueDragReorder() {
    if (!queueList) return;

    let dragEl = null;
    let dragId = null;
    let startY = 0;
    let placeholder = null;

    function getY(e) {
        return e.touches ? e.touches[0].clientY : e.clientY;
    }

    function onStart(e) {
        const handle = e.target.closest('.queue-drag-handle');
        if (!handle) return;
        e.preventDefault();

        dragId = handle.dataset.id;
        dragEl = handle.closest('.queue-item');
        if (!dragEl) return;

        startY = getY(e);

        // Create placeholder
        placeholder = document.createElement('div');
        placeholder.className = 'queue-drag-placeholder';
        placeholder.style.height = dragEl.offsetHeight + 'px';
        dragEl.parentNode.insertBefore(placeholder, dragEl);

        // Float the dragged element
        dragEl.classList.add('dragging');
        dragEl.style.width = dragEl.offsetWidth + 'px';

        document.addEventListener('touchmove', onMove, { passive: false });
        document.addEventListener('mousemove', onMove);
        document.addEventListener('touchend', onEnd);
        document.addEventListener('mouseup', onEnd);
    }

    function onMove(e) {
        if (!dragEl) return;
        e.preventDefault();

        const y = getY(e);
        const dy = y - startY;
        dragEl.style.transform = `translateY(${dy}px)`;

        // Find which item we're over
        const items = [...queueList.querySelectorAll('.queue-item:not(.dragging)')];
        for (const item of items) {
            const rect = item.getBoundingClientRect();
            const mid = rect.top + rect.height / 2;
            if (y < mid) {
                queueList.insertBefore(placeholder, item);
                return;
            }
        }
        // Past all items — put at end
        queueList.appendChild(placeholder);
    }

    function onEnd() {
        if (!dragEl) return;

        document.removeEventListener('touchmove', onMove);
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('touchend', onEnd);
        document.removeEventListener('mouseup', onEnd);

        dragEl.classList.remove('dragging');
        dragEl.style.transform = '';
        dragEl.style.width = '';

        // Insert dragEl where placeholder is
        if (placeholder && placeholder.parentNode) {
            placeholder.parentNode.insertBefore(dragEl, placeholder);
            placeholder.remove();
        }

        // Rebuild queueItems order from DOM
        const newOrder = [];
        queueList.querySelectorAll('.queue-item').forEach(el => {
            const item = queueItems.find(i => i.id === el.dataset.id);
            if (item) newOrder.push(item);
        });
        // Add any items not in DOM (shouldn't happen but safety)
        for (const item of queueItems) {
            if (!newOrder.some(i => i.id === item.id)) newOrder.push(item);
        }
        queueItems.length = 0;
        queueItems.push(...newOrder);
        saveQueueToStorage();

        dragEl = null;
        dragId = null;
        placeholder = null;
    }

    queueList.addEventListener('touchstart', onStart, { passive: false });
    queueList.addEventListener('mousedown', onStart);
}

/**
 * Update queue badge visibility and count (both view bar and tab).
 */
export function updateQueueBadge(count) {
    if (queueBadge) {
        if (count > 0) {
            queueBadge.textContent = count.toString();
            queueBadge.classList.remove('hidden');
        } else {
            queueBadge.classList.add('hidden');
        }
    }
    if (queueTabBadge) {
        if (count > 0) {
            queueTabBadge.textContent = count.toString();
            queueTabBadge.classList.remove('hidden');
        } else {
            queueTabBadge.classList.add('hidden');
        }
    }
}

/**
 * Refresh queue list from server.
 */
export async function refreshQueueList() {
    if (!ctx.currentSession) return;

    try {
        const listParams = new URLSearchParams({ session: ctx.currentSession, token: ctx.token });
        if (ctx.activeTarget) listParams.set('pane_id', ctx.activeTarget);
        const resp = await ctx.apiFetch(`/api/queue/list?${listParams}`);
        if (resp.ok) {
            const data = await resp.json();
            queueItems = data.items || [];
            queuePaused = data.paused || false;
            updatePauseButton();
            renderQueueList();
        }
    } catch (e) {
        console.error('Failed to refresh queue:', e);
    }
}

/**
 * Enqueue a command.
 * Generates client-side ID for idempotency and persists to localStorage.
 */
export function isQueueAutoSend() { return queueAutoSend; }

export async function enqueueCommand(text, policy = 'auto', backlogId = null) {
    // In auto-send mode, override 'auto' policy to 'safe' so items send without manual Run
    if (policy === 'auto' && queueAutoSend) policy = 'safe';
    if (!ctx.currentSession) return false;

    const itemId = makeQueueId();

    const localItem = {
        id: itemId,
        text: text,
        policy: policy,
        status: 'queued',
        createdAt: Date.now(),
        attempts: 0
    };

    if (queueItems.some(i => i.id === itemId)) {
        console.warn('Duplicate queue item ID:', itemId);
        return false;
    }

    queueItems.push(localItem);
    saveQueueToStorage();
    renderQueueList();

    try {
        const params = new URLSearchParams({
            session: ctx.currentSession,
            text: text,
            policy: policy,
            id: itemId,
            token: ctx.token
        });
        if (ctx.activeTarget) params.set('pane_id', ctx.activeTarget);
        if (backlogId) params.set('backlog_id', backlogId);
        const resp = await ctx.apiFetch(`/api/queue/enqueue?${params}`, { method: 'POST' });

        if (resp.ok) {
            const data = await resp.json();
            const idx = queueItems.findIndex(i => i.id === itemId);
            if (idx >= 0) {
                // Server is authoritative for status. If the server says
                // the item is already 'sent' or 'failed' (idempotency
                // collision with a prior submission), keep that — don't
                // overwrite back to 'queued' from our local state.
                queueItems[idx] = { ...queueItems[idx], ...data.item };
                saveQueueToStorage();
                renderQueueList();
            }
            return true;
        }
    } catch (e) {
        console.error('Failed to enqueue to server:', e);
    }
    return true;
}

async function removeQueueItem(itemId) {
    if (!ctx.currentSession) return;

    queueItems = queueItems.filter(item => item.id !== itemId);
    saveQueueToStorage();
    renderQueueList();

    try {
        const params = new URLSearchParams({
            session: ctx.currentSession,
            item_id: itemId,
            token: ctx.token
        });
        if (ctx.activeTarget) params.set('pane_id', ctx.activeTarget);
        await ctx.apiFetch(`/api/queue/remove?${params}`, { method: 'POST' });
    } catch (e) {
        console.error('Failed to remove queue item from server:', e);
    }
}

async function toggleQueuePause() {
    if (!ctx.currentSession) return;

    // Optimistic update — flip immediately for instant feel
    queuePaused = !queuePaused;
    updatePauseButton();

    const endpoint = queuePaused ? '/api/queue/pause' : '/api/queue/resume';
    const params = new URLSearchParams({
        session: ctx.currentSession,
        token: ctx.token
    });
    if (ctx.activeTarget) params.set('pane_id', ctx.activeTarget);

    try {
        const resp = await ctx.apiFetch(`${endpoint}?${params}`, { method: 'POST' });
        if (!resp.ok) {
            // Revert on failure
            queuePaused = !queuePaused;
            updatePauseButton();
        }
    } catch (e) {
        console.error('Failed to toggle pause:', e);
        queuePaused = !queuePaused;
        updatePauseButton();
    }
}

function sendQueueItemNow(itemId) {
    const item = queueItems.find(i => i.id === itemId && i.status === 'queued');
    if (!item) return;
    if (ctx.sendTextAtomic) {
        ctx.sendTextAtomic(item.text, true);
        item.status = 'sent';
        item.sentAt = Date.now();
        saveQueueToStorage();
        renderQueueList();
        scheduleSentPurge();
    }
}

function insertNextToInput(specificId) {
    const item = specificId
        ? queueItems.find(i => i.id === specificId && i.status === 'queued')
        : queueItems.find(i => i.status === 'queued');
    if (!item) return;

    // Pass the original item's id along so compose's Send/Queue handler
    // can dequeue-then-re-enqueue atomically (awaited), avoiding the
    // race where the parallel remove+enqueue lets reconcileQueue (on a
    // WS reconnect) refetch the original from the server before the
    // remove POST landed — which produced visible duplicates.
    //
    // Side benefit: the original stays in the queue while the user is
    // editing. If they cancel the modal, nothing was lost.
    if (window.prefillCompose) {
        window.prefillCompose(item.text, item.id);
    }
}

async function reorderQueueItem(itemId, direction) {
    const idx = queueItems.findIndex(i => i.id === itemId);
    if (idx < 0) return;

    const newIdx = direction === 'up' ? idx - 1 : idx + 1;
    if (newIdx < 0 || newIdx >= queueItems.length) return;

    const tmp = queueItems[idx];
    queueItems[idx] = queueItems[newIdx];
    queueItems[newIdx] = tmp;
    saveQueueToStorage();
    renderQueueList();

    try {
        const params = new URLSearchParams({
            session: ctx.currentSession,
            item_id: itemId,
            new_index: newIdx.toString(),
            token: ctx.token
        });
        if (ctx.activeTarget) params.set('pane_id', ctx.activeTarget);
        await ctx.apiFetch(`/api/queue/reorder?${params}`, { method: 'POST' });
    } catch (e) {
        console.error('Failed to reorder queue item:', e);
        const tmp2 = queueItems[idx];
        queueItems[idx] = queueItems[newIdx];
        queueItems[newIdx] = tmp2;
        saveQueueToStorage();
        renderQueueList();
    }
}

async function flushQueue() {
    if (!ctx.currentSession) return;

    if (!confirm('Clear all queued commands?')) return;

    try {
        const params = new URLSearchParams({
            session: ctx.currentSession,
            confirm: 'true',
            token: ctx.token
        });
        if (ctx.activeTarget) params.set('pane_id', ctx.activeTarget);
        const resp = await ctx.apiFetch(`/api/queue/flush?${params}`, { method: 'POST' });

        if (resp.ok) {
            queueItems = [];
            renderQueueList();
        }
    } catch (e) {
        console.error('Failed to flush queue:', e);
    }
}

/**
 * Reconcile local queue with server state.
 * Server status wins for items on both sides.
 */
export async function reconcileQueue() {
    if (!ctx.currentSession) return;

    const localItems = loadQueueFromStorage();

    let serverItems = [];
    try {
        const listParams = new URLSearchParams({ session: ctx.currentSession, token: ctx.token });
        if (ctx.activeTarget) listParams.set('pane_id', ctx.activeTarget);
        const resp = await ctx.apiFetch(`/api/queue/list?${listParams}`);
        if (resp.ok) {
            const data = await resp.json();
            serverItems = data.items || [];
            queuePaused = data.paused || false;
            updatePauseButton();
        }
    } catch (e) {
        console.warn('Failed to fetch server queue for reconciliation:', e);
    }

    const serverMap = new Map(serverItems.map(i => [i.id, i]));

    const merged = [...serverItems];

    const toEnqueue = [];
    for (const local of localItems) {
        if (!serverMap.has(local.id)) {
            // Only re-enqueue items still in "queued" status.
            // Sent/completed items not on server = already processed, drop them.
            if (local.status === 'queued') {
                toEnqueue.push(local);
            }
        }
    }

    for (const item of toEnqueue) {
        try {
            const params = new URLSearchParams({
                session: ctx.currentSession,
                text: item.text,
                policy: item.policy || 'auto',
                id: item.id,
                token: ctx.token
            });
            if (ctx.activeTarget) params.set('pane_id', ctx.activeTarget);
            const resp = await ctx.apiFetch(`/api/queue/enqueue?${params}`, { method: 'POST' });
            if (resp.ok) {
                const data = await resp.json();
                if (data.is_new) {
                    merged.push(data.item);
                }
            }
        } catch (e) {
            console.warn('Failed to re-enqueue item:', item.id, e);
            merged.push(item);
        }
    }

    queueItems = merged;
    saveQueueToStorage();
    renderQueueList();

    console.log(`Queue reconciled: ${serverItems.length} server, ${localItems.length} local, ${toEnqueue.length} re-enqueued`);
}

/**
 * Does this message target the queue we're currently displaying?
 *
 * Server-side broadcasts now stamp every queue_* message with `session`
 * and `pane_id`. Older builds may omit them — accept those for backward
 * compatibility (better to show a possibly-extra item than to drop a real
 * one). When both sides are stamped, we silently ignore messages for
 * other panes/sessions so the visible list stays consistent.
 */
function messageTargetsCurrentView(msg) {
    if (msg.session != null && msg.session !== ctx.currentSession) return false;
    // pane_id can legitimately be null on either side ("session-level
    // queue, no specific pane"). Treat null/undefined as equal to the
    // current activeTarget being null/undefined.
    const msgPane = msg.pane_id ?? null;
    const myPane = ctx.activeTarget ?? null;
    if (msg.pane_id !== undefined && msgPane !== myPane) return false;
    return true;
}

/**
 * Handle queue WebSocket messages.
 * Persists changes to localStorage.
 */
export function handleQueueMessage(msg) {
    if (!messageTargetsCurrentView(msg)) return;

    switch (msg.type) {
        case 'queue_update':
            if (msg.action === 'add') {
                if (!queueItems.some(i => i.id === msg.item.id)) {
                    queueItems.push(msg.item);
                }
            } else if (msg.action === 'update') {
                const idx = queueItems.findIndex(i => i.id === msg.item.id);
                if (idx >= 0) queueItems[idx] = msg.item;
            } else if (msg.action === 'remove') {
                queueItems = queueItems.filter(i => i.id !== msg.item.id);
            }
            saveQueueToStorage();
            renderQueueList();
            break;

        case 'queue_sent': {
            const sentIdx = queueItems.findIndex(i => i.id === msg.id);
            if (sentIdx >= 0) {
                queueItems[sentIdx].status = 'sent';
                queueItems[sentIdx].sentAt = Date.now();
            }
            saveQueueToStorage();
            renderQueueList();
            scheduleSentPurge();
            break;
        }

        case 'queue_state':
            queuePaused = msg.paused;
            updatePauseButton();
            updateQueueBadge(msg.count);
            break;
    }
}

/**
 * Reload queue from localStorage for the current target and render.
 * Called when switching targets.
 */
export function reloadQueueForTarget() {
    queueItems = loadQueueFromStorage();
    renderQueueList();
}

// ── Getters used by terminal.js (manual Run path) ───────────────────

/** Get current queue items (read-only snapshot). */
export function getQueueItems() {
    return queueItems;
}

/** Whether the queue is currently paused. */
export function isQueuePaused() {
    return queuePaused;
}

/**
 * Mark the first queued item as "sent" and return it.
 * Item stays in the list (dimmed) so the user can re-queue if it landed wrong.
 * Auto-purged after SENT_RETAIN_MS.
 */
export function popNextQueueItem() {
    const idx = queueItems.findIndex(i => i.status === 'queued');
    if (idx < 0) return null;
    const item = queueItems[idx];
    item.status = 'sent';
    item.sentAt = Date.now();
    saveQueueToStorage();
    renderQueueList();
    scheduleSentPurge();
    return item;
}

/**
 * Mark a specific queued item as "sent" by ID and return it.
 * Kept exported for any future "send specific item" UI affordance.
 */
export function popNextQueueItemById(itemId) {
    const idx = queueItems.findIndex(i => i.id === itemId && i.status === 'queued');
    if (idx < 0) return null;
    const item = queueItems[idx];
    item.status = 'sent';
    item.sentAt = Date.now();
    saveQueueToStorage();
    renderQueueList();
    scheduleSentPurge();
    return item;
}

/**
 * Re-insert an item at the front of the queue (e.g. after a failed send).
 */
export function requeueItem(item) {
    // If item is still in the array (marked sent), just flip status back
    const idx = queueItems.findIndex(i => i.id === item.id);
    if (idx >= 0) {
        queueItems[idx].status = 'queued';
        delete queueItems[idx].sentAt;
    } else {
        item.status = 'queued';
        delete item.sentAt;
        queueItems.unshift(item);
    }
    saveQueueToStorage();
    renderQueueList();
}

// ── Sent item retention ─────────────────────────────────────────────

let sentPurgeTimer = null;

function scheduleSentPurge() {
    if (sentPurgeTimer) return; // Already scheduled
    sentPurgeTimer = setTimeout(() => {
        sentPurgeTimer = null;
        const now = Date.now();
        const before = queueItems.length;
        queueItems = queueItems.filter(i => {
            if (i.status !== 'sent') return true;
            return (now - (i.sentAt || 0)) < SENT_RETAIN_MS;
        });
        if (queueItems.length !== before) {
            saveQueueToStorage();
            renderQueueList();
        }
        // Re-schedule if sent items remain
        if (queueItems.some(i => i.status === 'sent')) {
            scheduleSentPurge();
        }
    }, SENT_RETAIN_MS);
}

/**
 * Re-queue a sent item (user recovery action).
 */
function requeueSentItem(itemId) {
    const idx = queueItems.findIndex(i => i.id === itemId && i.status === 'sent');
    if (idx < 0) return;
    queueItems[idx].status = 'queued';
    delete queueItems[idx].sentAt;
    saveQueueToStorage();
    renderQueueList();
    // Also re-enqueue on server
    const item = queueItems[idx];
    const params = new URLSearchParams({
        session: ctx.currentSession,
        text: item.text,
        policy: item.policy || 'auto',
        id: item.id,
        token: ctx.token,
    });
    if (ctx.activeTarget) params.set('pane_id', ctx.activeTarget);
    ctx.apiFetch(`/api/queue/enqueue?${params}`, { method: 'POST' }).catch(() => {});
}

// ── Public API ───────────────────────────────────────────────────────

// Re-entry guard. terminal.js wires this up from DOMContentLoaded but
// it could end up called twice (e.g. after a partial reload). Without a
// guard, every static button (Pause/Flush/Auto) accumulates duplicate
// listeners and fires N times per click.
let _queueInitialized = false;

/**
 * Bind queue event listeners. Idempotent — repeated calls are no-ops.
 * Called once from DOMContentLoaded.
 */
export function initQueue() {
    if (_queueInitialized) return;
    _queueInitialized = true;

    queueList = document.getElementById('queueList');
    queueCount = document.getElementById('queueCount');
    queueBadge = document.getElementById('queueBadge');
    queueTabBadge = document.getElementById('queueTabBadge');
    queuePauseBtn = document.getElementById('queuePauseBtn');
    queueSendNext = document.getElementById('queueSendNext');
    queueFlush = document.getElementById('queueFlush');
    queueAutoToggle = document.getElementById('queueAutoToggle');

    if (queuePauseBtn) queuePauseBtn.addEventListener('click', toggleQueuePause);
    // queueSendNext ("Run") is wired in terminal.js to call sendNextUnsafe()
    if (queueFlush) queueFlush.addEventListener('click', flushQueue);

    if (queueAutoToggle) {
        updateAutoToggle();
        queueAutoToggle.addEventListener('click', () => {
            queueAutoSend = !queueAutoSend;
            sessionStorage.setItem('mto_queue_autosend', queueAutoSend);
            updateAutoToggle();
        });
    }

    // Single delegated click handler on queueList — replaces N
    // per-render forEach/addEventListener passes that were O(items)
    // DOM work on every state change. event.target.closest() picks
    // the right action by class.
    if (queueList) {
        queueList.addEventListener('click', (e) => {
            // Most-specific match first; .closest() walks up to queueList.
            const removeBtn = e.target.closest('.queue-item-remove');
            if (removeBtn) {
                e.stopPropagation();
                removeQueueItem(removeBtn.dataset.id);
                return;
            }
            const sendBtn = e.target.closest('.queue-send-btn');
            if (sendBtn) {
                e.stopPropagation();
                sendQueueItemNow(sendBtn.dataset.id);
                return;
            }
            const editBtn = e.target.closest('.queue-edit-btn');
            if (editBtn) {
                e.stopPropagation();
                insertNextToInput(editBtn.dataset.id);
                return;
            }
            const requeueBtn = e.target.closest('.queue-item-requeue');
            if (requeueBtn) {
                e.stopPropagation();
                requeueSentItem(requeueBtn.dataset.id);
                return;
            }
            // Previous-section "Clear all" button. Stop propagation so
            // the header toggle below doesn't also fire.
            if (e.target.closest('#queuePrevClear')) {
                e.stopPropagation();
                queueItems = queueItems.filter(i => i.status !== 'sent');
                saveQueueToStorage();
                renderQueueList();
                return;
            }
            // Previous-section header: toggle expand/collapse.
            if (e.target.closest('#queuePrevHeader')) {
                previousExpanded = !previousExpanded;
                sessionStorage.setItem('mto_queue_prev_expanded', String(previousExpanded));
                renderQueueList();
                return;
            }
        });
    }

    refreshQueueList();
}
