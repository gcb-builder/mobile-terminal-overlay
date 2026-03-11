/**
 * Command queue: persistent queue of commands to send when terminal is idle.
 *
 * Reads: ctx.token, ctx.currentSession, ctx.activeTarget
 * DOM owned: #queueList, #queueCount, #queueBadge, #queueTabBadge,
 *            #queuePauseBtn, #queueSendNext, #queueFlush
 * Timers: none (scheduling is done by terminal.js via tryDrainQueue)
 */
import ctx from '../context.js';
import { escapeHtml } from '../utils.js';

// Module-local state
let queueItems = [];
let queuePaused = false;

// Queue persistence constants
const QUEUE_STORAGE_PREFIX = 'mto_queue_';
const QUEUE_SENDING_TIMEOUT_MS = 30000;
const SENT_RETAIN_MS = 60000; // Keep sent items visible for 60s

// DOM refs (set in initQueue)
let queueList, queueCount, queueBadge, queueTabBadge;
let queuePauseBtn, queueSendNext, queueFlush;

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
    const pane = ctx.activeTarget || 'default';
    return QUEUE_STORAGE_PREFIX + (session || 'default') + ':' + pane;
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

// ── Core functions ───────────────────────────────────────────────────

/**
 * Render queue items in the drawer.
 */
export function renderQueueList() {
    if (!queueList) return;

    if (queueItems.length === 0) {
        queueList.innerHTML = '<div class="queue-empty">Queue is empty</div>';
        if (queueCount) queueCount.textContent = '0';
        updateQueueBadge(0);
        return;
    }

    const queuedIndices = [];
    queueItems.forEach((item, i) => { if (item.status === 'queued') queuedIndices.push(i); });
    const firstQueued = queuedIndices[0];
    const lastQueued = queuedIndices[queuedIndices.length - 1];

    queueList.innerHTML = queueItems.map((item, idx) => {
        const displayText = item.text.length > 40 ? item.text.slice(0, 40) + '...' : item.text;
        const escapedText = escapeHtml(displayText);
        const isQueued = item.status === 'queued';
        const isSent = item.status === 'sent';
        const escapedId = escapeHtml(String(item.id));
        let actionsHtml = '';
        if (isQueued) {
            actionsHtml = `
                <div class="queue-item-reorder">
                    <button class="queue-reorder-btn up" data-id="${escapedId}" data-dir="up"${idx === firstQueued ? ' style="visibility:hidden"' : ''}>&#x25B2;</button>
                    <button class="queue-reorder-btn down" data-id="${escapedId}" data-dir="down"${idx === lastQueued ? ' style="visibility:hidden"' : ''}>&#x25BC;</button>
                </div>`;
        } else if (isSent) {
            actionsHtml = `<button class="queue-item-requeue" data-id="${escapedId}" title="Re-queue">&#x21BA;</button>`;
        }
        return `
            <div class="queue-item" data-id="${escapedId}" data-status="${escapeHtml(item.status)}">
                <span class="queue-item-status ${escapeHtml(item.status)}"></span>${actionsHtml}
                <div class="queue-item-content">
                    <div class="queue-item-text">${escapedText || '(Enter)'}</div>
                    <div class="queue-item-meta">
                        <span class="queue-item-policy ${escapeHtml(item.policy)}">${escapeHtml(item.policy)}</span>
                        ${isSent ? '<span class="queue-item-sent-label">sent</span>' : ''}
                    </div>
                </div>
                <button class="queue-item-remove" data-id="${escapedId}">&times;</button>
            </div>
        `;
    }).join('');

    const queuedCount = queueItems.filter(i => i.status === 'queued').length;
    if (queueCount) queueCount.textContent = queueItems.length.toString();
    updateQueueBadge(queuedCount);

    // Update sidebar queue count badge
    const sidebarQueueCount = document.getElementById('sidebarQueueCount');
    if (sidebarQueueCount) {
        sidebarQueueCount.textContent = queuedCount.toString();
        sidebarQueueCount.classList.toggle('hidden', queuedCount === 0);
    }

    queueList.querySelectorAll('.queue-item-remove').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            removeQueueItem(btn.dataset.id);
        });
    });

    queueList.querySelectorAll('.queue-item[data-status="queued"]').forEach(el => {
        el.addEventListener('click', () => {
            insertNextToInput(el.dataset.id);
        });
    });

    queueList.querySelectorAll('.queue-reorder-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            reorderQueueItem(btn.dataset.id, btn.dataset.dir);
        });
    });

    queueList.querySelectorAll('.queue-item-requeue').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            requeueSentItem(btn.dataset.id);
        });
    });
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
        const resp = await fetch(`/api/queue/list?${listParams}`);
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
export async function enqueueCommand(text, policy = 'auto') {
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
        const resp = await fetch(`/api/queue/enqueue?${params}`, { method: 'POST' });

        if (resp.ok) {
            const data = await resp.json();
            const idx = queueItems.findIndex(i => i.id === itemId);
            if (idx >= 0) {
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
        await fetch(`/api/queue/remove?${params}`, { method: 'POST' });
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
        const resp = await fetch(`${endpoint}?${params}`, { method: 'POST' });
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

function insertNextToInput(specificId) {
    const item = specificId
        ? queueItems.find(i => i.id === specificId && i.status === 'queued')
        : queueItems.find(i => i.status === 'queued');
    if (!item) return;

    const logInput = document.getElementById('logInput');
    if (logInput) {
        logInput.value = item.text;
        logInput.focus();
    }

    removeQueueItem(item.id);
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
        await fetch(`/api/queue/reorder?${params}`, { method: 'POST' });
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
        const resp = await fetch(`/api/queue/flush?${params}`, { method: 'POST' });

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
        const resp = await fetch(`/api/queue/list?${listParams}`);
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
            toEnqueue.push(local);
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
            const resp = await fetch(`/api/queue/enqueue?${params}`, { method: 'POST' });
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
 * Handle queue WebSocket messages.
 * Persists changes to localStorage.
 */
export function handleQueueMessage(msg) {
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

// ── Getters for terminal.js (tryDrainQueue) ──────────────────────────

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
 * Used by sendNextSafe to pop a specific safe item.
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
    fetch(`/api/queue/enqueue?${params}`, { method: 'POST' }).catch(() => {});
}

// ── Public API ───────────────────────────────────────────────────────

/**
 * Bind queue event listeners. Called once from DOMContentLoaded.
 */
export function initQueue() {
    queueList = document.getElementById('queueList');
    queueCount = document.getElementById('queueCount');
    queueBadge = document.getElementById('queueBadge');
    queueTabBadge = document.getElementById('queueTabBadge');
    queuePauseBtn = document.getElementById('queuePauseBtn');
    queueSendNext = document.getElementById('queueSendNext');
    queueFlush = document.getElementById('queueFlush');

    if (queuePauseBtn) queuePauseBtn.addEventListener('click', toggleQueuePause);
    // queueSendNext ("Run") is wired in terminal.js to call sendNextUnsafe()
    if (queueFlush) queueFlush.addEventListener('click', flushQueue);

    refreshQueueList();
}
