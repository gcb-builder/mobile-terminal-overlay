/**
 * Command Palette: mobile-first searchable command overlay.
 *
 * Reads: ctx.uiMode
 * DOM owned: #paletteOverlay, #paletteInput, #paletteResults, #paletteClose
 * Storage: localStorage key 'mto_palette_recents'
 */
import ctx from '../context.js';
import { escapeHtml } from '../utils.js';

// ── Constants ─────────────────────────────────────────────────────────
const RECENTS_KEY = 'mto_palette_recents';
const MAX_RECENTS = 20;
const MAX_VISIBLE = 50;

// ── Module-local state ────────────────────────────────────────────────
let isOpen = false;
let commands = [];
let filteredCommands = [];
let selectedIndex = 0;
let recents = {};

// DOM refs
let overlay, input, resultsContainer;

// ── Public API ────────────────────────────────────────────────────────

export function initPalette(callbacks) {
    overlay = document.getElementById('paletteOverlay');
    input = document.getElementById('paletteInput');
    resultsContainer = document.getElementById('paletteResults');
    const closeBtn = document.getElementById('paletteClose');

    if (!overlay || !input || !resultsContainer) return;

    loadRecents();
    registerBuiltinCommands(callbacks);

    input.addEventListener('input', onInputChange);
    input.addEventListener('keydown', onInputKeydown);

    closeBtn?.addEventListener('click', closePalette);

    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closePalette();
    });

    setupSwipeToDismiss();
    setupHeaderLongPress();
}

export function openPalette(initialQuery = '') {
    if (isOpen) return;
    isOpen = true;
    overlay.classList.remove('hidden');
    overlay.classList.add('visible');
    input.value = initialQuery;
    selectedIndex = 0;

    if (initialQuery) {
        const scored = filterAndSort(initialQuery);
        renderResults(scored);
    } else {
        renderResults(getInitialResults());
    }

    requestAnimationFrame(() => input.focus());
}

export function closePalette() {
    if (!isOpen) return;
    isOpen = false;
    overlay.classList.remove('visible');
    setTimeout(() => { if (!isOpen) overlay.classList.add('hidden'); }, 200);
    input.blur();
}

// ── Command Registration ──────────────────────────────────────────────

function registerBuiltinCommands(cb) {
    // Navigation — drawer tabs
    const navTabs = [
        { id: 'nav.queue',   label: 'Go to Queue',   icon: '\u23F3', tab: 'queue',   aliases: ['commands', 'pending'] },
        { id: 'nav.runner',  label: 'Go to Runner',  icon: '\u25B6', tab: 'runner',  aliases: ['build', 'test', 'lint'] },
        { id: 'nav.history', label: 'Go to History',  icon: '\u231A', tab: 'history', aliases: ['git', 'snapshots', 'rollback'] },
        { id: 'nav.mcp',     label: 'Go to MCP',      icon: '\u2699', tab: 'mcp',     aliases: ['servers', 'tools'] },
        { id: 'nav.plugins', label: 'Go to Plugins',  icon: '\u{1F9E9}', tab: 'plugins', aliases: ['extensions'] },
        { id: 'nav.activity',label: 'Go to Activity', icon: '\u{23F1}', tab: 'activity', aliases: ['timeline', 'events'] },
    ];
    navTabs.forEach(t => commands.push({
        ...t, category: 'Navigation',
        action: () => cb.openSurface(t.tab),
    }));

    // Views
    commands.push(
        { id: 'view.log', label: 'Switch to Log', category: 'Views',
          icon: '\u{1F4C4}', action: () => cb.switchToView('log'),
          aliases: ['output', 'messages'] },
        { id: 'view.terminal', label: 'Switch to Terminal', category: 'Views',
          icon: '\u2587', action: () => cb.switchToView('terminal'),
          aliases: ['shell', 'tmux', 'raw'] },
        { id: 'view.team', label: 'Switch to Team', category: 'Views',
          icon: '\u2261', action: () => cb.switchToView('team'),
          aliases: ['agents', 'multi'] },
    );

    // Actions
    commands.push(
        { id: 'action.compose', label: 'Open Compose', category: 'Actions',
          icon: '\u270E', action: () => document.getElementById('composeBtn')?.click(),
          aliases: ['send', 'message', 'type'] },
        { id: 'action.docs', label: 'Open Docs', category: 'Actions',
          icon: '\u{1F4C4}', action: () => document.getElementById('docsBtn')?.click(),
          aliases: ['plans', 'context', 'documentation'] },
        { id: 'action.interrupt', label: 'Interrupt (Ctrl+C)', category: 'Actions',
          icon: '\u25A0', action: () => cb.sendInterrupt(),
          aliases: ['stop', 'kill', 'cancel', 'abort'] },
        { id: 'action.refresh', label: 'Refresh', category: 'Actions',
          icon: '\u21BB', action: () => document.getElementById('refreshBtn')?.click(),
          aliases: ['reload'] },
        { id: 'action.restart', label: 'Restart Agent', category: 'Actions',
          icon: '\u{1F504}', action: () => cb.respawnProcess(),
          aliases: ['respawn', 'reboot'] },
        { id: 'action.launchTeam', label: 'Launch Team', category: 'Actions',
          icon: '\u{1F680}', action: () => cb.showLaunchTeamModal(),
          aliases: ['multi-agent', 'spawn'] },
    );

    // Runner
    commands.push(
        { id: 'runner.build', label: 'Run Build', category: 'Runner',
          icon: '\u{1F528}', action: () => cb.executeRunnerCommand('build'),
          aliases: ['compile'] },
        { id: 'runner.test', label: 'Run Tests', category: 'Runner',
          icon: '\u2705', action: () => cb.executeRunnerCommand('test'),
          aliases: ['pytest', 'jest'] },
        { id: 'runner.lint', label: 'Run Lint', category: 'Runner',
          icon: '\u{1F50D}', action: () => cb.executeRunnerCommand('lint'),
          aliases: ['check', 'ruff'] },
    );
}

// ── Fuzzy Search ──────────────────────────────────────────────────────

function fuzzyScore(query, target) {
    if (!query || !target) return -1;
    const q = query.toLowerCase();
    const t = target.toLowerCase();

    if (t.startsWith(q)) return 100;

    const words = t.split(/[\s\-_./]+/);
    if (words.some(w => w.startsWith(q))) return 80;

    if (t.includes(q)) return 60;

    // Fuzzy char-by-char
    let qi = 0;
    for (let ti = 0; ti < t.length && qi < q.length; ti++) {
        if (t[ti] === q[qi]) qi++;
    }
    if (qi === q.length) return Math.max(10, 40 - (t.length - q.length));

    return -1;
}

function scoreCommand(query, cmd) {
    const scores = [
        fuzzyScore(query, cmd.label) * 1.5,
        fuzzyScore(query, cmd.category),
        ...(cmd.aliases || []).map(a => fuzzyScore(query, a) * 1.2),
    ].filter(s => s > 0);

    if (scores.length === 0) return -1;
    return Math.max(...scores) + getRecencyBoost(cmd.id);
}

function filterAndSort(query) {
    const scored = [];
    for (const cmd of commands) {
        const s = scoreCommand(query, cmd);
        if (s > 0) scored.push({ ...cmd, _score: s });
    }
    scored.sort((a, b) => b._score - a._score);
    return scored.slice(0, MAX_VISIBLE);
}

// ── Initial State ─────────────────────────────────────────────────────

function getInitialResults() {
    // Recents first (up to 5)
    const recentIds = Object.entries(recents)
        .sort((a, b) => b[1].lastUsed - a[1].lastUsed)
        .slice(0, 5)
        .map(([id]) => id);

    const recentCmds = recentIds
        .map(id => commands.find(c => c.id === id))
        .filter(Boolean)
        .map(c => ({ ...c, category: 'Recent' }));

    const remaining = commands.filter(c => !recentIds.includes(c.id));
    return [...recentCmds, ...remaining];
}

// ── Rendering ─────────────────────────────────────────────────────────

function renderResults(cmds) {
    if (cmds.length === 0) {
        resultsContainer.innerHTML = '<div class="palette-empty">No matching commands</div>';
        filteredCommands = [];
        selectedIndex = -1;
        return;
    }

    // Group by category
    const groups = {};
    for (const cmd of cmds) {
        const cat = cmd.category || 'Other';
        if (!groups[cat]) groups[cat] = [];
        groups[cat].push(cmd);
    }

    const ORDER = ['Recent', 'Navigation', 'Views', 'Actions', 'Runner'];
    const orderedKeys = ORDER.filter(k => groups[k]);
    const extra = Object.keys(groups).filter(k => !ORDER.includes(k));
    const allKeys = [...orderedKeys, ...extra];

    const query = input.value.trim();
    let html = '';
    let idx = 0;
    for (const cat of allKeys) {
        html += `<div class="palette-group">`;
        html += `<div class="palette-group-label">${escapeHtml(cat)}</div>`;
        for (const cmd of groups[cat]) {
            const sel = idx === selectedIndex ? ' selected' : '';
            html += `<div class="palette-item${sel}" data-id="${escapeHtml(cmd.id)}" data-index="${idx}" role="option">`;
            html += `<span class="palette-item-icon">${cmd.icon || ''}</span>`;
            html += `<span class="palette-item-label">${query ? highlightMatch(cmd.label, query) : escapeHtml(cmd.label)}</span>`;
            if (cat !== 'Recent') {
                html += `<span class="palette-item-badge">${escapeHtml(cmd.category)}</span>`;
            }
            if (cmd.shortcut) {
                html += `<span class="palette-item-shortcut">${escapeHtml(cmd.shortcut)}</span>`;
            }
            html += `</div>`;
            idx++;
        }
        html += `</div>`;
    }

    resultsContainer.innerHTML = html;
    filteredCommands = cmds;

    // Event delegation for clicks
    resultsContainer.onclick = (e) => {
        const item = e.target.closest('.palette-item');
        if (item) onResultClick(item.dataset.id);
    };
}

function highlightMatch(text, query) {
    if (!query) return escapeHtml(text);
    const q = query.toLowerCase();
    const t = text.toLowerCase();
    const start = t.indexOf(q);
    if (start === -1) return escapeHtml(text);
    return escapeHtml(text.slice(0, start))
        + '<mark>' + escapeHtml(text.slice(start, start + q.length)) + '</mark>'
        + escapeHtml(text.slice(start + q.length));
}

// ── Event Handlers ────────────────────────────────────────────────────

function onInputChange() {
    const query = input.value.trim();
    selectedIndex = 0;
    if (query) {
        renderResults(filterAndSort(query));
    } else {
        renderResults(getInitialResults());
    }
}

function onInputKeydown(e) {
    const total = filteredCommands.length;

    switch (e.key) {
        case 'ArrowDown':
            e.preventDefault();
            if (total > 0) updateSelection(Math.min(selectedIndex + 1, total - 1));
            break;
        case 'ArrowUp':
            e.preventDefault();
            if (total > 0) updateSelection(Math.max(selectedIndex - 1, 0));
            break;
        case 'Enter':
            e.preventDefault();
            if (selectedIndex >= 0 && selectedIndex < total) {
                onResultClick(filteredCommands[selectedIndex].id);
            }
            break;
        case 'Escape':
            e.preventDefault();
            closePalette();
            break;
    }
}

function updateSelection(newIndex) {
    const prev = resultsContainer.querySelector(`.palette-item[data-index="${selectedIndex}"]`);
    if (prev) prev.classList.remove('selected');
    selectedIndex = newIndex;
    const next = resultsContainer.querySelector(`.palette-item[data-index="${selectedIndex}"]`);
    if (next) {
        next.classList.add('selected');
        next.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
}

function onResultClick(commandId) {
    const cmd = commands.find(c => c.id === commandId);
    if (!cmd) return;
    closePalette();
    recordUsage(commandId);
    requestAnimationFrame(() => {
        try { cmd.action(); }
        catch (err) {
            console.error('Palette command failed:', err);
            ctx.showToast?.(`Command failed: ${err.message}`, 'error');
        }
    });
}

// ── Recency Tracking ──────────────────────────────────────────────────

function loadRecents() {
    try {
        const raw = localStorage.getItem(RECENTS_KEY);
        recents = raw ? JSON.parse(raw) : {};
    } catch { recents = {}; }
}

function saveRecents() {
    try {
        const entries = Object.entries(recents)
            .sort((a, b) => b[1].lastUsed - a[1].lastUsed)
            .slice(0, MAX_RECENTS);
        recents = Object.fromEntries(entries);
        localStorage.setItem(RECENTS_KEY, JSON.stringify(recents));
    } catch { /* ignore */ }
}

function recordUsage(commandId) {
    if (!recents[commandId]) recents[commandId] = { count: 0, lastUsed: 0 };
    recents[commandId].count++;
    recents[commandId].lastUsed = Date.now();
    saveRecents();
}

function getRecencyBoost(commandId) {
    const entry = recents[commandId];
    if (!entry) return 0;
    const ageHours = (Date.now() - entry.lastUsed) / 3600000;
    const timeFactor = Math.max(0, 30 - ageHours * 2);
    const freqFactor = Math.min(20, entry.count * 4);
    return timeFactor + freqFactor;
}

// ── Gesture Handlers ──────────────────────────────────────────────────

function setupHeaderLongPress() {
    const header = document.querySelector('.header');
    if (!header) return;
    let pressTimer = null;
    header.addEventListener('touchstart', () => {
        if (isOpen) return;
        pressTimer = setTimeout(() => openPalette(), 500);
    }, { passive: true });
    header.addEventListener('touchend', () => clearTimeout(pressTimer));
    header.addEventListener('touchmove', () => clearTimeout(pressTimer));
    header.addEventListener('touchcancel', () => clearTimeout(pressTimer));
}

function setupSwipeToDismiss() {
    let startY = 0;
    let startedOnBackdrop = false;
    overlay.addEventListener('touchstart', (e) => {
        startY = e.touches[0].clientY;
        // Only track swipe if touch started on the backdrop, not inside scrollable content
        startedOnBackdrop = (e.target === overlay);
    }, { passive: true });
    overlay.addEventListener('touchend', (e) => {
        if (startedOnBackdrop && startY - e.changedTouches[0].clientY > 80) closePalette();
    }, { passive: true });
}
