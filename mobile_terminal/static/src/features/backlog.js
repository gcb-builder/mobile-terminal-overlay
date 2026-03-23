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
let candidateItems = [];
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
    const total = pending + candidateItems.length;
    if (backlogTabBadge) {
        backlogTabBadge.textContent = total.toString();
        backlogTabBadge.classList.toggle('hidden', total === 0);
    }
    const sidebarCount = document.getElementById('sidebarBacklogCount');
    if (sidebarCount) {
        sidebarCount.textContent = total.toString();
        sidebarCount.classList.toggle('hidden', total === 0);
    }
    const railBadge = document.getElementById('toolsBacklogBadge');
    if (railBadge) {
        railBadge.textContent = total.toString();
        railBadge.classList.toggle('hidden', total === 0);
    }
}

// ── Render ─────────────────────────────────────────────────────────────

export function renderBacklogList() {
    if (!backlogList) return;

    updateBadges();
    const totalCount = backlogItems.length + candidateItems.length;
    if (backlogCount) backlogCount.textContent = totalCount.toString();

    let html = '';

    // Candidate tray (suggestions from JSONL interception)
    if (candidateItems.length > 0) {
        html += '<div class="backlog-candidate-tray">';
        html += '<div class="backlog-candidate-header">';
        html += '<span class="backlog-candidate-title">Suggestions</span>';
        html += `<span class="backlog-candidate-count">${candidateItems.length}</span>`;
        html += '</div>';
        html += candidateItems.map(c => {
            const eid = escapeHtml(String(c.id));
            const summary = (c.summary || '').length > 60
                ? c.summary.slice(0, 60) + '\u2026' : c.summary;
            const tool = escapeHtml(c.source_tool || '');
            return `<div class="backlog-candidate-item" data-id="${eid}">
                <div class="backlog-item-dot candidate"></div>
                <div class="backlog-item-body">
                    <div class="backlog-item-summary">${escapeHtml(summary)}</div>
                    <div class="backlog-item-meta">
                        <span class="backlog-origin-badge jsonl_candidate">${tool}</span>
                    </div>
                </div>
                <div class="backlog-item-actions">
                    <button class="backlog-keep-btn" data-id="${eid}">Keep</button>
                    <button class="backlog-dismiss-btn" data-id="${eid}">&times;</button>
                </div>
            </div>`;
        }).join('');
        html += '</div>';
    }

    // Main backlog items
    if (backlogItems.length === 0 && candidateItems.length === 0) {
        backlogList.innerHTML = '<div class="backlog-empty">Backlog is empty</div>';
        return;
    }

    if (backlogItems.length > 0) {
        // Sort: pending first, then queued, then done/dismissed
        const order = { pending: 0, queued: 1, done: 2, dismissed: 3 };
        const sorted = [...backlogItems].sort((a, b) =>
            (order[a.status] ?? 9) - (order[b.status] ?? 9)
        );

        html += sorted.map(item => {
            const eid = escapeHtml(String(item.id));
            const summary = item.summary.length > 60
                ? item.summary.slice(0, 60) + '\u2026'
                : item.summary;
            const isDone = item.status === 'done' || item.status === 'dismissed';
            const isQueued = item.status === 'queued';

            let actions = '';
            if (!isDone && !isQueued) {
                actions += `<button class="backlog-queue-btn" data-id="${eid}">Queue</button>`;
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
    }

    backlogList.innerHTML = html;

    // Bind candidate events
    backlogList.querySelectorAll('.backlog-keep-btn').forEach(btn => {
        btn.addEventListener('click', e => { e.stopPropagation(); keepCandidate(btn.dataset.id); });
    });
    backlogList.querySelectorAll('.backlog-dismiss-btn').forEach(btn => {
        btn.addEventListener('click', e => { e.stopPropagation(); dismissCandidate(btn.dataset.id); });
    });

    // Bind backlog item events
    backlogList.querySelectorAll('.backlog-queue-btn').forEach(btn => {
        btn.addEventListener('click', e => { e.stopPropagation(); queueBacklogItem(btn.dataset.id); });
    });
    backlogList.querySelectorAll('.backlog-item-remove').forEach(btn => {
        btn.addEventListener('click', e => { e.stopPropagation(); removeBacklogItem(btn.dataset.id); });
    });
}

// ── Candidate Actions ─────────────────────────────────────────────────

async function keepCandidate(candidateId) {
    // Optimistic: remove from candidates
    candidateItems = candidateItems.filter(c => c.id !== candidateId);
    renderBacklogList();

    try {
        const params = new URLSearchParams({ id: candidateId, token: ctx.token });
        if (currentProject) params.set('project', currentProject);
        const resp = await fetch(`/api/backlog/candidates/keep?${params}`, { method: 'POST' });
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
        console.error('Failed to keep candidate:', e);
    }
}

async function dismissCandidate(candidateId) {
    candidateItems = candidateItems.filter(c => c.id !== candidateId);
    renderBacklogList();

    try {
        const params = new URLSearchParams({ id: candidateId, token: ctx.token });
        if (currentProject) params.set('project', currentProject);
        await fetch(`/api/backlog/candidates/dismiss?${params}`, { method: 'POST' });
    } catch (e) {
        console.error('Failed to dismiss candidate:', e);
    }
}

// ── Backlog Actions ───────────────────────────────────────────────────

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

export async function addBacklogItem(summary, promptText, source = 'human') {
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

export async function refreshCandidates() {
    try {
        const params = new URLSearchParams({ token: ctx.token });
        if (currentProject) params.set('project', currentProject);
        const resp = await fetch(`/api/backlog/candidates?${params}`);
        if (resp.ok) {
            const data = await resp.json();
            candidateItems = data.candidates || [];
            renderBacklogList();
        }
    } catch (e) {
        console.error('Failed to refresh candidates:', e);
    }
}

export function reloadBacklogForProject(project) {
    currentProject = project || '';
    backlogItems = loadFromStorage(currentProject);
    candidateItems = [];  // candidates are ephemeral, don't persist
    renderBacklogList();
    refreshBacklogList();
    refreshCandidates();
}

// ── WS Message Handlers ───────────────────────────────────────────────

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

export function handleCandidateMessage(payload) {
    if (!payload) return;

    if (payload.action === 'new' && payload.candidate) {
        if (!candidateItems.some(c => c.id === payload.candidate.id)) {
            candidateItems.push(payload.candidate);
            renderBacklogList();
        }
    } else if (payload.action === 'dismissed' && payload.candidate_id) {
        candidateItems = candidateItems.filter(c => c.id !== payload.candidate_id);
        renderBacklogList();
    }
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
        refreshCandidates();
    }
}
