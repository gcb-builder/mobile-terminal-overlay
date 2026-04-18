/**
 * History tab: unified commits + snapshots, git revert, dirty-state handling.
 *
 * Reads: ctx.token, ctx.currentSession, ctx.activeTarget
 * DOM owned: #historyList, #historyCommitDetail, #gitStatusBanner,
 *            #dirtyChoiceModal, #discardConfirmModal, #stashResultModal
 * Timers: none
 */
import ctx from '../context.js';
import { escapeHtml, formatTimeAgo } from '../utils.js';

// Module-local state
let historyItems = [];
let historyFilter = 'all';  // 'all', 'commit', 'snapshot'
let selectedHistoryCommit = null;
let lastKnownCommitHash = null;  // Track for auto-clearing snapshots on new commit
let historyDryRunValidatedHash = null;  // Commit hash that passed dry-run

let gitStatus = null;  // Current git status (branch, dirty, ahead/behind)
let selectedCommitHash = null;
let dryRunValidatedHash = null;  // Commit hash that passed dry-run (safer revert UX)

let pendingDirtyAction = null;  // 'dry-run' or 'revert'
let lastStashRef = null;  // Track stash created during revert flow

// Callbacks set during init
let captureSnapshotCb = null;
let enterPreviewModeCb = null;

// ── Helpers ──────────────────────────────────────────────────────────

function getTargetParams() {
    const params = new URLSearchParams();
    if (ctx.currentSession) params.append('session', ctx.currentSession);
    if (ctx.activeTarget) params.append('pane_id', ctx.activeTarget);
    return params.toString();
}

function showToast(msg, type, duration) {
    if (ctx.showToast) ctx.showToast(msg, type, duration);
}

// ── Core functions ───────────────────────────────────────────────────

/**
 * Clear all snapshots
 */
async function clearSnapshots() {
    try {
        await ctx.apiFetch(`/api/rollback/preview/clear`, { method: 'POST' });
    } catch (e) {
        console.error('Failed to clear snapshots:', e);
    }
}

/**
 * Load unified history (commits + snapshots)
 */
export async function loadHistory() {
    const list = document.getElementById('historyList');
    if (!list) return;

    list.innerHTML = '<div class="history-empty">Loading...</div>';

    try {
        const resp = await ctx.apiFetch(`/api/history?limit=40`);
        if (!resp.ok) throw new Error('Failed to load history');

        const data = await resp.json();
        historyItems = data.items || [];

        // Check for new commits and auto-clear snapshots
        const latestCommit = historyItems.find(i => i.type === 'commit');
        if (latestCommit) {
            if (lastKnownCommitHash && lastKnownCommitHash !== latestCommit.hash) {
                console.log('New commit detected, clearing snapshots');
                await clearSnapshots();
                showToast('Snapshots cleared (new commit)', 'info', 2000);
                // Reload to get updated list
                const resp2 = await ctx.apiFetch(`/api/history?limit=40`);
                const data2 = await resp2.json();
                historyItems = data2.items || [];
            }
            lastKnownCommitHash = latestCommit.hash;
        }

        renderHistoryList();
    } catch (e) {
        console.error('Failed to load history:', e);
        list.innerHTML = '<div class="history-empty">Failed to load history</div>';
    }
}

/**
 * Render history list with timeline visual and enhanced snapshot UI
 */
function renderHistoryList() {
    const list = document.getElementById('historyList');
    if (!list) return;

    // Filter items
    let items = historyItems;
    if (historyFilter === 'commit') {
        items = historyItems.filter(i => i.type === 'commit');
    } else if (historyFilter === 'snapshot') {
        items = historyItems.filter(i => i.type === 'snapshot');
    }

    if (items.length === 0) {
        list.innerHTML = '<div class="history-empty">No items</div>';
        return;
    }

    // Label badge colors
    const labelColors = {
        bash: '#22c55e', edit: '#f59e0b', tool_call: '#3b82f6',
        plan_transition: '#a855f7', task: '#ec4899',
        user_send: '#3b82f6', cmd: '#3b82f6',
        agent_done: '#6b7280', error: '#ef4444',
    };

    list.innerHTML = '<div class="history-timeline">' + items.map((item, idx) => {
        const timeAgo = formatTimeAgo(item.timestamp);
        const isLast = idx === items.length - 1;
        const lineClass = isLast ? 'tl-line tl-line-last' : 'tl-line';

        if (item.type === 'commit') {
            return `
                <div class="history-item history-commit tl-item" data-hash="${escapeHtml(item.hash)}">
                    <div class="tl-gutter">
                        <span class="tl-dot tl-dot-commit"></span>
                        <span class="${lineClass}"></span>
                    </div>
                    <div class="tl-content">
                        <div class="tl-row">
                            <span class="history-id">${escapeHtml(item.id)}</span>
                            <span class="history-subject">${escapeHtml(item.subject)}</span>
                        </div>
                        <span class="history-time">${timeAgo}</span>
                    </div>
                    <button class="history-action-btn" data-action="revert" data-hash="${escapeHtml(item.hash)}" title="Revert">↩</button>
                </div>`;
        } else {
            const rawLabel = item.label || 'snapshot';
            const labelDisplay = rawLabel === 'user_send' ? 'cmd' : rawLabel;
            const badgeColor = labelColors[rawLabel] || labelColors[labelDisplay] || '#6b7280';
            const notePreview = item.note ? `<span class="tl-note">${escapeHtml(item.note.substring(0, 50))}</span>` : '';
            const imgIndicator = item.image_path ? '<span class="tl-indicator">IMG</span>' : '';
            const pinIndicator = item.pinned ? '<span class="tl-indicator tl-pin">PIN</span>' : '';
            const gitHead = item.git_head ? `<span class="tl-git">${escapeHtml(item.git_head)}</span>` : '';

            return `
                <div class="history-item history-snapshot tl-item" data-id="${escapeHtml(String(item.id))}">
                    <div class="tl-gutter">
                        <span class="tl-dot" style="background:${badgeColor}"></span>
                        <span class="${lineClass}"></span>
                    </div>
                    <div class="tl-content">
                        <div class="tl-row">
                            <span class="tl-badge" style="background:${badgeColor}">${escapeHtml(labelDisplay)}</span>
                            ${gitHead}${pinIndicator}${imgIndicator}
                        </div>
                        ${notePreview}
                    </div>
                    <span class="history-time">${timeAgo}</span>
                    <div class="tl-actions">
                        <button class="history-action-btn" data-action="note" data-id="${escapeHtml(String(item.id))}" title="Note">N</button>
                        <button class="history-action-btn" data-action="preview" data-id="${escapeHtml(String(item.id))}" title="View">V</button>
                    </div>
                </div>`;
        }
    }).join('') + '</div>';

    // Attach note button handlers
    list.querySelectorAll('[data-action="note"]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const snapId = btn.dataset.id;
            const existing = historyItems.find(i => i.id === snapId)?.note || '';
            const note = prompt('Add note (max 500 chars):', existing);
            if (note !== null) {
                ctx.apiFetch(`/api/rollback/preview/${snapId}/annotate`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ note: note.substring(0, 500) }),
                }).then(r => {
                    if (r.ok) {
                        const item = historyItems.find(i => i.id === snapId);
                        if (item) item.note = note.substring(0, 500);
                        renderHistoryList();
                        showToast('Note saved', 'success');
                    }
                }).catch(() => showToast('Failed to save note', 'error'));
            }
        });
    });
}

/**
 * Show commit detail in history tab
 */
function showHistoryCommitDetail(hash) {
    const list = document.getElementById('historyList');
    const detail = document.getElementById('historyCommitDetail');
    const hashEl = document.getElementById('historyDetailHash');
    const content = document.getElementById('historyDetailContent');
    const dryRunResult = document.getElementById('historyDryRunResult');

    if (!list || !detail) return;

    selectedHistoryCommit = hash;
    historyDryRunValidatedHash = null;

    // Refresh git status for accurate dirty check
    loadGitStatus();

    list.classList.add('hidden');
    detail.classList.remove('hidden');
    hashEl.textContent = hash.slice(0, 7);
    content.innerHTML = '<div class="loading">Loading...</div>';
    dryRunResult?.classList.add('hidden');

    // Disable revert until dry-run passes
    const revertBtn = document.getElementById('historyRevertBtn');
    if (revertBtn) {
        revertBtn.disabled = true;
        revertBtn.title = 'Run dry-run first';
    }

    ctx.apiFetch(`/api/rollback/git/commit/${hash}`)
        .then(resp => {
            if (!resp.ok) throw new Error('Failed to load commit');
            return resp.json();
        })
        .then(data => {
            content.innerHTML = `
                <div class="commit-subject">${escapeHtml(data.subject)}</div>
                <div class="commit-meta">${escapeHtml(data.author)} · ${escapeHtml(data.date)}</div>
                ${data.body ? `<pre class="commit-body">${escapeHtml(data.body)}</pre>` : ''}
                <pre class="commit-stat">${escapeHtml(data.stat)}</pre>
            `;
        })
        .catch(e => {
            content.innerHTML = `<div class="error">Failed to load: ${escapeHtml(e.message)}</div>`;
        });
}

/**
 * Hide commit detail, show list
 */
function hideHistoryCommitDetail() {
    const list = document.getElementById('historyList');
    const detail = document.getElementById('historyCommitDetail');
    if (list) list.classList.remove('hidden');
    if (detail) detail.classList.add('hidden');
    selectedHistoryCommit = null;
}

/**
 * Dry-run revert for history commit
 */
async function historyDryRunRevert() {
    if (!selectedHistoryCommit) return;

    // Check for dirty state and show choice modal if needed
    if (gitStatus?.is_dirty) {
        showDirtyChoiceModal('dry-run');
        return;
    }

    const dryRunResult = document.getElementById('historyDryRunResult');
    const revertBtn = document.getElementById('historyRevertBtn');
    const dryRunBtn = document.getElementById('historyDryRunBtn');

    if (dryRunBtn) {
        dryRunBtn.disabled = true;
        dryRunBtn.textContent = 'Running...';
    }
    if (dryRunResult) {
        dryRunResult.classList.remove('hidden');
        dryRunResult.innerHTML = '<div class="loading">Running dry-run...</div>';
    }

    try {
        const resp = await ctx.apiFetch(`/api/rollback/git/revert/dry-run?commit_hash=${selectedHistoryCommit}`, {
            method: 'POST'
        });
        if (!resp.ok) {
            let errMsg = `Server error (${resp.status})`;
            try { const d = await resp.json(); errMsg = d.error || errMsg; } catch {}
            dryRunResult.innerHTML = `<div class="dry-run-error">\u2717 ${escapeHtml(errMsg)}</div>`;
            return;
        }
        const data = await resp.json();

        if (data.success) {
            dryRunResult.innerHTML = `<div class="dry-run-success">\u2713 Dry-run passed. Safe to revert.</div>`;
            historyDryRunValidatedHash = selectedHistoryCommit;
            if (revertBtn) {
                revertBtn.disabled = false;
                revertBtn.title = 'Revert this commit';
            }
        } else {
            dryRunResult.innerHTML = `<div class="dry-run-error">\u2717 Dry-run failed: ${escapeHtml(data.error || 'Unknown error')}</div>`;
        }
    } catch (e) {
        dryRunResult.innerHTML = `<div class="dry-run-error">\u2717 Error: ${escapeHtml(e.message)}</div>`;
    } finally {
        if (dryRunBtn) {
            dryRunBtn.disabled = false;
            dryRunBtn.textContent = 'Dry Run';
        }
    }
}

/**
 * Execute revert for history commit
 */
async function historyExecuteRevert() {
    if (!selectedHistoryCommit) return;
    if (historyDryRunValidatedHash !== selectedHistoryCommit) {
        showToast('Run dry-run first', 'error');
        return;
    }

    // Check for dirty state and show choice modal if needed
    if (gitStatus?.is_dirty) {
        showDirtyChoiceModal('revert');
        return;
    }

    if (!confirm(`Revert commit ${selectedHistoryCommit.slice(0, 7)}?`)) return;

    const revertBtn = document.getElementById('historyRevertBtn');
    if (revertBtn) {
        revertBtn.disabled = true;
        revertBtn.textContent = 'Reverting...';
    }

    try {
        const resp = await ctx.apiFetch(`/api/rollback/git/revert/execute?commit_hash=${selectedHistoryCommit}&${getTargetParams()}`, {
            method: 'POST'
        });
        if (!resp.ok) {
            let errMsg = `Server error (${resp.status})`;
            try { const d = await resp.json(); errMsg = d.error || errMsg; } catch {}
            showToast(errMsg, 'error');
            return;
        }
        const data = await resp.json();

        if (data.success) {
            showToast('Commit reverted', 'success');
            hideHistoryCommitDetail();
            loadHistory();
        } else {
            showToast(`Revert failed: ${data.error}`, 'error');
        }
    } catch (e) {
        showToast(`Revert error: ${e.message}`, 'error');
    } finally {
        if (revertBtn) {
            revertBtn.disabled = false;
            revertBtn.textContent = 'Revert';
        }
    }
}

// ── Git status ───────────────────────────────────────────────────────

/**
 * Load git status (branch, dirty, ahead/behind)
 * Always fetches and updates gitStatus variable, even if DOM elements don't exist
 */
export async function loadGitStatus() {
    try {
        const resp = await ctx.apiFetch(`/api/rollback/git/status`);
        gitStatus = await resp.json();

        // Update DOM if elements exist
        const banner = document.getElementById('gitStatusBanner');
        const statusText = document.getElementById('gitStatusText');

        if (banner && statusText) {
            if (!gitStatus.has_repo) {
                banner.className = 'git-status-banner no-repo';
                statusText.innerHTML = 'No git repository found';
            } else {
                // Build status text
                let html = `<span class="git-status-branch">${escapeHtml(gitStatus.branch)}</span>`;

                if (gitStatus.is_dirty) {
                    html += ` <span class="git-status-dirty">(${gitStatus.dirty_files} uncommitted)</span>`;
                    banner.className = 'git-status-banner dirty';
                } else {
                    banner.className = 'git-status-banner clean';
                }

                if (gitStatus.has_upstream) {
                    const parts = [];
                    if (gitStatus.ahead > 0) parts.push(`\u2191${gitStatus.ahead}`);
                    if (gitStatus.behind > 0) parts.push(`\u2193${gitStatus.behind}`);
                    if (parts.length > 0) {
                        html += ` <span class="git-status-ahead-behind">${parts.join(' ')}</span>`;
                    }
                }

                // Show PR info if available
                if (gitStatus.pr) {
                    const prState = gitStatus.pr.state === 'OPEN' ? 'open' : 'closed';
                    html += ` <a href="${escapeHtml(gitStatus.pr.url)}" target="_blank" class="git-status-pr ${prState}" title="${escapeHtml(gitStatus.pr.title)}">PR #${gitStatus.pr.number}</a>`;
                }

                statusText.innerHTML = html;
            }
        }

        // Always update button state
        updateRevertButtonState();

    } catch (e) {
        console.error('Failed to load git status:', e);
        const banner = document.getElementById('gitStatusBanner');
        const statusText = document.getElementById('gitStatusText');
        if (banner && statusText) {
            banner.className = 'git-status-banner';
            statusText.textContent = 'Error loading status';
        }
    }
}

/**
 * Update revert button enabled state based on git status and dry-run validation
 * Note: Buttons are now always enabled - dirty state is handled via choice modal
 */
function updateRevertButtonState() {
    const revertBtn = document.getElementById('gitRevertBtn');
    const dryRunBtn = document.getElementById('gitDryRunBtn');

    if (revertBtn) {
        const hasDryRun = dryRunValidatedHash === selectedCommitHash;
        // Revert requires dry-run passed (dirty state handled by modal)
        if (!hasDryRun) {
            revertBtn.disabled = true;
            revertBtn.title = 'Run dry-run first to preview changes';
        } else {
            revertBtn.disabled = false;
            revertBtn.title = gitStatus?.is_dirty ? 'Will prompt to handle uncommitted changes' : '';
        }
    }

    // Dry-run button always enabled (dirty state handled by modal)
    if (dryRunBtn) {
        dryRunBtn.disabled = false;
        dryRunBtn.title = gitStatus?.is_dirty ? 'Will prompt to handle uncommitted changes' : '';
    }
}

// ── Dirty state modals ───────────────────────────────────────────────

/**
 * Show the dirty choice modal
 */
function showDirtyChoiceModal(action) {
    pendingDirtyAction = action;
    const modal = document.getElementById('dirtyChoiceModal');
    const filesInfo = document.getElementById('dirtyChoiceFiles');

    if (gitStatus) {
        const modified = gitStatus.dirty_files || 0;
        const untracked = gitStatus.untracked_files || 0;
        let info = [];
        if (modified > 0) info.push(`${modified} modified`);
        if (untracked > 0) info.push(`${untracked} untracked`);
        filesInfo.textContent = info.join(', ') || 'Uncommitted changes detected';
    }

    modal.classList.remove('hidden');
}

/**
 * Hide the dirty choice modal
 * @param {boolean} clearAction - Whether to clear pendingDirtyAction (default true)
 */
function hideDirtyChoiceModal(clearAction = true) {
    document.getElementById('dirtyChoiceModal').classList.add('hidden');
    if (clearAction) {
        pendingDirtyAction = null;
    }
}

/**
 * Handle stash choice - stash changes and continue with pending action
 */
async function handleStashChoice() {
    const action = pendingDirtyAction;  // Save before hiding clears it
    hideDirtyChoiceModal();
    showToast('Stashing changes...', 'info');

    try {
        const resp = await ctx.apiFetch(`/api/git/stash/push`, { method: 'POST' });
        const data = await resp.json();

        if (!resp.ok || data.error) {
            showToast(`Stash failed: ${data.error || 'Unknown error'}`, 'error');
            return;
        }

        lastStashRef = data.stash_ref;
        showToast('Changes stashed', 'success');

        // Reload git status and continue with pending action
        await loadGitStatus();

        if (action === 'dry-run') {
            await historyDryRunRevert();
        } else if (action === 'revert') {
            await historyExecuteRevertWithStash();
        }
    } catch (e) {
        showToast(`Stash error: ${e.message}`, 'error');
    }
}

/**
 * Show discard confirmation modal
 */
function showDiscardConfirmModal() {
    hideDirtyChoiceModal(false);  // Don't clear pendingDirtyAction, we need it later
    const modal = document.getElementById('discardConfirmModal');
    const fileList = document.getElementById('discardFileList');
    const untrackedLabel = document.getElementById('discardUntrackedLabel');
    const untrackedCheckbox = document.getElementById('discardUntrackedCheckbox');

    // Build file list
    const modified = gitStatus?.dirty_files || 0;
    const untracked = gitStatus?.untracked_files || 0;

    fileList.innerHTML = '';
    if (modified > 0) {
        const li = document.createElement('li');
        li.textContent = `${modified} modified file${modified > 1 ? 's' : ''}`;
        fileList.appendChild(li);
    }

    // Handle untracked files checkbox
    if (untracked > 0) {
        untrackedLabel.textContent = `Also remove ${untracked} untracked file${untracked > 1 ? 's' : ''}`;
        untrackedCheckbox.parentElement.style.display = 'flex';
        untrackedCheckbox.checked = false;
    } else {
        untrackedCheckbox.parentElement.style.display = 'none';
    }

    modal.classList.remove('hidden');
}

/**
 * Hide discard confirmation modal
 * @param {boolean} clearAction - Whether to clear pendingDirtyAction (default true for cancel)
 */
function hideDiscardConfirmModal(clearAction = true) {
    document.getElementById('discardConfirmModal').classList.add('hidden');
    if (clearAction) {
        pendingDirtyAction = null;
    }
}

/**
 * Handle discard confirmation - discard changes and continue
 */
async function handleDiscardConfirm() {
    const action = pendingDirtyAction;
    const includeUntracked = document.getElementById('discardUntrackedCheckbox').checked;
    hideDiscardConfirmModal(false);  // Don't clear action, we're continuing
    pendingDirtyAction = null;  // Clear now that we've saved it
    showToast('Discarding changes...', 'info');

    try {
        const resp = await ctx.apiFetch(
            `/api/git/discard?include_untracked=${includeUntracked}&${getTargetParams()}`,
            { method: 'POST' }
        );
        const data = await resp.json();

        if (!resp.ok || data.error) {
            showToast(`Discard failed: ${data.error || 'Unknown error'}`, 'error');
            return;
        }

        showToast('Changes discarded', 'success');

        // Reload git status and continue with pending action
        await loadGitStatus();

        if (action === 'dry-run') {
            await historyDryRunRevert();
        } else if (action === 'revert') {
            await historyExecuteRevert();
        }
    } catch (e) {
        showToast(`Discard error: ${e.message}`, 'error');
    }
}

/**
 * Execute revert with stash - show stash management after success
 */
async function historyExecuteRevertWithStash() {
    if (!selectedHistoryCommit) return;
    if (historyDryRunValidatedHash !== selectedHistoryCommit) {
        showToast('Run dry-run first', 'error');
        return;
    }

    if (!confirm(`Revert commit ${selectedHistoryCommit.slice(0, 7)}?`)) return;

    const revertBtn = document.getElementById('historyRevertBtn');
    if (revertBtn) {
        revertBtn.disabled = true;
        revertBtn.textContent = 'Reverting...';
    }

    try {
        const resp = await ctx.apiFetch(`/api/rollback/git/revert/execute?commit_hash=${selectedHistoryCommit}&${getTargetParams()}`, {
            method: 'POST'
        });
        if (!resp.ok) {
            let errMsg = `Server error (${resp.status})`;
            try { const d = await resp.json(); errMsg = d.error || errMsg; } catch {}
            showToast(errMsg, 'error');
            return;
        }
        const data = await resp.json();

        if (data.success) {
            hideHistoryCommitDetail();
            loadHistory();
            // Show stash result modal instead of simple toast
            showStashResultModal();
        } else {
            showToast(`Revert failed: ${data.error}`, 'error');
        }
    } catch (e) {
        showToast(`Revert error: ${e.message}`, 'error');
    } finally {
        if (revertBtn) {
            revertBtn.disabled = false;
            revertBtn.textContent = 'Revert';
        }
    }
}

/**
 * Show stash result modal after successful revert with stash
 */
function showStashResultModal() {
    const modal = document.getElementById('stashResultModal');
    const refSpan = document.getElementById('stashResultRef');
    refSpan.textContent = lastStashRef || 'stash@{0}';
    modal.classList.remove('hidden');
}

/**
 * Hide stash result modal
 */
function hideStashResultModal() {
    document.getElementById('stashResultModal').classList.add('hidden');
}

/**
 * Apply the stash that was created during revert
 */
async function applyStash() {
    const ref = lastStashRef || 'stash@{0}';
    showToast('Applying stash...', 'info');

    try {
        const resp = await ctx.apiFetch(`/api/git/stash/apply?ref=${encodeURIComponent(ref)}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (data.conflict) {
            showToast('Stash applied with conflicts - resolve manually', 'error');
            hideStashResultModal();
            loadGitStatus();
            return;
        }

        if (!resp.ok || !data.success) {
            showToast(`Apply failed: ${data.error || 'Unknown error'}`, 'error');
            return;
        }

        showToast('Stash applied successfully', 'success');
        hideStashResultModal();
        loadGitStatus();
    } catch (e) {
        showToast(`Apply error: ${e.message}`, 'error');
    }
}

/**
 * Drop the stash that was created during revert
 */
async function dropStash() {
    const ref = lastStashRef || 'stash@{0}';

    try {
        const resp = await ctx.apiFetch(`/api/git/stash/drop?ref=${encodeURIComponent(ref)}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (!resp.ok || !data.success) {
            showToast(`Drop failed: ${data.error || 'Unknown error'}`, 'error');
            return;
        }

        showToast('Stash dropped', 'success');
        hideStashResultModal();
        lastStashRef = null;
    } catch (e) {
        showToast(`Drop error: ${e.message}`, 'error');
    }
}

// ── Event setup ──────────────────────────────────────────────────────

/**
 * Setup dirty choice modal event listeners
 */
function setupDirtyChoiceModals() {
    // Dirty choice modal
    document.getElementById('dirtyChoiceStash')?.addEventListener('click', handleStashChoice);
    document.getElementById('dirtyChoiceDiscard')?.addEventListener('click', showDiscardConfirmModal);
    document.getElementById('dirtyChoiceCancel')?.addEventListener('click', hideDirtyChoiceModal);

    // Click outside to close
    document.getElementById('dirtyChoiceModal')?.addEventListener('click', (e) => {
        if (e.target.id === 'dirtyChoiceModal') hideDirtyChoiceModal();
    });

    // Discard confirmation modal
    document.getElementById('discardConfirmCancel')?.addEventListener('click', hideDiscardConfirmModal);
    document.getElementById('discardConfirmYes')?.addEventListener('click', handleDiscardConfirm);
    document.getElementById('discardConfirmModal')?.addEventListener('click', (e) => {
        if (e.target.id === 'discardConfirmModal') hideDiscardConfirmModal();
    });

    // Stash result modal
    document.getElementById('stashResultApply')?.addEventListener('click', applyStash);
    document.getElementById('stashResultDrop')?.addEventListener('click', dropStash);
    document.getElementById('stashResultClose')?.addEventListener('click', hideStashResultModal);
    document.getElementById('stashResultModal')?.addEventListener('click', (e) => {
        if (e.target.id === 'stashResultModal') hideStashResultModal();
    });
}

function setupHistoryTabHandlers() {
    // History tab button handlers
    document.getElementById('historyBackBtn')?.addEventListener('click', hideHistoryCommitDetail);
    document.getElementById('historyDryRunBtn')?.addEventListener('click', historyDryRunRevert);
    document.getElementById('historyRevertBtn')?.addEventListener('click', historyExecuteRevert);
    document.getElementById('historySnapBtn')?.addEventListener('click', async () => {
        const btn = document.getElementById('historySnapBtn');
        if (btn) btn.disabled = true;
        if (captureSnapshotCb) await captureSnapshotCb('manual');
        setTimeout(() => {
            loadHistory();
            if (btn) btn.disabled = false;
        }, 500);
    });

    // History filter buttons
    document.querySelectorAll('.history-filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.history-filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            historyFilter = btn.dataset.filter;
            renderHistoryList();
        });
    });

    // History list click handler (commits and snapshots)
    document.getElementById('historyList')?.addEventListener('click', async (e) => {
        // Handle action buttons
        const actionBtn = e.target.closest('.history-action-btn');
        if (actionBtn) {
            const action = actionBtn.dataset.action;
            if (action === 'revert') {
                const hash = actionBtn.dataset.hash;
                if (hash) showHistoryCommitDetail(hash);
            } else if (action === 'preview') {
                const id = actionBtn.dataset.id;
                if (id && enterPreviewModeCb) enterPreviewModeCb(id);
            }
            return;
        }

        // Handle item clicks
        const commitItem = e.target.closest('.history-commit');
        if (commitItem) {
            const hash = commitItem.dataset.hash;
            if (hash) showHistoryCommitDetail(hash);
            return;
        }

        const snapshotItem = e.target.closest('.history-snapshot');
        if (snapshotItem) {
            const id = snapshotItem.dataset.id;
            if (id && enterPreviewModeCb) enterPreviewModeCb(id);
        }
    });
}

// ── Public API ───────────────────────────────────────────────────────

/**
 * Bind history event listeners. Called once from DOMContentLoaded.
 * @param {Object} opts
 * @param {Function} opts.captureSnapshot - captureSnapshot(label) from terminal.js
 * @param {Function} opts.enterPreviewMode - enterPreviewMode(id) from terminal.js
 */
export function initHistory(opts = {}) {
    captureSnapshotCb = opts.captureSnapshot || null;
    enterPreviewModeCb = opts.enterPreviewMode || null;
    setupHistoryTabHandlers();
    setupDirtyChoiceModals();
}
