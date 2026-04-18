/**
 * Markdown parsing and plan reference detection in the log view.
 *
 * Reads: ctx.token (for plan preview fetch)
 * DOM owned: .plan-file-ref elements inside logContent
 * Timers: requestIdleCallback for deferred markdown parsing and plan detection
 */
import ctx from '../context.js';
import { escapeHtml } from '../utils.js';

// Module-local state
let logContentEl = null;
let markdownParseQueue = [];
let markdownParseScheduled = false;
let processedPlanRefs = new Set();

const scheduleIdle = window.requestIdleCallback || ((cb) => setTimeout(cb, 100));

// ── Markdown parsing ─────────────────────────────────────────────────

/**
 * Schedule markdown parsing for idle time.
 * Called from log rendering after appending entries with markdown content.
 */
export function scheduleMarkdownParse(element) {
    markdownParseQueue.push(element);
    if (!markdownParseScheduled) {
        markdownParseScheduled = true;
        if ('requestIdleCallback' in window) {
            requestIdleCallback(processMarkdownQueue, { timeout: 100 });
        } else {
            setTimeout(processMarkdownQueue, 16);
        }
    }
}

function processMarkdownQueue(deadline) {
    const timeLimit = deadline?.timeRemaining ? deadline.timeRemaining() : 8;
    const start = performance.now();

    while (markdownParseQueue.length > 0 && (performance.now() - start) < timeLimit) {
        const el = markdownParseQueue.shift();
        if (el.dataset.markdown && el.isConnected) {
            try {
                el.innerHTML = marked.parse(el.dataset.markdown);
                delete el.dataset.markdown;
            } catch (e) {
                // Keep plain text on parse error
            }
        }
    }

    if (markdownParseQueue.length > 0) {
        if ('requestIdleCallback' in window) {
            requestIdleCallback(processMarkdownQueue, { timeout: 100 });
        } else {
            setTimeout(processMarkdownQueue, 16);
        }
    } else {
        markdownParseScheduled = false;
    }
}

// ── Plan reference detection ─────────────────────────────────────────

/**
 * Schedule plan file preview detection for idle time.
 * Called from log rendering after appending entries.
 */
export function schedulePlanPreviews() {
    if (!logContentEl) return;

    scheduleIdle(() => {
        try {
            detectAndReplacePlanRefs();
        } catch (e) {
            console.warn('Plan preview detection failed:', e);
        }
    }, { timeout: 600 });
}

function detectAndReplacePlanRefs() {
    if (!logContentEl) return;

    const walker = document.createTreeWalker(
        logContentEl,
        NodeFilter.SHOW_TEXT,
        null,
        false
    );

    const planPathRegex = /(?:~|\/home\/\w+)\/\.claude\/plans\/([\w\-\.]+\.md)/g;
    const nodesToReplace = [];

    let node;
    while (node = walker.nextNode()) {
        const text = node.textContent;
        if (planPathRegex.test(text)) {
            planPathRegex.lastIndex = 0;
            nodesToReplace.push(node);
        }
    }

    for (const textNode of nodesToReplace) {
        const text = textNode.textContent;
        const fragment = document.createDocumentFragment();
        let lastIndex = 0;
        let match;

        planPathRegex.lastIndex = 0;
        while ((match = planPathRegex.exec(text)) !== null) {
            if (match.index > lastIndex) {
                fragment.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
            }

            const filename = match[1];
            const fullPath = match[0];

            if (!processedPlanRefs.has(fullPath)) {
                const planLink = document.createElement('span');
                planLink.className = 'plan-file-ref';
                planLink.dataset.filename = filename;
                planLink.innerHTML = `<span class="plan-file-icon">\u{1f4cb}</span> ${escapeHtml(filename)} <span class="plan-expand-hint">(tap to preview)</span>`;
                fragment.appendChild(planLink);
                if (processedPlanRefs.size > 500) processedPlanRefs.clear();
                processedPlanRefs.add(fullPath);
            } else {
                fragment.appendChild(document.createTextNode(fullPath));
            }

            lastIndex = match.index + match[0].length;
        }

        if (lastIndex < text.length) {
            fragment.appendChild(document.createTextNode(text.slice(lastIndex)));
        }

        textNode.parentNode.replaceChild(fragment, textNode);
    }
}

function setupPlanPreviewHandler() {
    if (!logContentEl) return;

    logContentEl.addEventListener('click', async (e) => {
        const planRef = e.target.closest('.plan-file-ref');
        if (!planRef) return;

        const filename = planRef.dataset.filename;
        if (!filename) return;

        const existingPreview = planRef.querySelector('.plan-preview');
        if (existingPreview) {
            existingPreview.remove();
            planRef.classList.remove('expanded');
            return;
        }

        planRef.classList.add('loading');
        try {
            const response = await ctx.apiFetch(`/api/plan?filename=${encodeURIComponent(filename)}&preview=true`);
            const data = await response.json();

            if (data.exists && data.content) {
                const preview = document.createElement('div');
                preview.className = 'plan-preview';
                preview.innerHTML = `<pre>${escapeHtml(data.content)}</pre>`;
                planRef.appendChild(preview);
                planRef.classList.add('expanded');
            } else {
                const preview = document.createElement('div');
                preview.className = 'plan-preview error';
                preview.textContent = 'Plan file not found';
                planRef.appendChild(preview);
            }
        } catch (err) {
            console.error('Failed to fetch plan preview:', err);
        } finally {
            planRef.classList.remove('loading');
        }
    });
}

// ── Public API ───────────────────────────────────────────────────────

/**
 * Bind markdown/plan event listeners. Called once from DOMContentLoaded.
 * @param {HTMLElement} logContent - the #logContent element
 */
// Re-entry guard. Repeated initMarkdown() calls are no-ops; otherwise
// re-binding listeners on every call would stack handlers.
let _markdownInitialized = false;

export function initMarkdown(logContent) {
    if (_markdownInitialized) return;
    _markdownInitialized = true;
    logContentEl = logContent;
    setupPlanPreviewHandler();
}
