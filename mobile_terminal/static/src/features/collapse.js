/**
 * Tool collapse: groups consecutive duplicate tool entries in the log view.
 *
 * Reads: nothing from ctx (DOM-only module)
 * DOM owned: .collapse-count badges, .collapsed-duplicate, .tool-supergroup headers
 * Timers: requestIdleCallback for deferred collapse
 */

// Module-local state
let logContentEl = null;
let lastCollapseHash = '';
let expandedGroups = new Set();
const scheduleIdle = window.requestIdleCallback || ((cb) => setTimeout(cb, 100));

// Super-collapse state for grouping many tool calls into single row
const SUPER_COLLAPSE_THRESHOLD = 6;
let lastSuperCollapseHash = '';
let expandedSuperGroups = new Set();

// ── Regular collapse ─────────────────────────────────────────────────

/**
 * Schedule tool collapse for idle time.
 * Computes hash to skip if content unchanged.
 */
export function scheduleCollapse() {
    if (!logContentEl) return;

    const tools = logContentEl.querySelectorAll('.log-tool');
    if (tools.length < 2) return;

    const lastTool = tools[tools.length - 1];
    const hash = `${tools.length}:${lastTool?.dataset.toolKey || ''}:${logContentEl.innerHTML.length}`;

    if (hash === lastCollapseHash) return;

    scheduleIdle(() => {
        try {
            collapseRepeatedTools(hash);
        } catch (e) {
            console.warn('Collapse failed:', e);
        }
    }, { timeout: 500 });
}

/**
 * Single-pass collapse of consecutive duplicate tools.
 * Adds badge to first, hides rest unless expanded.
 *
 * Operates on ``container`` (defaults to ``logContentEl``). Accepting a
 * container lets callers pre-collapse an off-DOM ``DocumentFragment``
 * before inserting it, so the final scrollHeight is correct at paint time
 * and the log can be pinned to the bottom without a later re-layout.
 */
function collapseRepeatedTools(hash, container) {
    container = container || logContentEl;
    if (!container) return;
    const tools = container.querySelectorAll('.log-tool');
    if (tools.length < 2) return;

    // Clean previous collapse state
    container.querySelectorAll('.collapse-count').forEach(b => b.remove());
    container.querySelectorAll('.collapsed-duplicate').forEach(t =>
        t.classList.remove('collapsed-duplicate'));

    let i = 0;
    while (i < tools.length) {
        const toolName = tools[i].dataset.tool;
        const groupKey = tools[i].dataset.toolKey;

        let count = 1;
        let j = i + 1;
        while (j < tools.length && tools[j].dataset.tool === toolName) {
            count++;
            j++;
        }

        if (count > 1) {
            const summary = tools[i].querySelector('.log-tool-summary');
            if (summary) {
                const badge = document.createElement('span');
                badge.className = 'collapse-count';
                badge.dataset.groupKey = groupKey;
                badge.textContent = `×${count}`;
                summary.appendChild(badge);
            }

            if (!expandedGroups.has(groupKey)) {
                for (let k = i + 1; k < j; k++) {
                    tools[k].classList.add('collapsed-duplicate');
                }
            }
        }

        i = j;
    }

    // Hash tracking only applies to the live element — fragments get a
    // fresh pass each time, which is fine because they're built and
    // collapsed once per render.
    if (hash && container === logContentEl) {
        lastCollapseHash = hash;
    }
}

// ── Super-collapse ───────────────────────────────────────────────────

/**
 * Schedule super-collapse for idle time.
 * Groups runs of many tool calls into single summary row.
 */
export function scheduleSuperCollapse() {
    if (!logContentEl) return;

    setTimeout(() => {
        const tools = logContentEl.querySelectorAll('.log-tool');
        if (tools.length < SUPER_COLLAPSE_THRESHOLD) return;

        const hash = `super:${tools.length}:${logContentEl.innerHTML.length}`;
        if (hash === lastSuperCollapseHash) return;

        scheduleIdle(() => {
            try {
                applySuperCollapse(hash);
            } catch (e) {
                console.warn('Super-collapse failed:', e);
            }
        }, { timeout: 700 });
    }, 150);
}

function applySuperCollapse(hash, container) {
    container = container || logContentEl;
    if (!container) return;

    container.querySelectorAll('.tool-supergroup').forEach(g => g.remove());
    container.querySelectorAll('.super-collapsed').forEach(t =>
        t.classList.remove('super-collapsed'));

    const cards = container.querySelectorAll('.log-card');

    for (const card of cards) {
        const cardBody = card.querySelector('.log-card-body');
        if (!cardBody) continue;

        const children = Array.from(cardBody.children);
        let runStart = -1;
        let runTools = [];

        for (let i = 0; i <= children.length; i++) {
            const child = children[i];
            const isTool = child?.classList?.contains('log-tool');

            if (isTool) {
                if (runStart === -1) runStart = i;
                runTools.push(child);
            } else {
                if (runTools.length >= SUPER_COLLAPSE_THRESHOLD) {
                    createSuperGroup(cardBody, runTools, runStart);
                }
                runStart = -1;
                runTools = [];
            }
        }
    }

    // Only persist hash when operating on the live log container.
    if (hash && container === logContentEl) {
        lastSuperCollapseHash = hash;
    }
}

function createSuperGroup(container, tools, insertIndex) {
    const firstTool = tools[0];
    const firstKey = firstTool.dataset.toolKey || firstTool.dataset.tool || 'tools';
    const groupKey = `supergroup:${firstKey}:${tools.length}`;

    const header = document.createElement('div');
    header.className = 'tool-supergroup';
    header.dataset.groupKey = groupKey;

    const isExpanded = expandedSuperGroups.has(groupKey);
    const arrow = isExpanded ? '▼' : '▶';

    header.innerHTML = `<button class="tool-supergroup-toggle">\u{1f527} ${tools.length} tool operations ${arrow}</button>`;

    container.insertBefore(header, tools[0]);

    if (!isExpanded) {
        for (const tool of tools) {
            tool.classList.add('super-collapsed');
        }
    }
}

// ── Event handlers ───────────────────────────────────────────────────

function setupCollapseHandler() {
    if (!logContentEl) return;

    logContentEl.addEventListener('click', (e) => {
        const badge = e.target.closest('.collapse-count');
        if (!badge) return;

        const groupKey = badge.dataset.groupKey;
        if (!groupKey) return;

        if (expandedGroups.has(groupKey)) {
            expandedGroups.delete(groupKey);
        } else {
            if (expandedGroups.size > 500) expandedGroups.clear();
            expandedGroups.add(groupKey);
        }

        lastCollapseHash = '';
        scheduleCollapse();
    });
}

function setupSuperCollapseHandler() {
    if (!logContentEl) return;

    logContentEl.addEventListener('click', (e) => {
        const toggle = e.target.closest('.tool-supergroup-toggle');
        if (!toggle) return;

        const header = toggle.closest('.tool-supergroup');
        if (!header) return;

        const groupKey = header.dataset.groupKey;
        if (!groupKey) return;

        if (expandedSuperGroups.has(groupKey)) {
            expandedSuperGroups.delete(groupKey);
        } else {
            if (expandedSuperGroups.size > 500) expandedSuperGroups.clear();
            expandedSuperGroups.add(groupKey);
        }

        lastSuperCollapseHash = '';
        scheduleSuperCollapse();
    });
}

// ── Public API ───────────────────────────────────────────────────────

/**
 * Bind collapse event listeners. Called once from DOMContentLoaded.
 * @param {HTMLElement} logContent - the #logContent element
 */
// Re-entry guard. Repeated initCollapse() calls are no-ops; otherwise
// re-binding listeners on every call would stack handlers.
let _collapseInitialized = false;

export function initCollapse(logContent) {
    if (_collapseInitialized) return;
    _collapseInitialized = true;
    logContentEl = logContent;
    setupCollapseHandler();
    setupSuperCollapseHandler();
}

/**
 * Apply both collapse passes synchronously against an arbitrary container
 * (typically a DocumentFragment being built off-DOM before an atomic swap).
 *
 * This is the anti-jump path: by collapsing BEFORE the fragment is inserted
 * into ``logContent``, the fragment's final scrollHeight is known at paint
 * time, so ``scrollTop = scrollHeight`` actually pins to the real bottom
 * and no subsequent idle callback can shrink the content out from under
 * the pin.
 *
 * @param {DocumentFragment|HTMLElement} container
 */
export function applyCollapseSync(container) {
    if (!container) return;
    try {
        collapseRepeatedTools(null, container);
    } catch (e) {
        console.warn('applyCollapseSync: collapse failed:', e);
    }
    try {
        const tools = container.querySelectorAll('.log-tool');
        if (tools.length >= SUPER_COLLAPSE_THRESHOLD) {
            applySuperCollapse(null, container);
        }
    } catch (e) {
        console.warn('applyCollapseSync: super-collapse failed:', e);
    }
}
