/**
 * Permissions: manage auto-approval rules and view audit log.
 *
 * Reads: ctx.token
 * DOM owned: #permissionsList, #permissionsAudit, #permissionsModeToggle
 */
import ctx from '../context.js';
import { escapeHtml } from '../utils.js';

let currentMode = 'safe_auto';
let currentRepo = '';
let rules = [];
let auditEntries = [];

let permissionsList, permissionsAudit, modeToggle;

// ── Render ─────────────────────────────────────────────────────────────

function renderPermissions() {
    renderRules();
    renderAudit();
    renderModeToggle();
}

function renderModeToggle() {
    if (!modeToggle) return;
    modeToggle.querySelectorAll('.perm-mode-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.mode === currentMode);
    });
}

function renderRules() {
    if (!permissionsList) return;

    if (rules.length === 0) {
        permissionsList.innerHTML = '<div class="permissions-empty">No rules configured</div>';
        return;
    }

    // Group by scope, filter repo rules to current repo
    const defaults = rules.filter(r => r.created_from === 'default');
    const thisRepoRules = rules.filter(r => r.scope === 'repo' && r.created_from !== 'default'
        && r.scope_value === currentRepo);
    const otherRepoRules = rules.filter(r => r.scope === 'repo' && r.created_from !== 'default'
        && r.scope_value !== currentRepo);
    const globalRules = rules.filter(r => r.scope === 'global' && r.created_from !== 'default');
    const sessionRules = rules.filter(r => r.scope === 'session');

    let html = '';

    if (defaults.length > 0) {
        html += '<div class="perm-group-header">DEFAULTS</div>';
        html += defaults.map(r => ruleRow(r, false)).join('');
    }

    if (thisRepoRules.length > 0) {
        const repoName = currentRepo.split('/').pop() || 'repo';
        html += `<div class="perm-group-header">REPO: ${escapeHtml(repoName)}</div>`;
        html += thisRepoRules.map(r => ruleRow(r, true)).join('');
    }

    if (otherRepoRules.length > 0) {
        html += '<div class="perm-group-header perm-group-muted">OTHER REPOS</div>';
        html += otherRepoRules.map(r => ruleRow(r, true, true)).join('');
    }

    if (globalRules.length > 0) {
        html += '<div class="perm-group-header">GLOBAL</div>';
        html += globalRules.map(r => ruleRow(r, true)).join('');
    }

    if (sessionRules.length > 0) {
        html += '<div class="perm-group-header">SESSION</div>';
        html += sessionRules.map(r => ruleRow(r, true)).join('');
    }

    permissionsList.innerHTML = html;

    // Bind delete buttons
    permissionsList.querySelectorAll('.perm-rule-delete').forEach(btn => {
        btn.addEventListener('click', e => {
            e.stopPropagation();
            deleteRule(btn.dataset.id);
        });
    });
}

function ruleRow(rule, canDelete, muted = false) {
    const eid = escapeHtml(rule.id);
    const tool = escapeHtml(rule.tool);
    const matcher = rule.matcher ? escapeHtml(rule.matcher) : '';

    // Build descriptive label
    let label;
    if (rule.matcher_type === 'tool_only' || !matcher) {
        // "Bash (any command)" or "Edit (any file)"
        const desc = tool === 'Bash' ? 'any command'
            : (tool === 'Read' || tool === 'Edit' || tool === 'Write' || tool === 'Glob' || tool === 'Grep')
                ? 'any file' : 'any';
        label = `${tool} <span class="perm-rule-desc">(${desc})</span>`;
    } else if (rule.matcher_type === 'command') {
        label = `${tool} <span class="perm-rule-desc">cmd:</span> <code>${matcher}</code>`;
    } else if (rule.matcher_type === 'path') {
        label = `${tool} <span class="perm-rule-desc">path:</span> <code>${matcher}</code>`;
    } else {
        label = `${tool}: ${matcher}`;
    }

    // Show repo name for other-repo rules
    if (muted && rule.scope_value) {
        const repoName = rule.scope_value.split('/').pop() || '';
        label += ` <span class="perm-rule-repo">(${escapeHtml(repoName)})</span>`;
    }
    const action = escapeHtml(rule.action);
    const deleteBtn = canDelete
        ? `<button class="perm-rule-delete" data-id="${eid}">&times;</button>`
        : '';
    const cls = muted ? 'perm-rule-item muted' : 'perm-rule-item';
    return `<div class="${cls}" data-id="${eid}">
        <span class="perm-rule-dot ${action}"></span>
        <span class="perm-rule-label">${label}</span>
        <span class="perm-rule-action">${action}</span>
        ${deleteBtn}
    </div>`;
}

function renderAudit() {
    if (!permissionsAudit) return;

    if (auditEntries.length === 0) {
        permissionsAudit.innerHTML = '<div class="permissions-empty">No recent activity</div>';
        return;
    }

    let html = '<div class="perm-group-header">RECENT</div>';
    html += auditEntries.slice(0, 20).map(e => {
        const ago = formatAgo(e.ts);
        const icon = e.decision === 'allow' ? '&#x2713;' : (e.decision === 'deny' ? '&#x2717;' : '&#x2026;');
        const cls = e.decision === 'allow' ? 'allow' : (e.decision === 'deny' ? 'deny' : 'prompt');
        const tool = escapeHtml(e.tool || '');
        const target = escapeHtml((e.target || '').slice(0, 40));
        const reason = escapeHtml(e.reason || '');
        const repo = e.repo ? escapeHtml(e.repo.split('/').pop()) : '';
        const repoTag = repo ? `<span class="perm-audit-repo">${repo}</span> ` : '';
        return `<div class="perm-audit-item ${cls}">
            <span class="perm-audit-ago">${ago}</span>
            <span class="perm-audit-icon">${icon}</span>
            <span class="perm-audit-label">${repoTag}${tool} ${target}</span>
            <span class="perm-audit-reason">${reason}</span>
        </div>`;
    }).join('');

    permissionsAudit.innerHTML = html;
}

function formatAgo(ts) {
    const secs = Math.floor(Date.now() / 1000 - ts);
    if (secs < 60) return `${secs}s`;
    if (secs < 3600) return `${Math.floor(secs / 60)}m`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
    return `${Math.floor(secs / 86400)}d`;
}

// ── Actions ────────────────────────────────────────────────────────────

async function setMode(mode) {
    try {
        const params = new URLSearchParams({ mode, token: ctx.token });
        const resp = await ctx.apiFetch(`/api/permissions/mode?${params}`, { method: 'POST' });
        if (resp.ok) {
            const data = await resp.json();
            if (data.status === 'ok') {
                currentMode = data.mode;
                renderModeToggle();
                ctx.showToast(`Mode: ${currentMode}`, 'info');
            }
        }
    } catch (e) {
        console.error('Failed to set permission mode:', e);
    }
}

async function deleteRule(ruleId) {
    try {
        const params = new URLSearchParams({ id: ruleId, token: ctx.token });
        const resp = await ctx.apiFetch(`/api/permissions/rules?${params}`, { method: 'DELETE' });
        if (resp.ok) {
            rules = rules.filter(r => r.id !== ruleId);
            renderRules();
            ctx.showToast('Rule removed', 'info');
        }
    } catch (e) {
        console.error('Failed to delete permission rule:', e);
    }
}

// ── Load from server ───────────────────────────────────────────────────

export async function loadPermissions() {
    try {
        const params = new URLSearchParams({ token: ctx.token });
        const [rulesResp, auditResp] = await Promise.all([
            ctx.apiFetch(`/api/permissions/rules?${params}`),
            ctx.apiFetch(`/api/permissions/audit?${params}&limit=30`),
        ]);
        if (rulesResp.ok) {
            const data = await rulesResp.json();
            currentMode = data.mode || 'safe_auto';
            currentRepo = data.repo || '';
            rules = data.rules || [];
        }
        if (auditResp.ok) {
            const data = await auditResp.json();
            auditEntries = data.entries || [];
        }
        renderPermissions();
    } catch (e) {
        console.error('Failed to load permissions:', e);
    }
}

// ── Init ───────────────────────────────────────────────────────────────

// Re-entry guard. Repeated initPermissions() calls are no-ops; otherwise
// re-binding listeners on every call would stack handlers.
let _permissionsInitialized = false;

export function initPermissions() {
    if (_permissionsInitialized) return;
    _permissionsInitialized = true;
    permissionsList = document.getElementById('permissionsList');
    permissionsAudit = document.getElementById('permissionsAudit');
    modeToggle = document.getElementById('permissionsModeToggle');

    if (modeToggle) {
        modeToggle.addEventListener('click', e => {
            const btn = e.target.closest('.perm-mode-btn');
            if (btn && btn.dataset.mode) {
                setMode(btn.dataset.mode);
            }
        });
    }

    loadPermissions();
}
