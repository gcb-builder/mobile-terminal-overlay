/**
 * Activity Timeline: structured event feed of agent actions.
 *
 * Reads: ctx.token, ctx.activeTarget
 * DOM owned: #activityList, #activityPhaseBanner, .activity-filter-pill
 * Timers: pollController (AbortController)
 */
import ctx from '../context.js';
import { escapeHtml, formatTimeAgo, abortableSleep } from '../utils.js';

// ── Module state ──────────────────────────────────────────────────────
let activeCategory = 'all';
let lastModified = 0;
let pollController = null;
let events = [];

const POLL_INTERVAL = 3000;
const IDLE_POLL_INTERVAL = 10000;

const CATEGORY_ICONS = {
    tools: '\u2699',
    files: '\uD83D\uDCDD',
    tests: '\u2713',
    git: '\uD83D\uDD00',
    errors: '\u26A0',
};

// ── Public API ────────────────────────────────────────────────────────

export function initActivity() {
    document.querySelectorAll('.activity-filter-pill').forEach(pill => {
        pill.addEventListener('click', () => {
            document.querySelectorAll('.activity-filter-pill').forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            activeCategory = pill.dataset.category;
            applyFilter();
        });
    });
}

export async function loadActivity() {
    await fetchAndRender();
    startPolling();
}

export function stopActivity() {
    if (pollController) {
        pollController.abort();
        pollController = null;
    }
}

// ── Fetch & Render ────────────────────────────────────────────────────

async function fetchAndRender() {
    const list = document.getElementById('activityList');
    if (!list) return;

    try {
        const paneParam = ctx.activeTarget ? `&pane_id=${encodeURIComponent(ctx.activeTarget)}` : '';
        const resp = await fetch(`/api/activity?token=${ctx.token}${paneParam}&limit=150`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        if (data.modified === lastModified && events.length > 0) return;
        lastModified = data.modified || 0;

        events = data.events || [];
        renderEvents(list);
        updatePhaseBanner();
    } catch (err) {
        console.debug('Activity fetch error:', err);
        if (events.length === 0) {
            list.innerHTML = '<div class="activity-empty">No activity yet</div>';
        }
    }
}

function renderEvents(list) {
    if (events.length === 0) {
        list.innerHTML = '<div class="activity-empty">No activity yet</div>';
        return;
    }

    const frag = document.createDocumentFragment();

    // Group events by group_id
    const groups = new Map();
    const ordered = []; // track first-seen order of group_ids

    for (const evt of events) {
        const gid = evt.group_id;
        if (gid) {
            if (!groups.has(gid)) {
                groups.set(gid, []);
                ordered.push(gid);
            }
            groups.get(gid).push(evt);
        } else {
            ordered.push(evt);
        }
    }

    for (const item of ordered) {
        if (typeof item === 'string') {
            // group_id
            const groupEvents = groups.get(item);
            if (groupEvents.length === 1) {
                frag.appendChild(createEventCard(groupEvents[0]));
            } else {
                frag.appendChild(createEventGroup(item, groupEvents));
            }
        } else {
            // ungrouped event
            frag.appendChild(createEventCard(item));
        }
    }

    list.innerHTML = '';
    list.appendChild(frag);
    applyFilter();
}

function createEventCard(evt) {
    const card = document.createElement('div');
    card.className = 'activity-event';
    card.dataset.category = evt.category;

    const icon = CATEGORY_ICONS[evt.category] || '\u2022';
    const timeAgo = evt.ts_epoch ? formatTimeAgo(evt.ts_epoch) : '';

    let badgeHtml = '';
    if (evt.status_badge) {
        const cls = evt.status === 'error' ? 'activity-badge-error' : 'activity-badge-ok';
        badgeHtml = `<span class="activity-badge ${cls}">${escapeHtml(evt.status_badge)}</span>`;
    }

    card.innerHTML = `<div class="activity-event-header" role="button" tabindex="0">` +
        `<span class="activity-event-icon">${icon}</span>` +
        `<span class="activity-event-title">${escapeHtml(evt.title)}</span>` +
        badgeHtml +
        `<span class="activity-event-time">${escapeHtml(timeAgo)}</span>` +
        `</div>`;

    if (evt.detail) {
        const detail = document.createElement('div');
        detail.className = 'activity-event-detail hidden';
        detail.textContent = evt.detail;
        card.appendChild(detail);

        card.querySelector('.activity-event-header').addEventListener('click', () => {
            detail.classList.toggle('hidden');
            card.classList.toggle('expanded');
        });
    }

    return card;
}

function createEventGroup(groupId, groupEvents) {
    const details = document.createElement('details');
    details.className = 'activity-group';
    details.dataset.groupId = groupId;

    // Compute category counts
    const catCounts = {};
    groupEvents.forEach(evt => {
        const cat = evt.category || 'other';
        catCounts[cat] = (catCounts[cat] || 0) + 1;
    });

    // Duration
    const timestamps = groupEvents.map(e => e.ts_epoch || 0).filter(t => t > 0);
    const startTs = Math.min(...timestamps);
    const endTs = Math.max(...timestamps);
    const durationMs = endTs - startTs;
    let durationStr = '';
    if (durationMs > 60000) durationStr = `${Math.round(durationMs / 60000)}m`;
    else if (durationMs > 1000) durationStr = `${Math.round(durationMs / 1000)}s`;

    const badges = Object.entries(catCounts)
        .map(([cat, count]) => `${count} ${cat}`)
        .join(', ');

    const timeAgo = groupEvents[0]?.ts_epoch ? formatTimeAgo(groupEvents[0].ts_epoch) : '';

    const summary = document.createElement('summary');
    summary.className = 'activity-group-header';
    summary.innerHTML =
        `<span class="activity-group-icon">\u25B6</span>` +
        `<span class="activity-group-title">${groupEvents.length} actions</span>` +
        `<span class="activity-group-badges">${escapeHtml(badges)}</span>` +
        (durationStr ? `<span class="activity-group-duration">${escapeHtml(durationStr)}</span>` : '') +
        `<span class="activity-event-time">${escapeHtml(timeAgo)}</span>`;

    details.appendChild(summary);

    const content = document.createElement('div');
    content.className = 'activity-group-content';
    groupEvents.forEach(evt => content.appendChild(createEventCard(evt)));
    details.appendChild(content);

    return details;
}

function applyFilter() {
    const list = document.getElementById('activityList');
    if (!list) return;

    // Filter individual events
    list.querySelectorAll('.activity-event').forEach(el => {
        if (activeCategory === 'all' || el.dataset.category === activeCategory) {
            el.classList.remove('filtered-out');
        } else {
            el.classList.add('filtered-out');
        }
    });

    // Show/hide groups based on visible children
    list.querySelectorAll('.activity-group').forEach(group => {
        const visible = group.querySelectorAll('.activity-event:not(.filtered-out)');
        group.classList.toggle('filtered-out', visible.length === 0);
    });
}

// ── Phase Banner ──────────────────────────────────────────────────────

async function updatePhaseBanner() {
    const banner = document.getElementById('activityPhaseBanner');
    const iconEl = document.getElementById('activityPhaseIcon');
    const textEl = document.getElementById('activityPhaseText');
    if (!banner || !iconEl || !textEl) return;

    try {
        const paneParam = ctx.activeTarget ? `&pane_id=${encodeURIComponent(ctx.activeTarget)}` : '';
        const resp = await fetch(`/api/health/agent?token=${ctx.token}${paneParam}`);
        if (!resp.ok) { banner.classList.add('hidden'); return; }
        const data = await resp.json();

        if (!data.phase || data.phase === 'idle') {
            banner.classList.add('hidden');
            return;
        }

        const icons = { working: '\u2699', planning: '\uD83D\uDCCB', waiting: '\u23F3', running_task: '\uD83E\uDD16' };
        iconEl.textContent = icons[data.phase] || '\u2022';
        textEl.textContent = data.detail || data.phase;
        banner.classList.remove('hidden');
    } catch {
        banner.classList.add('hidden');
    }
}

// ── Polling ───────────────────────────────────────────────────────────

function startPolling() {
    stopActivity();
    pollController = new AbortController();
    const signal = pollController.signal;

    (async () => {
        while (!signal.aborted) {
            try {
                // Back off when no recent events
                const interval = events.length === 0 ? IDLE_POLL_INTERVAL : POLL_INTERVAL;
                await abortableSleep(interval, signal);
                await fetchAndRender();
            } catch (e) {
                if (e.name === 'AbortError') break;
                console.debug('Activity poll error:', e);
                try { await abortableSleep(5000, signal); } catch { break; }
            }
        }
    })();
}
