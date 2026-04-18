/**
 * Tool output: lazy-load full tool_result content on details expand.
 *
 * Reads: ctx.token (for API auth)
 * DOM owned: .log-tool-content replacement inside logContent
 * Listens: toggle event on details[data-tool-use-id]
 */
import ctx from '../context.js';

let logContentEl = null;

// Cache fetched content keyed by tool_use_id so rerenders don't refetch
const outputCache = new Map();
const MAX_CACHED = 500;

/**
 * Initialize tool output lazy-loading.
 * @param {HTMLElement} logContent - the #logContent container
 */
export function initToolOutput(logContent) {
    logContentEl = logContent;

    // Delegated toggle listener
    logContent.addEventListener('toggle', (e) => {
        const details = e.target;
        if (!(details instanceof HTMLDetailsElement)) return;
        if (!details.open) return;

        const toolUseId = details.dataset.toolUseId;
        if (!toolUseId) return;

        // If already cached, repopulate from cache (handles rerender)
        const cached = outputCache.get(toolUseId);
        if (cached) {
            applyOutput(details, cached);
            return;
        }

        loadToolOutput(details, toolUseId);
    }, true); // Use capture phase for toggle events
}

function applyOutput(details, data) {
    const contentEl = details.querySelector('.log-tool-content');
    if (!contentEl) return;

    contentEl.textContent = data.content || '(empty)';
    contentEl.classList.remove('loading');

    if (data.is_error) {
        contentEl.classList.add('error');
    }

    if (data.truncated && !details.querySelector('.log-tool-truncated')) {
        const notice = document.createElement('div');
        notice.className = 'log-tool-truncated';
        notice.textContent = `Output truncated (${data.char_count.toLocaleString()} chars shown of larger output)`;
        details.appendChild(notice);
    }
}

async function loadToolOutput(details, toolUseId) {
    const contentEl = details.querySelector('.log-tool-content');
    if (!contentEl) return;

    // Mark as loading
    contentEl.textContent = 'Loading...';
    contentEl.classList.add('loading');
    contentEl.classList.remove('error');

    try {
        const params = new URLSearchParams({ token: ctx.token, tool_use_id: toolUseId });
        const resp = await ctx.apiFetch(`/api/log/tool-output?${params}`);

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ error: resp.statusText }));
            contentEl.textContent = err.error || 'Failed to load';
            contentEl.classList.remove('loading');
            contentEl.classList.add('error');
            return;
        }

        const data = await resp.json();

        // Cache for rerender resilience
        outputCache.set(toolUseId, data);
        if (outputCache.size > MAX_CACHED) {
            const first = outputCache.keys().next().value;
            outputCache.delete(first);
        }

        applyOutput(details, data);
    } catch (err) {
        contentEl.textContent = `Network error: ${err.message}`;
        contentEl.classList.remove('loading');
        contentEl.classList.add('error');
    }
}
