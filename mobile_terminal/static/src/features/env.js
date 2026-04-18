/**
 * ENV tab: .env file editor with scope switching.
 *
 * Reads: ctx.token
 * DOM owned: #envVarList, #envReloadBanner, ENV form fields
 * Timers: none
 */
import ctx from '../context.js';
import { escapeHtml } from '../utils.js';

// Module-local state
let envEditingKey = null;
let envDirty = false;
let envVarsCache = [];
let envCurrentScope = 'repo';
let envValueMaskState = {};

// ── Core functions ───────────────────────────────────────────────────

async function loadEnvVars() {
    const list = document.getElementById('envVarList');
    if (!list) return;

    list.innerHTML = '<p class="process-description">Loading...</p>';
    envValueMaskState = {};

    try {
        const response = await ctx.apiFetch(`/api/env?scope=${envCurrentScope}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();

        envVarsCache = data.vars || [];

        if (envVarsCache.length === 0) {
            const msg = data.exists
                ? 'No variables in .env file.'
                : '.env file does not exist yet. Add a variable to create it.';
            list.innerHTML = `<p class="process-description">${escapeHtml(msg)}</p>`;
            return;
        }

        let html = '';
        for (const v of envVarsCache) {
            html += renderEnvVarCard(v.key, v.value);
        }
        list.innerHTML = html;

    } catch (error) {
        console.error('Failed to load env vars:', error);
        list.innerHTML = '<p class="process-description">Failed to load env vars.</p>';
    }
}

function renderEnvVarCard(key, value) {
    return `<div class="mcp-server-item">
        <div class="mcp-server-info">
            <span class="mcp-server-name">${escapeHtml(key)}</span>
            <span class="env-var-value masked" data-key="${escapeHtml(key)}">\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022</span>
        </div>
        <div class="mcp-server-actions">
            <button class="mcp-server-edit" data-name="${escapeHtml(key)}">Edit</button>
            <button class="mcp-server-remove" data-name="${escapeHtml(key)}">Remove</button>
        </div>
    </div>`;
}

function editEnvVar(key, value) {
    envEditingKey = key;

    const keyInput = document.getElementById('envKeyInput');
    const valueInput = document.getElementById('envValueInput');
    const header = document.getElementById('envFormHeader');
    const addBtn = document.getElementById('envAddBtn');
    const cancelBtn = document.getElementById('envCancelEditBtn');
    const toggle = document.getElementById('envValueToggle');

    if (keyInput) { keyInput.value = key; keyInput.disabled = true; }
    if (valueInput) { valueInput.value = value; valueInput.type = 'text'; }
    if (toggle) toggle.textContent = 'Hide';
    if (header) header.textContent = 'Edit Variable';
    if (addBtn) addBtn.textContent = 'Save';
    if (cancelBtn) cancelBtn.classList.remove('hidden');
}

function cancelEnvEdit() {
    envEditingKey = null;

    const keyInput = document.getElementById('envKeyInput');
    const valueInput = document.getElementById('envValueInput');
    const header = document.getElementById('envFormHeader');
    const addBtn = document.getElementById('envAddBtn');
    const cancelBtn = document.getElementById('envCancelEditBtn');
    const toggle = document.getElementById('envValueToggle');

    if (keyInput) { keyInput.value = ''; keyInput.disabled = false; }
    if (valueInput) { valueInput.value = ''; valueInput.type = 'password'; }
    if (toggle) toggle.textContent = 'Show';
    if (header) header.textContent = 'Add Variable';
    if (addBtn) addBtn.textContent = 'Set';
    if (cancelBtn) cancelBtn.classList.add('hidden');
}

async function addEnvVar() {
    const keyInput = document.getElementById('envKeyInput');
    const valueInput = document.getElementById('envValueInput');

    const key = envEditingKey || (keyInput?.value || '').trim();
    const value = valueInput?.value || '';

    if (!key) {
        ctx.showToast('Key is required', 'error');
        return;
    }

    try {
        const response = await ctx.apiFetch(`/api/env`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scope: envCurrentScope, key, value }),
        });

        const data = await response.json();

        if (!response.ok) {
            ctx.showToast(data.error || 'Failed to set variable', 'error');
            return;
        }

        const action = data.updated ? 'Updated' : 'Added';
        ctx.showToast(`${action} ${key}`, 'success');
        cancelEnvEdit();
        await loadEnvVars();
        envSetDirty();

    } catch (error) {
        console.error('Failed to set env var:', error);
        ctx.showToast('Failed to set variable', 'error');
    }
}

async function removeEnvVar(key) {
    if (!confirm(`Remove "${key}" from .env?`)) return;

    try {
        const response = await ctx.apiFetch(
            `/api/env/${encodeURIComponent(key)}?scope=${envCurrentScope}`,
            { method: 'DELETE' }
        );

        const data = await response.json();

        if (!response.ok) {
            ctx.showToast(data.error || 'Failed to remove variable', 'error');
            return;
        }

        ctx.showToast(`Removed ${key}`, 'success');
        await loadEnvVars();
        envSetDirty();

    } catch (error) {
        console.error('Failed to remove env var:', error);
        ctx.showToast('Failed to remove variable', 'error');
    }
}

function envSetDirty() {
    envDirty = true;
    const banner = document.getElementById('envReloadBanner');
    if (banner) banner.classList.remove('hidden');
}

async function envReload() {
    const btn = document.getElementById('envReloadBtn');
    if (btn) btn.disabled = true;

    try {
        const response = await ctx.apiFetch(`/api/reload-env`, { method: 'POST' });
        const data = await response.json();

        if (response.ok) {
            ctx.showToast(data.message || 'Env reloaded', 'success');
            envDirty = false;
            const banner = document.getElementById('envReloadBanner');
            if (banner) banner.classList.add('hidden');
        } else {
            ctx.showToast(data.error || 'Reload failed', 'error');
        }
    } catch (error) {
        console.error('Failed to reload env:', error);
        ctx.showToast('Failed to reload env', 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ── Public API ───────────────────────────────────────────────────────

/**
 * Bind ENV event listeners. Called once from DOMContentLoaded.
 */
// Re-entry guard. Repeated initEnv() calls are no-ops; otherwise
// re-binding listeners on every call would stack handlers.
let _envInitialized = false;

export function initEnv() {
    if (_envInitialized) return;
    _envInitialized = true;
    document.getElementById('envScopeSelect')?.addEventListener('change', (e) => {
        envCurrentScope = e.target.value;
        loadEnvVars();
    });
    document.getElementById('envAddBtn')?.addEventListener('click', addEnvVar);
    document.getElementById('envCancelEditBtn')?.addEventListener('click', cancelEnvEdit);
    document.getElementById('envReloadBtn')?.addEventListener('click', envReload);
    document.getElementById('envValueToggle')?.addEventListener('click', () => {
        const input = document.getElementById('envValueInput');
        const btn = document.getElementById('envValueToggle');
        if (input && btn) {
            const show = input.type === 'password';
            input.type = show ? 'text' : 'password';
            btn.textContent = show ? 'Hide' : 'Show';
        }
    });

    // Env var list event delegation (edit/remove/reveal)
    document.getElementById('envVarList')?.addEventListener('click', (e) => {
        const editBtn = e.target.closest('.mcp-server-edit');
        const removeBtn = e.target.closest('.mcp-server-remove');
        const valueEl = e.target.closest('.env-var-value');

        if (editBtn) {
            const key = editBtn.dataset.name;
            if (key) {
                const entry = envVarsCache.find(v => v.key === key);
                if (entry) editEnvVar(entry.key, entry.value);
            }
        } else if (removeBtn) {
            const key = removeBtn.dataset.name;
            if (key) removeEnvVar(key);
        } else if (valueEl) {
            const key = valueEl.dataset.key;
            if (key) {
                envValueMaskState[key] = !envValueMaskState[key];
                const entry = envVarsCache.find(v => v.key === key);
                if (entry) {
                    if (envValueMaskState[key]) {
                        valueEl.textContent = entry.value;
                        valueEl.classList.remove('masked');
                    } else {
                        valueEl.textContent = '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022';
                        valueEl.classList.add('masked');
                    }
                }
            }
        }
    });
}

/**
 * Load ENV tab content. Called when ENV tab becomes active.
 */
export function loadEnv() {
    loadEnvVars();
}
