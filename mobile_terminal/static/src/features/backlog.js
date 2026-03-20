/**
 * Backlog: project-scoped list of deferred / possible work items.
 *
 * Reads: ctx.token
 * DOM owned: #backlogList, #backlogCount, #backlogTabBadge, #backlogAddBtn
 */
import ctx from '../context.js';
import { escapeHtml } from '../utils.js';
import { enqueueCommand } from './queue.js';

// Module-local state
let backlogItems = [];
let currentProject = '';

// DOM refs (set in initBacklog)
let backlogList, backlogCount, backlogTabBadge, backlogAddBtn;

// ── Storage ────────────────────────────────────────────────────────────

function getStorageKey(project) {
    const safe = (project || 'default').replace(/\//g, '_').replace(/:/g, '_');
    return 'mto_backlog_' + safe;
}

function saveToStorage() {
    if (!currentProject) return;
    try {
        localStorage.setItem(getStorageKey(currentProject), JSON.stringify({
            items: backlogItems, savedAt: Date.now()
        }));
    } catch (e) { /* quota exceeded — non-fatal */ }
}

function loadFromStorage(project) {
    try {
        const raw = localStorage.getItem(getStorageKey(project));
        if (!raw) return [];
        const data = JSON.parse(raw);
        return data.items || [];
    } catch (e) {
        return [];
    }
}

// ── Badges ─────────────────────────────────────────────────────────────

function updateBadges() {
    const pending = backlogItems.filter(i => i.status === 'pending').length;
    if (backlogTabBadge) {
        backlogTabBadge.textContent = pending.toString();
        backlogTabBadge.classList.toggle('hidden', pending === 0);
    }
    const sidebarCount = document.getElementById('sidebarBacklogCount');
    if (sidebarCount) {
        sidebarCount.textContent = pending.toString();
        sidebarCount.classList.toggle('hidden', pending === 0);
    }
    const railBadge = document.getElementById('toolsBacklogBadge');
    if (railBadge) {
        railBadge.textContent = pending.toString();
        railBadge.classList.toggle('hidden', pending === 0);
    }
}

// ── Render ─────────────────────────────────────────────────────────────

export function renderBacklogList() {
    if (!backlogList) return;

    updateBadges();
    if (backlogCount) backlogCount.textContent = backlogItems.length.toString();

    if (backlogItems.length === 0) {
        backlogList.innerHTML = '<div class="backlog-empty">Backlog is empty</div>';
        return;
    }

    // Sort: pending first, then queued, then done/dismissed
    const order = { pending: 0, queued: 1, done: 2, dismissed: 3 };
    const sorted = [...backlogItems].sort((a, b) =>
        (order[a.status] ?? 9) - (order[b.status] ?? 9)
    );

    backlogList.innerHTML = sorted.map(item => {
        const eid = escapeHtml(String(item.id));
        const summary = item.summary.length > 60
            ? item.summary.slice(0, 60) + '\u2026'
            : item.summary;
        const isDone = item.status === 'done' || item.status === 'dismissed';
        const isQueued = item.status === 'queued';

        let actions = '';
        if (!isDone && !isQueued) {
            actions += `<button class="backlog-queue-btn" data-id="${eid}">Queue</button>`;
            actions += `<button class="backlog-done-btn" data-id="${eid}" data-action="done">Done</button>`;
        } else if (isQueued) {
            actions += `<span style="font-size:10px;color:var(--warning);padding:4px 6px">Queued</span>`;
        }
        actions += `<button class="backlog-item-remove" data-id="${eid}" title="Remove">&times;</button>`;

        return `<div class="backlog-item" data-id="${eid}" data-status="${escapeHtml(item.status)}">
            <div class="backlog-item-dot ${escapeHtml(item.status)}"></div>
            <div class="backlog-item-body">
                <div class="backlog-item-summary">${escapeHtml(summary)}</div>
                <div class="backlog-item-meta">
                    <span class="backlog-source-badge ${escapeHtml(item.source)}">${escapeHtml(item.source)}</span>
                </div>
            </div>
            <div class="backlog-item-actions">${actions}</div>
        </div>`;
    }).join('');

    // Bind events
    backlogList.querySelectorAll('.backlog-queue-btn').forEach(btn => {
        btn.addEventListener('click', e => { e.stopPropagation(); queueBacklogItem(btn.dataset.id); });
    });
    backlogList.querySelectorAll('.backlog-done-btn').forEach(btn => {
        btn.addEventListener('click', e => { e.stopPropagation(); updateBacklogStatus(btn.dataset.id, 'done'); });
    });
    backlogList.querySelectorAll('.backlog-item-remove').forEach(btn => {
        btn.addEventListener('click', e => { e.stopPropagation(); removeBacklogItem(btn.dataset.id); });
    });
}

// ── Actions ────────────────────────────────────────────────────────────

async function queueBacklogItem(itemId) {
    const item = backlogItems.find(i => i.id === itemId);
    if (!item || item.status === 'queued' || item.status === 'done') return;

    // Enqueue the prompt as a queue command, linking back to this backlog item
    const success = await enqueueCommand(item.prompt, 'auto', item.id);
    if (success === false) return;

    // Update backlog status
    await updateBacklogStatus(itemId, 'queued');
}

async function updateBacklogStatus(itemId, status) {
    // Optimistic update
    const idx = backlogItems.findIndex(i => i.id === itemId);
    if (idx >= 0) {
        backlogItems[idx] = { ...backlogItems[idx], status, updated_at: Date.now() / 1000 };
        saveToStorage();
        renderBacklogList();
    }

    try {
        const params = new URLSearchParams({
            id: itemId, status, token: ctx.token
        });
        if (currentProject) params.set('project', currentProject);
        const resp = await fetch(`/api/backlog/update?${params}`, { method: 'POST' });
        if (resp.ok) {
            const data = await resp.json();
            if (data.status === 'ok' && data.item) {
                const i = backlogItems.findIndex(x => x.id === itemId);
                if (i >= 0) backlogItems[i] = data.item;
                saveToStorage();
                renderBacklogList();
            }
        }
    } catch (e) {
        console.error('Failed to update backlog item:', e);
    }
}

async function removeBacklogItem(itemId) {
    backlogItems = backlogItems.filter(i => i.id !== itemId);
    saveToStorage();
    renderBacklogList();

    try {
        const params = new URLSearchParams({ id: itemId, token: ctx.token });
        if (currentProject) params.set('project', currentProject);
        await fetch(`/api/backlog/remove?${params}`, { method: 'POST' });
    } catch (e) {
        console.error('Failed to remove backlog item:', e);
    }
}

function openAddDialog() {
    const summary = prompt('Item summary:');
    if (!summary) return;
    const promptText = prompt('Instruction (what to send when queued):', summary);
    if (promptText === null) return;
    addBacklogItem(summary, promptText || summary, 'human');
}

async function addBacklogItem(summary, promptText, source = 'human') {
    try {
        const params = new URLSearchParams({
            summary, prompt: promptText, source, token: ctx.token
        });
        if (currentProject) params.set('project', currentProject);
        const resp = await fetch(`/api/backlog/add?${params}`, { method: 'POST' });
        if (resp.ok) {
            const data = await resp.json();
            if (data.status === 'ok' && data.item) {
                if (!backlogItems.some(i => i.id === data.item.id)) {
                    backlogItems.push(data.item);
                }
                saveToStorage();
                renderBacklogList();
            }
        }
    } catch (e) {
        console.error('Failed to add backlog item:', e);
    }
}

// ── Server Refresh ─────────────────────────────────────────────────────

export async function refreshBacklogList() {
    if (!currentProject) return;
    try {
        const params = new URLSearchParams({ project: currentProject, token: ctx.token });
        const resp = await fetch(`/api/backlog/list?${params}`);
        if (resp.ok) {
            const data = await resp.json();
            backlogItems = data.items || [];
            saveToStorage();
            renderBacklogList();
        }
    } catch (e) {
        console.error('Failed to refresh backlog:', e);
    }
}

export function reloadBacklogForProject(project) {
    currentProject = project || '';
    backlogItems = loadFromStorage(currentProject);
    renderBacklogList();
    refreshBacklogList();
}

// ── WS Message Handler ─────────────────────────────────────────────────

export function handleBacklogMessage(msg) {
    if (msg.type !== 'backlog_update') return;

    if (msg.action === 'add') {
        if (!backlogItems.some(i => i.id === msg.item.id)) {
            backlogItems.push(msg.item);
        }
    } else if (msg.action === 'update') {
        const idx = backlogItems.findIndex(i => i.id === msg.item.id);
        if (idx >= 0) backlogItems[idx] = msg.item;
        else backlogItems.push(msg.item);
    } else if (msg.action === 'remove') {
        backlogItems = backlogItems.filter(i => i.id !== msg.item.id);
    }

    saveToStorage();
    renderBacklogList();
}

// ── Init ───────────────────────────────────────────────────────────────

export function initBacklog(project) {
    backlogList = document.getElementById('backlogList');
    backlogCount = document.getElementById('backlogCount');
    backlogTabBadge = document.getElementById('backlogTabBadge');
    backlogAddBtn = document.getElementById('backlogAddBtn');

    if (backlogAddBtn) {
        backlogAddBtn.addEventListener('click', openAddDialog);
    }

    currentProject = project || '';
    if (currentProject) {
        backlogItems = loadFromStorage(currentProject);
        renderBacklogList();
        refreshBacklogList();
    }
}
