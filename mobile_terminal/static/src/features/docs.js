/**
 * Docs modal: Plans, Context, Touch, Sessions, and File browser tabs.
 *
 * Reads: ctx.token, ctx.showToast
 * DOM owned: #docsBtn, #docsModal, #docsModalClose, #docsModalTitle, #docsModalBody
 * Timers: searchDebounceTimer for file tree filter
 */
import ctx from '../context.js';
import { escapeHtml, formatTimeAgo, formatFileSize } from '../utils.js';

// Module-local state
let searchDebounceTimer = null;

// All docs-tab GETs use this so the browser HTTP cache can't hand back a
// stale plan list / context file / session log. The module-level caches
// (plansCache, sessionsCache, fileTreeCache) are already nulled on modal
// open, but without no-store the second-layer browser cache could still
// return yesterday's response. Combined: every modal open hits disk.
const NO_CACHE = { cache: 'no-store' };

// ── Public API ───────────────────────────────────────────────────────

/**
 * Setup docs button and modal handlers.
 * Called once from DOMContentLoaded.
 */
// Re-entry guard. Repeated initDocs() calls are no-ops; otherwise
// re-binding listeners on every call would stack handlers.
let _docsInitialized = false;

export function initDocs() {
    if (_docsInitialized) return;
    _docsInitialized = true;
    const docsBtn = document.getElementById('docsBtn');
    const docsModal = document.getElementById('docsModal');
    const docsModalClose = document.getElementById('docsModalClose');
    const docsModalTitle = document.getElementById('docsModalTitle');
    const docsModalBody = document.getElementById('docsModalBody');

    if (!docsBtn || !docsModal) return;

    let currentTab = 'plans';
    let plansCache = null;
    let selectedPlan = null;
    let sessionsCache = null;
    let viewingSessionId = null;

    // Tab click handlers
    const tabs = docsModal.querySelectorAll('.docs-tab');
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const tabName = tab.dataset.tab;
            switchTab(tabName);
        });
    });

    function switchTab(tabName) {
        currentTab = tabName;
        viewingSessionId = null;
        tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === tabName));
        loadTabContent(tabName);
    }

    async function loadTabContent(tabName) {
        docsModalBody.innerHTML = '<div class="docs-loading">Loading...</div>';

        switch (tabName) {
            case 'plans':
                await loadPlansTab();
                break;
            case 'context':
                await loadContextTab();
                break;
            case 'touch':
                await loadTouchTab();
                break;
            case 'sessions':
                await loadSessionsTab();
                break;
            case 'search':
                loadSearchTab();
                break;
        }
    }

    // Search/Files tab - shows file tree with search
    let fileTreeCache = null;
    let expandedDirs = new Set();

    async function loadSearchTab() {
        docsModalBody.innerHTML = '<div class="docs-loading">Loading files...</div>';

        try {
            const resp = await ctx.apiFetch(`/api/files/tree`, NO_CACHE);
            if (!resp.ok) throw new Error('Failed to load files');
            fileTreeCache = await resp.json();
        } catch (e) {
            docsModalBody.innerHTML = `<div class="docs-error">Error: ${escapeHtml(e.message)}</div>`;
            return;
        }

        renderFileTree('');
    }

    function renderFileTree(filter) {
        if (!fileTreeCache) return;

        const { files, directories, root_name } = fileTreeCache;
        const filterLower = filter.toLowerCase();

        const filteredFiles = filter
            ? files.filter(f => f.toLowerCase().includes(filterLower))
            : files;

        const tree = {};
        filteredFiles.forEach(filePath => {
            const parts = filePath.split('/');
            let current = tree;
            for (let i = 0; i < parts.length - 1; i++) {
                const dir = parts[i];
                if (!current[dir]) current[dir] = { __files: [], __dirs: {} };
                current = current[dir].__dirs;
            }
            const fileName = parts[parts.length - 1];
            if (!current.__root) current.__root = { __files: [], __dirs: {} };
            if (parts.length === 1) {
                if (!tree.__files) tree.__files = [];
                tree.__files.push(fileName);
            } else {
                let node = tree;
                for (let i = 0; i < parts.length - 1; i++) {
                    if (!node[parts[i]]) node[parts[i]] = { __files: [], __dirs: {} };
                    if (i === parts.length - 2) {
                        node[parts[i]].__files.push(fileName);
                    } else {
                        node = node[parts[i]].__dirs;
                    }
                }
            }
        });

        docsModalBody.innerHTML = `
            <div class="docs-search-container">
                <div class="search-repo-path">${escapeHtml(root_name) || 'Repository'}</div>
                <input type="text" id="docsSearchInput" class="docs-search-input"
                       placeholder="Filter files..." autocomplete="off" autocorrect="off"
                       autocapitalize="off" spellcheck="false" value="${escapeHtml(filter)}">
                <div id="fileTreeContainer" class="file-tree-container">
                    ${renderTreeNode(tree, '', 0, filter)}
                </div>
                <div class="file-count">${filteredFiles.length} files</div>
            </div>
        `;

        const searchInput = document.getElementById('docsSearchInput');
        searchInput.addEventListener('input', (e) => {
            clearTimeout(searchDebounceTimer);
            searchDebounceTimer = setTimeout(() => {
                renderFileTree(e.target.value);
                const input = document.getElementById('docsSearchInput');
                if (input) {
                    input.focus();
                    input.setSelectionRange(input.value.length, input.value.length);
                }
            }, 150);
        });

        document.querySelectorAll('.tree-folder').forEach(el => {
            el.addEventListener('click', (e) => {
                e.stopPropagation();
                const path = el.dataset.path;
                if (expandedDirs.has(path)) {
                    expandedDirs.delete(path);
                } else {
                    expandedDirs.add(path);
                }
                renderFileTree(filter);
            });
        });

        document.querySelectorAll('.tree-file').forEach(el => {
            el.addEventListener('click', () => {
                const filePath = el.dataset.path;
                openFileInModal(filePath);
            });
        });
    }

    function renderTreeNode(node, path, depth, filter) {
        let html = '';
        const indent = depth * 16;

        const dirs = Object.keys(node).filter(k => !k.startsWith('__')).sort();
        dirs.forEach(dir => {
            const dirPath = path ? `${path}/${dir}` : dir;
            const isExpanded = expandedDirs.has(dirPath) || filter.length > 0;
            const icon = isExpanded ? '&#9660;' : '&#9654;';
            const childNode = node[dir];

            html += `<div class="tree-folder" data-path="${escapeHtml(dirPath)}" style="padding-left:${indent}px">
                <span class="tree-icon">${icon}</span>
                <span class="tree-name">${escapeHtml(dir)}/</span>
            </div>`;

            if (isExpanded) {
                html += renderTreeNode(childNode.__dirs || {}, dirPath, depth + 1, filter);
                (childNode.__files || []).sort().forEach(file => {
                    html += `<div class="tree-file" data-path="${escapeHtml(dirPath)}/${escapeHtml(file)}" style="padding-left:${indent + 16}px">
                        <span class="tree-icon">&#128196;</span>
                        <span class="tree-name">${escapeHtml(file)}</span>
                    </div>`;
                });
            }
        });

        if (node.__files) {
            node.__files.sort().forEach(file => {
                const filePath = path ? `${path}/${file}` : file;
                html += `<div class="tree-file" data-path="${escapeHtml(filePath)}" style="padding-left:${indent}px">
                    <span class="tree-icon">&#128196;</span>
                    <span class="tree-name">${escapeHtml(file)}</span>
                </div>`;
            });
        }

        return html;
    }

    async function openFileInModal(filePath) {
        docsModalBody.innerHTML = '<div class="docs-loading">Loading file...</div>';
        try {
            const resp = await ctx.apiFetch(`/api/file?path=${encodeURIComponent(filePath)}`, NO_CACHE);
            if (!resp.ok) throw new Error('Failed to load file');
            const data = await resp.json();

            const ext = filePath.split('.').pop().toLowerCase();
            const isMarkdown = ['md', 'markdown'].includes(ext);

            docsModalBody.innerHTML = `
                <div class="file-viewer">
                    <div class="file-viewer-header">
                        <button class="file-back-btn" id="fileBackBtn">&larr; Back</button>
                        <span class="file-viewer-path">${escapeHtml(filePath)}</span>
                    </div>
                    <div class="file-viewer-content ${isMarkdown ? 'markdown-content' : 'code-content'}">
                        ${isMarkdown ? marked.parse(data.content || '') : `<pre>${escapeHtml(data.content || '')}</pre>`}
                    </div>
                </div>
            `;

            document.getElementById('fileBackBtn').addEventListener('click', () => {
                loadSearchTab();
            });
        } catch (e) {
            docsModalBody.innerHTML = `<div class="docs-error">Error: ${escapeHtml(e.message)}</div>`;
        }
    }

    // Plans tab with dropdown selector.
    // Always re-fetches on entry (even when the modal stays open) so the
    // dropdown reflects any plan files written between tab switches.
    async function loadPlansTab() {
        try {
            // Plan files are usually authored by Claude in the background;
            // you want the dropdown to show whatever's on disk RIGHT NOW.
            // No conditional cache — every entry hits disk.
            const response = await ctx.apiFetch(`/api/plans`, NO_CACHE);
            const data = await response.json();
            plansCache = data.plans || [];

            if (plansCache.length === 0) {
                docsModalBody.innerHTML = '<div class="docs-empty">No plan files found in ~/.claude/plans/</div>';
                return;
            }

            let html = '<div class="docs-plan-selector"><select class="docs-plan-select" id="docsPlanSelect">';
            html += '<option value="">Select a plan...</option>';
            for (const p of plansCache) {
                const title = p.title || p.filename;
                const selected = selectedPlan === p.filename ? 'selected' : '';
                html += `<option value="${escapeHtml(p.filename)}" ${selected}>${escapeHtml(title)}</option>`;
            }
            html += '</select></div>';
            html += '<div id="docsPlanContent"></div>';

            docsModalBody.innerHTML = html;

            const select = document.getElementById('docsPlanSelect');
            select.addEventListener('change', async () => {
                selectedPlan = select.value;
                if (selectedPlan) {
                    await loadPlanContent(selectedPlan);
                } else {
                    document.getElementById('docsPlanContent').innerHTML = '';
                }
            });

            if (selectedPlan) {
                await loadPlanContent(selectedPlan);
            }
        } catch (e) {
            console.error('Failed to load plans:', e);
            docsModalBody.innerHTML = '<div class="docs-empty" style="color: var(--danger);">Error loading plans</div>';
        }
    }

    let lastPlanRawContent = '';

    async function loadPlanContent(filename) {
        const contentDiv = document.getElementById('docsPlanContent');
        if (!contentDiv) return;
        contentDiv.innerHTML = '<div class="docs-loading">Loading plan...</div>';
        lastPlanRawContent = '';

        try {
            const response = await ctx.apiFetch(`/api/plan?filename=${encodeURIComponent(filename)}&preview=false`, NO_CACHE);
            const data = await response.json();

            if (data.exists && data.content) {
                lastPlanRawContent = data.content;
                const copyBtn = '<div class="docs-plan-actions">'
                    + '<button class="docs-copy-btn" id="docsPlanCopyBtn">Copy</button>'
                    + '<button class="docs-copy-btn docs-challenge-btn" id="docsPlanChallengeBtn">Challenge</button>'
                    + '</div>';
                let rendered;
                try {
                    rendered = marked.parse(data.content);
                } catch (e) {
                    rendered = `<pre>${escapeHtml(data.content)}</pre>`;
                }
                contentDiv.innerHTML = copyBtn + rendered;
                document.getElementById('docsPlanCopyBtn').addEventListener('click', copyPlanContent);
                const challBtn = document.getElementById('docsPlanChallengeBtn');
                if (challBtn) challBtn.addEventListener('click', () => {
                    const cb = document.getElementById('challengeBtn');
                    if (cb) cb.click();
                });
            } else {
                contentDiv.innerHTML = '<div class="docs-empty">Plan file not found</div>';
            }
        } catch (e) {
            console.error('Failed to load plan content:', e);
            contentDiv.innerHTML = '<div class="docs-empty" style="color: var(--danger);">Error loading plan</div>';
        }
    }

    async function copyPlanContent() {
        if (!lastPlanRawContent) return;
        const btn = document.getElementById('docsPlanCopyBtn');
        try {
            await navigator.clipboard.writeText(lastPlanRawContent);
            if (btn) { btn.textContent = 'Copied'; setTimeout(() => { btn.textContent = 'Copy'; }, 1500); }
        } catch (e) {
            ctx.showToast('Copy failed', 'error');
        }
    }

    // Context tab
    async function loadContextTab() {
        try {
            const response = await ctx.apiFetch(`/api/docs/context`, NO_CACHE);
            const data = await response.json();

            if (data.exists && data.content) {
                try {
                    docsModalBody.innerHTML = marked.parse(data.content);
                } catch (e) {
                    docsModalBody.innerHTML = `<pre>${escapeHtml(data.content)}</pre>`;
                }
            } else {
                docsModalBody.innerHTML = '<div class="docs-empty">No .claude/CONTEXT.md found</div>';
            }
        } catch (e) {
            console.error('Failed to load context:', e);
            docsModalBody.innerHTML = '<div class="docs-empty" style="color: var(--danger);">Error loading context</div>';
        }
    }

    // Touch summary tab
    async function loadTouchTab() {
        try {
            const response = await ctx.apiFetch(`/api/docs/touch`, NO_CACHE);
            const data = await response.json();

            if (data.exists && data.content) {
                try {
                    docsModalBody.innerHTML = marked.parse(data.content);
                } catch (e) {
                    docsModalBody.innerHTML = `<pre>${escapeHtml(data.content)}</pre>`;
                }
            } else {
                docsModalBody.innerHTML = '<div class="docs-empty">No .claude/touch-summary.md found</div>';
            }
        } catch (e) {
            console.error('Failed to load touch summary:', e);
            docsModalBody.innerHTML = '<div class="docs-empty" style="color: var(--danger);">Error loading touch summary</div>';
        }
    }

    // Sessions tab
    async function loadSessionsTab() {
        if (viewingSessionId) {
            await loadSessionContent(viewingSessionId);
            return;
        }

        try {
            const response = await ctx.apiFetch(`/api/log/sessions`, NO_CACHE);
            const data = await response.json();
            sessionsCache = data.sessions || [];

            if (sessionsCache.length === 0) {
                docsModalBody.innerHTML = '<div class="docs-empty">No session logs found</div>';
                return;
            }

            let html = '<div class="docs-session-list">';
            for (const s of sessionsCache) {
                const isCurrent = s.is_current;
                const shortId = s.id.substring(0, 8) + '...';
                const preview = s.preview || '(empty)';
                const modifiedTs = s.modified ? new Date(s.modified).getTime() : 0;
                const modified = modifiedTs ? formatTimeAgo(modifiedTs) : '';
                const size = s.size ? formatFileSize(s.size) : '';

                // Build action buttons
                let buttons = '';
                if (s.is_pinned) {
                    buttons = `<button class="docs-session-unpin-btn" data-session="${escapeHtml(s.id)}">Unpin</button>`;
                } else {
                    buttons = `<div class="docs-session-buttons">`;
                    if (!isCurrent) {
                        buttons += `<button class="docs-session-view-btn" data-session="${escapeHtml(s.id)}">View</button>`;
                    }
                    buttons += `<button class="docs-session-pin-btn" data-session="${escapeHtml(s.id)}">Pin</button>`;
                    buttons += `</div>`;
                }

                html += `
                    <div class="docs-session-item ${isCurrent ? 'current' : ''} ${s.is_pinned ? 'pinned' : ''}">
                        <div class="docs-session-indicator"></div>
                        <div class="docs-session-info">
                            <div class="docs-session-id">${escapeHtml(shortId)}${isCurrent ? ' (current)' : ''}${s.is_pinned ? ' (pinned)' : ''}</div>
                            <div class="docs-session-preview">"${escapeHtml(preview)}"</div>
                            <div class="docs-session-meta">${modified}${size ? ' · ' + size : ''}</div>
                        </div>
                        ${buttons}
                    </div>
                `;
            }
            html += '</div>';

            docsModalBody.innerHTML = html;

            docsModalBody.querySelectorAll('.docs-session-view-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    viewingSessionId = btn.dataset.session;
                    loadSessionContent(viewingSessionId);
                });
            });

            docsModalBody.querySelectorAll('.docs-session-pin-btn').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const sid = btn.dataset.session;
                    btn.disabled = true;
                    btn.textContent = '...';
                    try {
                        const resp = await ctx.apiFetch(`/api/log/select?session_id=${encodeURIComponent(sid)}`, { method: 'POST' });
                        if (resp.ok) {
                            sessionsCache = null;
                            loadSessionsTab();
                        } else {
                            const err = await resp.json();
                            ctx.showToast?.(err.error || 'Pin failed', 'error');
                            btn.disabled = false;
                            btn.textContent = 'Pin';
                        }
                    } catch (e) {
                        console.error('Pin failed:', e);
                        btn.disabled = false;
                        btn.textContent = 'Pin';
                    }
                });
            });

            docsModalBody.querySelectorAll('.docs-session-unpin-btn').forEach(btn => {
                btn.addEventListener('click', async () => {
                    btn.disabled = true;
                    btn.textContent = '...';
                    try {
                        await ctx.apiFetch(`/api/log/unpin`, { method: 'POST' });
                        sessionsCache = null;
                        loadSessionsTab();
                    } catch (e) {
                        console.error('Unpin failed:', e);
                        btn.disabled = false;
                        btn.textContent = 'Unpin';
                    }
                });
            });
        } catch (e) {
            console.error('Failed to load sessions:', e);
            docsModalBody.innerHTML = '<div class="docs-empty" style="color: var(--danger);">Error loading sessions</div>';
        }
    }

    async function loadSessionContent(sessionId) {
        docsModalBody.innerHTML = '<div class="docs-loading">Loading session...</div>';

        try {
            const response = await ctx.apiFetch(`/api/log?session_id=${encodeURIComponent(sessionId)}`, NO_CACHE);
            const data = await response.json();

            const shortId = sessionId.substring(0, 8) + '...';
            let html = `<button class="docs-back-btn" id="docsSessionBack">\u2190 Back to sessions</button>`;
            html += `<div style="margin-bottom: 8px; color: var(--text-muted); font-size: 12px;">Session: ${escapeHtml(shortId)}</div>`;

            if (data.exists && data.content) {
                html += `<pre style="white-space: pre-wrap; font-size: 12px; line-height: 1.5;">${escapeHtml(data.content)}</pre>`;
            } else {
                html += '<div class="docs-empty">Session log is empty or not found</div>';
            }

            docsModalBody.innerHTML = html;

            document.getElementById('docsSessionBack')?.addEventListener('click', () => {
                viewingSessionId = null;
                loadSessionsTab();
            });
        } catch (e) {
            console.error('Failed to load session content:', e);
            docsModalBody.innerHTML = '<div class="docs-empty" style="color: var(--danger);">Error loading session</div>';
        }
    }

    // Open modal
    docsBtn.addEventListener('click', () => {
        docsModal.classList.remove('hidden');
        plansCache = null;
        sessionsCache = null;
        viewingSessionId = null;
        switchTab(currentTab);
    });

    // Close modal
    if (docsModalClose) {
        docsModalClose.addEventListener('click', () => {
            docsModal.classList.add('hidden');
        });
    }

    // Close on backdrop click
    docsModal.addEventListener('click', (e) => {
        if (e.target === docsModal) {
            docsModal.classList.add('hidden');
        }
    });
}
