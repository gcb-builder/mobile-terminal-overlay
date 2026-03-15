/**
 * MCP tab: plugin management, MCP server CRUD, and agent restart.
 *
 * Reads: ctx.token, ctx.activeTarget, ctx.currentSession
 * DOM owned: #pluginList, #mcpServerList, #mcpRestartBanner, MCP form fields
 * Timers: none
 */
import ctx from '../context.js';
import { escapeHtml, shellSplit } from '../utils.js';

// Module-local state
let mcpEditingName = null;
let mcpDirty = false;
let mcpServersCache = {};
let marketplaceCache = [];
let marketplaceCategory = '';
let marketplaceSearch = '';
let mcpCatalogCache = [];
let mcpCatalogSearch = '';

// Callback for resetting agent health tracking after restart
// Set by terminal.js via initMcp(opts)
let onAgentRestarted = null;

// ── Plugin functions ─────────────────────────────────────────────────

async function loadPlugins() {
    const list = document.getElementById('pluginList');
    if (!list) return;

    try {
        const response = await ctx.apiFetch(`/api/plugins?token=${ctx.token}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();

        const enabled = data.enabled || {};
        const installed = data.installed || [];
        const allIds = new Set([...Object.keys(enabled), ...installed]);

        if (allIds.size === 0) {
            list.innerHTML = '<p class="process-description">No plugins found.</p>';
            return;
        }

        let html = '';
        for (const id of allIds) {
            const isEnabled = !!enabled[id];
            const shortName = id.split('@')[0];
            html += `<div class="mcp-plugin-item">
                <span class="mcp-plugin-name" title="${escapeHtml(id)}">${escapeHtml(shortName)}</span>
                <label class="mcp-toggle">
                    <input type="checkbox" data-plugin="${escapeHtml(id)}" ${isEnabled ? 'checked' : ''}>
                    <span class="mcp-toggle-slider"></span>
                </label>
            </div>`;
        }
        list.innerHTML = html;

        list.querySelectorAll('input[data-plugin]').forEach(input => {
            input.addEventListener('change', () => {
                togglePlugin(input.dataset.plugin, input.checked);
            });
        });

    } catch (error) {
        console.error('Failed to load plugins:', error);
        list.innerHTML = '<p class="process-description">Failed to load plugins.</p>';
    }
}

async function togglePlugin(name, enabled) {
    try {
        const response = await ctx.apiFetch(`/api/plugins/toggle?token=${ctx.token}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, enabled }),
        });

        const data = await response.json();

        if (!response.ok) {
            ctx.showToast(data.error || 'Failed to toggle plugin', 'error');
            await loadPlugins();
            return false;
        }

        const action = enabled ? 'Enabled' : 'Disabled';
        ctx.showToast(`${action} ${name.split('@')[0]}`, 'success');
        mcpSetDirty();
        return true;

    } catch (error) {
        console.error('Failed to toggle plugin:', error);
        ctx.showToast('Failed to toggle plugin', 'error');
        await loadPlugins();
        return false;
    }
}

async function addPlugin() {
    const input = document.getElementById('pluginIdInput');
    const name = (input?.value || '').trim();
    if (!name) {
        ctx.showToast('Plugin ID is required', 'error');
        return;
    }

    const ok = await togglePlugin(name, true);
    if (ok) {
        if (input) input.value = '';
        await loadPlugins();
    }
}

// ── Marketplace browse ───────────────────────────────────────────────

async function loadMarketplace() {
    const list = document.getElementById('pluginBrowseList');
    if (!list) return;

    try {
        const response = await ctx.apiFetch(`/api/plugins/marketplace?token=${ctx.token}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        marketplaceCache = data.plugins || [];
    } catch (error) {
        console.error('Failed to load marketplace:', error);
        marketplaceCache = [];
    }

    // Build category pills
    const categories = [...new Set(marketplaceCache.map(p => p.category).filter(Boolean))].sort();
    const filterDiv = document.getElementById('pluginCategoryFilter');
    if (filterDiv) {
        let pills = `<button class="plugin-category-pill${!marketplaceCategory ? ' active' : ''}" data-cat="">All</button>`;
        for (const cat of categories) {
            const label = cat.charAt(0).toUpperCase() + cat.slice(1);
            pills += `<button class="plugin-category-pill${marketplaceCategory === cat ? ' active' : ''}" data-cat="${escapeHtml(cat)}">${escapeHtml(label)}</button>`;
        }
        filterDiv.innerHTML = pills;
    }

    renderMarketplace();
}

function renderMarketplace() {
    const list = document.getElementById('pluginBrowseList');
    if (!list) return;

    let filtered = marketplaceCache;

    if (marketplaceCategory) {
        filtered = filtered.filter(p => p.category === marketplaceCategory);
    }

    if (marketplaceSearch) {
        const q = marketplaceSearch.toLowerCase();
        filtered = filtered.filter(p =>
            p.name.toLowerCase().includes(q) ||
            p.description.toLowerCase().includes(q)
        );
    }

    if (filtered.length === 0) {
        list.innerHTML = '<p class="process-description">No plugins match.</p>';
        return;
    }

    let html = '';
    for (const p of filtered) {
        const catBadge = p.category
            ? `<span class="plugin-browse-category">${escapeHtml(p.category)}</span>`
            : '';
        html += `<div class="plugin-browse-item">
            <div class="plugin-browse-info">
                <div class="plugin-browse-name">${escapeHtml(p.name)}</div>
                <div class="plugin-browse-desc">${escapeHtml(p.description)}</div>
                ${catBadge}
            </div>
            <label class="mcp-toggle">
                <input type="checkbox" data-browse-plugin="${escapeHtml(p.id)}" ${p.enabled ? 'checked' : ''}>
                <span class="mcp-toggle-slider"></span>
            </label>
        </div>`;
    }
    list.innerHTML = html;

    list.querySelectorAll('input[data-browse-plugin]').forEach(input => {
        input.addEventListener('change', async () => {
            const id = input.dataset.browsePlugin;
            const enabled = input.checked;
            const ok = await togglePlugin(id, enabled);
            if (!ok) {
                input.checked = !enabled; // revert on failure
            } else {
                // Update cache
                const entry = marketplaceCache.find(p => p.id === id);
                if (entry) entry.enabled = enabled;
            }
        });
    });
}

let _searchTimer = null;
function onPluginSearch(e) {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => {
        marketplaceSearch = (e.target.value || '').trim();
        renderMarketplace();
    }, 200);
}

// ── MCP Server catalog ───────────────────────────────────────────────

async function loadMcpCatalog() {
    const list = document.getElementById('mcpCatalogList');
    if (!list) return;

    try {
        const response = await ctx.apiFetch(`/api/mcp-servers/catalog?token=${ctx.token}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        mcpCatalogCache = data.servers || [];
    } catch (error) {
        console.error('Failed to load MCP catalog:', error);
        mcpCatalogCache = [];
    }

    renderMcpCatalog();
}

function renderMcpCatalog() {
    const list = document.getElementById('mcpCatalogList');
    if (!list) return;

    let filtered = mcpCatalogCache;
    if (mcpCatalogSearch) {
        const q = mcpCatalogSearch.toLowerCase();
        filtered = filtered.filter(s =>
            s.name.toLowerCase().includes(q) ||
            s.description.toLowerCase().includes(q)
        );
    }

    if (filtered.length === 0) {
        list.innerHTML = mcpCatalogCache.length === 0
            ? '<p class="process-description">No catalog available.</p>'
            : '<p class="process-description">No servers match.</p>';
        return;
    }

    let html = '';
    for (const s of filtered) {
        const typeBadge = `<span class="plugin-browse-category">${escapeHtml(s.type)}</span>`;
        html += `<div class="plugin-browse-item">
            <div class="plugin-browse-info">
                <div class="plugin-browse-name">${escapeHtml(s.name)}</div>
                <div class="plugin-browse-desc">${escapeHtml(s.description)}</div>
                ${typeBadge}
            </div>
            <button class="process-action-btn catalog-add-btn${s.configured ? ' configured' : ''}" data-catalog-name="${escapeHtml(s.name)}">${s.configured ? 'Added' : 'Add'}</button>
        </div>`;
    }
    list.innerHTML = html;

    list.querySelectorAll('.catalog-add-btn').forEach(btn => {
        if (btn.classList.contains('configured')) {
            btn.disabled = true;
            return;
        }
        btn.addEventListener('click', () => {
            const name = btn.dataset.catalogName;
            const server = mcpCatalogCache.find(s => s.name === name);
            if (!server) return;
            addCatalogServer(server, btn);
        });
    });
}

async function addCatalogServer(server, btn) {
    const config = server.config;
    const body = { name: server.name };

    // stdio servers have command+args, others use type+url
    if (config.command) {
        body.command = config.command;
        body.args = config.args || [];
    } else {
        // For http/sse servers, store as url-type config
        body.command = config.type || 'http';
        body.args = [config.url || ''];
    }

    try {
        const response = await ctx.apiFetch(`/api/mcp-servers?token=${ctx.token}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await response.json();
        if (!response.ok) {
            ctx.showToast(data.error || 'Failed to add server', 'error');
            return;
        }
        ctx.showToast(`Added ${server.name}`, 'success');
        btn.textContent = 'Added';
        btn.disabled = true;
        btn.classList.add('configured');
        server.configured = true;
        await loadMcpServers();
        mcpSetDirty();
    } catch (error) {
        console.error('Failed to add catalog server:', error);
        ctx.showToast('Failed to add server', 'error');
    }
}

let _catalogSearchTimer = null;
function onCatalogSearch(e) {
    clearTimeout(_catalogSearchTimer);
    _catalogSearchTimer = setTimeout(() => {
        mcpCatalogSearch = (e.target.value || '').trim();
        renderMcpCatalog();
    }, 200);
}

// ── MCP Server functions ─────────────────────────────────────────────

async function loadMcpServers() {
    const list = document.getElementById('mcpServerList');
    const errorDiv = document.getElementById('mcpError');
    if (!list) return;

    list.innerHTML = '<p class="process-description">Loading...</p>';
    if (errorDiv) {
        errorDiv.classList.add('hidden');
        errorDiv.textContent = '';
    }

    try {
        const response = await ctx.apiFetch(`/api/mcp-servers?token=${ctx.token}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();

        if (data.error && errorDiv) {
            errorDiv.textContent = data.error;
            errorDiv.classList.remove('hidden');
        }

        mcpServersCache = data.servers || {};
        const names = Object.keys(mcpServersCache);

        if (names.length === 0) {
            list.innerHTML = '<p class="process-description">No MCP servers configured.</p>';
            return;
        }

        let html = '';
        for (const name of names) {
            html += renderMcpServerCard(name, mcpServersCache[name]);
        }
        list.innerHTML = html;

    } catch (error) {
        console.error('Failed to load MCP servers:', error);
        list.innerHTML = '<p class="process-description">Failed to load MCP servers.</p>';
    }
}

function renderMcpServerCard(name, config) {
    const cmd = config.command || '';
    const args = (config.args || []).join(' ');
    const cmdDisplay = args ? `${cmd} ${args}` : cmd;
    return `<div class="mcp-server-item">
        <div class="mcp-server-info">
            <span class="mcp-server-name">${escapeHtml(name)}</span>
            <span class="mcp-server-cmd">${escapeHtml(cmdDisplay)}</span>
        </div>
        <div class="mcp-server-actions">
            <button class="mcp-server-edit" data-name="${escapeHtml(name)}">Edit</button>
            <button class="mcp-server-remove" data-name="${escapeHtml(name)}">Remove</button>
        </div>
    </div>`;
}

function editMcpServer(name, config) {
    mcpEditingName = name;

    const nameInput = document.getElementById('mcpNameInput');
    const cmdInput = document.getElementById('mcpCommandInput');
    const argsInput = document.getElementById('mcpArgsInput');
    const header = document.getElementById('mcpFormHeader');
    const addBtn = document.getElementById('mcpAddBtn');
    const cancelBtn = document.getElementById('mcpCancelEditBtn');

    if (nameInput) { nameInput.value = name; nameInput.disabled = true; }
    if (cmdInput) cmdInput.value = config.command || '';
    if (argsInput) argsInput.value = (config.args || []).join(' ');
    if (header) header.textContent = 'Edit Server';
    if (addBtn) addBtn.textContent = 'Save Changes';
    if (cancelBtn) cancelBtn.classList.remove('hidden');
}

function cancelMcpEdit() {
    mcpEditingName = null;

    const nameInput = document.getElementById('mcpNameInput');
    const cmdInput = document.getElementById('mcpCommandInput');
    const argsInput = document.getElementById('mcpArgsInput');
    const header = document.getElementById('mcpFormHeader');
    const addBtn = document.getElementById('mcpAddBtn');
    const cancelBtn = document.getElementById('mcpCancelEditBtn');

    if (nameInput) { nameInput.value = ''; nameInput.disabled = false; }
    if (cmdInput) cmdInput.value = '';
    if (argsInput) argsInput.value = '';
    if (header) header.textContent = 'Add Server';
    if (addBtn) addBtn.textContent = 'Add Server';
    if (cancelBtn) cancelBtn.classList.add('hidden');
}

async function addMcpServer() {
    const nameInput = document.getElementById('mcpNameInput');
    const cmdInput = document.getElementById('mcpCommandInput');
    const argsInput = document.getElementById('mcpArgsInput');
    const resultDiv = document.getElementById('mcpResult');

    const name = mcpEditingName || (nameInput?.value || '').trim();
    const command = (cmdInput?.value || '').trim();
    const argsStr = (argsInput?.value || '').trim();
    const args = argsStr ? shellSplit(argsStr) : [];

    if (!name) {
        ctx.showToast('Server name is required', 'error');
        return;
    }
    if (!command) {
        ctx.showToast('Command is required', 'error');
        return;
    }

    try {
        const response = await ctx.apiFetch(`/api/mcp-servers?token=${ctx.token}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, command, args }),
        });

        const data = await response.json();

        if (!response.ok) {
            ctx.showToast(data.error || 'Failed to add server', 'error');
            if (resultDiv) {
                resultDiv.textContent = data.error || 'Error';
                resultDiv.className = 'process-result error';
            }
            return;
        }

        const action = data.updated ? 'Updated' : 'Added';
        ctx.showToast(`${action} ${name}`, 'success');
        cancelMcpEdit();
        await loadMcpServers();
        mcpSetDirty();

    } catch (error) {
        console.error('Failed to add MCP server:', error);
        ctx.showToast('Failed to add server', 'error');
    }
}

async function removeMcpServer(name) {
    if (!confirm(`Remove MCP server "${name}"?`)) return;

    try {
        const response = await ctx.apiFetch(
            `/api/mcp-servers/${encodeURIComponent(name)}?token=${ctx.token}`,
            { method: 'DELETE' }
        );

        const data = await response.json();

        if (!response.ok) {
            ctx.showToast(data.error || 'Failed to remove server', 'error');
            return;
        }

        ctx.showToast(`Removed ${name}`, 'success');
        await loadMcpServers();
        mcpSetDirty();

    } catch (error) {
        console.error('Failed to remove MCP server:', error);
        ctx.showToast('Failed to remove server', 'error');
    }
}

// ── Dirty state & restart ────────────────────────────────────────────

function showRestartBanner(banner, agentRunning) {
    if (!banner) return;
    const span = banner.querySelector('span');
    const oneBtn = banner.querySelector('.mcp-restart-btn:first-child');
    const allBtn = banner.querySelector('.mcp-restart-btn:last-child');

    if (agentRunning) {
        if (span) span.textContent = 'Restart to apply';
        if (oneBtn) oneBtn.classList.remove('hidden');
        if (allBtn) allBtn.classList.remove('hidden');
    } else {
        if (span) span.textContent = 'Applies on next start';
        if (oneBtn) oneBtn.classList.add('hidden');
        if (allBtn) allBtn.classList.add('hidden');
    }
    banner.classList.remove('hidden');
}

async function mcpSetDirty() {
    mcpDirty = true;

    let agentRunning = false;
    if (ctx.activeTarget) {
        try {
            const resp = await ctx.apiFetch(`/api/health/agent?pane_id=${encodeURIComponent(ctx.activeTarget)}&token=${ctx.token}`);
            if (resp.ok) {
                const data = await resp.json();
                agentRunning = !!data.running;
            }
        } catch (e) { /* ignore */ }
    }

    showRestartBanner(document.getElementById('mcpRestartBanner'), agentRunning);
    showRestartBanner(document.getElementById('pluginsRestartBanner'), agentRunning);
}

async function stopAgentInPane(paneId, session) {
    await ctx.apiFetch(`/api/sendkey?key=ctrl-c&session=${encodeURIComponent(session)}&msg_id=mcp-stop-${paneId}&token=${ctx.token}`, {
        method: 'POST',
    });

    for (let i = 0; i < 20; i++) {
        await new Promise(resolve => setTimeout(resolve, 500));
        try {
            const resp = await ctx.apiFetch(`/api/health/agent?pane_id=${encodeURIComponent(paneId)}&token=${ctx.token}`);
            if (resp.ok) {
                const data = await resp.json();
                if (!data.running) return true;
            }
        } catch (e) { /* ignore */ }
    }

    // Second Ctrl-C attempt
    await ctx.apiFetch(`/api/sendkey?key=ctrl-c&session=${encodeURIComponent(session)}&msg_id=mcp-stop2-${paneId}&token=${ctx.token}`, {
        method: 'POST',
    });
    await new Promise(resolve => setTimeout(resolve, 2000));
    return false;
}

async function startAgentWithResume(paneId) {
    return ctx.apiFetch(`/api/agent/start?pane_id=${encodeURIComponent(paneId)}&token=${ctx.token}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ startup_command: 'claude --resume' }),
    });
}

async function mcpRestartAgents(mode) {
    const oneBtn = document.getElementById('mcpRestartOneBtn');
    const allBtn = document.getElementById('mcpRestartAllBtn');
    const clickedBtn = mode === 'one' ? oneBtn : allBtn;

    const msg = mode === 'one'
        ? 'Restart the active agent? It will resume with --resume.'
        : 'Restart ALL running agents across all sessions? They will resume with --resume.';
    if (!confirm(msg)) return;

    if (oneBtn) oneBtn.disabled = true;
    if (allBtn) allBtn.disabled = true;
    if (clickedBtn) clickedBtn.textContent = 'Stopping...';

    try {
        let panesToRestart = [];

        if (mode === 'one') {
            if (!ctx.activeTarget) {
                ctx.showToast('No active target selected', 'info');
                return;
            }
            panesToRestart = [{ paneId: ctx.activeTarget, session: ctx.currentSession }];
        } else {
            let sessions = [ctx.currentSession];
            try {
                const sessResp = await ctx.apiFetch(`/api/tmux/sessions?token=${ctx.token}`);
                if (sessResp.ok) {
                    const sessData = await sessResp.json();
                    sessions = sessData.sessions || [ctx.currentSession];
                }
            } catch (e) { /* use current session */ }

            const stateResults = await Promise.all(
                sessions.map(sess =>
                    ctx.apiFetch(`/api/team/state?token=${ctx.token}&session=${encodeURIComponent(sess)}`)
                        .then(r => r.ok ? r.json() : null)
                        .catch(() => null)
                )
            );
            stateResults.forEach((team, i) => {
                if (!team) return;
                for (const p of (team.panes || [])) {
                    if (p.running) {
                        panesToRestart.push({ paneId: p.pane_id, session: sessions[i] });
                    }
                }
            });
        }

        if (panesToRestart.length === 0) {
            ctx.showToast('No running agents found', 'info');
            return;
        }

        await Promise.all(panesToRestart.map(p => stopAgentInPane(p.paneId, p.session)));

        if (clickedBtn) clickedBtn.textContent = `Starting ${panesToRestart.length}...`;

        const startResults = await Promise.allSettled(
            panesToRestart.map(p => startAgentWithResume(p.paneId))
        );
        const started = startResults.filter(r => r.status === 'fulfilled' && r.value.ok).length;

        const label = panesToRestart.length === 1 ? 'agent' : 'agents';
        ctx.showToast(`Restarted ${started}/${panesToRestart.length} ${label} with --resume`, 'success');

        mcpDirty = false;
        document.getElementById('mcpRestartBanner')?.classList.add('hidden');
        document.getElementById('pluginsRestartBanner')?.classList.add('hidden');

        // Notify terminal.js to reset health tracking if active pane was restarted
        if (panesToRestart.some(p => p.paneId === ctx.activeTarget) && onAgentRestarted) {
            onAgentRestarted();
        }

    } catch (error) {
        console.error('Failed to restart agents:', error);
        ctx.showToast('Failed to restart agents', 'error');
    } finally {
        if (oneBtn) { oneBtn.disabled = false; oneBtn.textContent = 'Restart Pane'; }
        if (allBtn) { allBtn.disabled = false; allBtn.textContent = 'Restart All'; }
    }
}

// ── Public API ───────────────────────────────────────────────────────

/**
 * Bind MCP event listeners. Called once from DOMContentLoaded.
 * @param {Object} opts
 * @param {Function} opts.onAgentRestarted - callback when agent restart completes
 */
export function initMcp(opts = {}) {
    onAgentRestarted = opts.onAgentRestarted || null;

    // MCP Servers tab
    document.getElementById('mcpRefreshBtn')?.addEventListener('click', () => { loadMcpServers(); loadMcpCatalog(); });
    document.getElementById('mcpCatalogSearch')?.addEventListener('input', onCatalogSearch);
    document.getElementById('mcpAddBtn')?.addEventListener('click', addMcpServer);
    document.getElementById('mcpCancelEditBtn')?.addEventListener('click', cancelMcpEdit);
    document.getElementById('mcpRestartOneBtn')?.addEventListener('click', () => mcpRestartAgents('one'));
    document.getElementById('mcpRestartAllBtn')?.addEventListener('click', () => mcpRestartAgents('all'));

    document.getElementById('mcpServerList')?.addEventListener('click', (e) => {
        const editBtn = e.target.closest('.mcp-server-edit');
        const removeBtn = e.target.closest('.mcp-server-remove');
        if (editBtn) {
            const name = editBtn.dataset.name;
            if (name && mcpServersCache[name]) {
                editMcpServer(name, mcpServersCache[name]);
            }
        } else if (removeBtn) {
            const name = removeBtn.dataset.name;
            if (name) removeMcpServer(name);
        }
    });

    // Plugins tab
    document.getElementById('pluginAddBtn')?.addEventListener('click', addPlugin);
    document.getElementById('pluginSearchInput')?.addEventListener('input', onPluginSearch);

    document.getElementById('pluginCategoryFilter')?.addEventListener('click', (e) => {
        const pill = e.target.closest('.plugin-category-pill');
        if (!pill) return;
        marketplaceCategory = pill.dataset.cat || '';
        document.querySelectorAll('.plugin-category-pill').forEach(p => p.classList.remove('active'));
        pill.classList.add('active');
        renderMarketplace();
    });

    // Plugins tab restart buttons (mirror MCP restart)
    document.querySelector('.plugins-restart-one')?.addEventListener('click', () => mcpRestartAgents('one'));
    document.querySelector('.plugins-restart-all')?.addEventListener('click', () => mcpRestartAgents('all'));
}

/**
 * Load MCP tab content (servers only). Called when MCP tab becomes active.
 */
export function loadMcp() {
    loadMcpServers();
    loadMcpCatalog();
}

/**
 * Load Plugins tab content. Called when Plugins tab becomes active.
 */
export function loadPluginsTab() {
    loadPlugins();
    loadMarketplace();
}
