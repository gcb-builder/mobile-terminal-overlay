/**
 * Team view: team cards, dispatch bar, agent selector, keyboard navigation.
 *
 * Reads: ctx.token, ctx.currentSession, ctx.teamState, ctx.activeTarget,
 *        ctx.uiMode, ctx.currentView
 * DOM owned: #teamView, #teamCardsContainer, #systemStatusStrip,
 *            #teamDispatchBar, #terminalAgentSelector, #desktopDensityToggle,
 *            #teamSearchInput, #teamFilterBar
 * Timers: teamCardRefreshTimer (5s interval for auto-refresh)
 */
import ctx from '../context.js';
import { escapeHtml } from '../utils.js';
import { deriveUIState, deriveSystemSummary } from '../ui-state.js';
import { initTeamLauncher, showLaunchTeamModal } from './team-launcher.js';

export { showLaunchTeamModal };

// Module-local state
let dispatchPlansCache = null;
let dispatchInFlight = false;
let teamRefreshInFlight = false;
let teamDensity = localStorage.getItem('mto_team_density') || 'compact';
let teamFilterState = { search: '', filter: 'all' };
let selectedAgentIndex = -1;
let lastRenderedAgentNames = []; // Track agent order for keyboard nav
let teamCardRefreshTimer = null;
let lastSystemSummary = null;

// Callbacks set during init
let selectTargetCb = null;
let switchToViewCb = null;
let fetchWithTimeoutCb = null;
let updateActionBarCb = null;

// ── Helpers ──────────────────────────────────────────────────────────

function showToast(msg, type) {
    if (ctx.showToast) ctx.showToast(msg, type);
}

function shouldTeamRefreshRun() {
    return document.visibilityState === 'visible' &&
           (ctx.uiMode === 'desktop-multipane' || ctx.currentView === 'team');
}

// ── Agent selector ───────────────────────────────────────────────────

/**
 * Populate terminal agent selector dropdown from team state.
 * Only shown when team is detected.
 */
export function updateTerminalAgentSelector() {
    const wrapper = document.getElementById('terminalAgentSelector');
    const select = document.getElementById('terminalAgentSelect');
    if (!wrapper || !select) return;

    const hasTeam = ctx.teamState && ctx.teamState.has_team && ctx.teamState.team;
    if (!hasTeam) {
        wrapper.classList.add('hidden');
        return;
    }

    wrapper.classList.remove('hidden');

    // Collect agents
    const agents = [];
    if (ctx.teamState.team.leader) agents.push(ctx.teamState.team.leader);
    if (ctx.teamState.team.agents) agents.push(...ctx.teamState.team.agents);

    // Preserve current selection
    const current = select.value || ctx.activeTarget;

    select.innerHTML = '';
    agents.forEach(agent => {
        const opt = document.createElement('option');
        opt.value = agent.target_id;
        const role = agent.team_role === 'leader' ? ' (leader)' : '';
        const phase = agent.phase ? ' \u2014 ' + agent.phase : '';
        opt.textContent = (agent.agent_name || agent.target_id) + role + phase;
        if (agent.target_id === current) opt.selected = true;
        select.appendChild(opt);
    });
}

// ── Team card refresh ────────────────────────────────────────────────

export function startTeamCardRefresh() {
    stopTeamCardRefresh();
    teamCardRefreshTimer = setInterval(() => {
        if (shouldTeamRefreshRun()) {
            refreshTeamCards();
        }
    }, 5000);
}

export function stopTeamCardRefresh() {
    if (teamCardRefreshTimer) {
        clearInterval(teamCardRefreshTimer);
        teamCardRefreshTimer = null;
    }
}

export async function refreshTeamCards() {
    if (!shouldTeamRefreshRun() || !ctx.teamState || !ctx.teamState.has_team) return;
    if (teamRefreshInFlight) return;
    teamRefreshInFlight = true;

    const sessParam = ctx.currentSession ? `&session=${encodeURIComponent(ctx.currentSession)}` : '';
    try {
        const resp = await fetchWithTimeoutCb(
            `/api/team/capture?lines=8&token=${ctx.token}${sessParam}`, {}, 5000
        );
        if (!resp.ok) return;
        const data = await resp.json();

        // Also refresh team state for latest phase/permission info
        const stateResp = await fetchWithTimeoutCb(
            `/api/team/state?token=${ctx.token}${sessParam}`, {}, 5000
        );
        if (stateResp.ok) {
            ctx.teamState = await stateResp.json();
        }

        renderTeamCards(ctx.teamState, data.captures || {});
    } catch (e) {
        console.warn('Team card refresh failed:', e);
    } finally {
        teamRefreshInFlight = false;
    }
}

// ── Team card rendering ──────────────────────────────────────────────

export function renderTeamCards(state, captures) {
    const teamViewEl = document.getElementById('teamView');
    if (!teamViewEl) return;

    // Use cards container if available (desktop restructured DOM), else fallback
    const cardsTarget = document.getElementById('teamCardsContainer') || teamViewEl;

    // Empty state: no team running
    if (!state || !state.team || !state.has_team) {
        cardsTarget.innerHTML = `
            <div class="team-no-team">
                <div class="team-no-team-text">No team running</div>
                <button class="no-team-launch-btn" id="noTeamLaunchBtn">Launch Team</button>
            </div>
        `;
        document.getElementById('noTeamLaunchBtn')?.addEventListener('click', showLaunchTeamModal);
        return;
    }

    // Repo-scoping: only show team UI when viewing the same repo as team members
    const activeTarget = ctx.targets?.find(t => t.id === ctx.activeTarget);
    const activeCwd = activeTarget?.cwd;
    if (activeCwd) {
        const allMembers = [state.team.leader, ...(state.team.agents || [])].filter(Boolean);
        const teamCwds = allMembers.map(a => a.cwd).filter(Boolean);
        if (teamCwds.length > 0) {
            // Check if active CWD shares a common repo root with any team member
            const inSameRepo = teamCwds.some(tc =>
                activeCwd.startsWith(tc) || tc.startsWith(activeCwd)
            );
            if (!inSameRepo) {
                const teamRepo = teamCwds[0].split('/').pop() || 'another repo';
                cardsTarget.innerHTML = `
                    <div class="team-no-team">
                        <div class="team-no-team-text">Team active in <strong>${escapeHtml(teamRepo)}</strong></div>
                    </div>
                `;
                return;
            }
        }
    }

    // Collect all agents
    const allAgents = [];
    if (state.team.leader) allAgents.push(state.team.leader);
    if (state.team.agents) allAgents.push(...state.team.agents);

    // Derive UIState for each agent
    const uiPairs = allAgents.map(a => ({ agent: a, ui: deriveUIState(a) }));

    const attention = uiPairs.filter(p => p.ui.section === 'attention');
    const active = uiPairs.filter(p => p.ui.section === 'active');
    const idle = uiPairs.filter(p => p.ui.section === 'idle');

    cardsTarget.innerHTML = '';

    // Track agent names for keyboard navigation
    lastRenderedAgentNames = allAgents.map(a => a.agent_name || 'unknown');

    const allIdle = attention.length === 0 && active.length === 0;

    // Dismiss button when team exists
    const dismissBar = document.createElement('div');
    dismissBar.className = 'team-dismiss-bar';
    const dismissBtn = document.createElement('button');
    dismissBtn.className = 'team-dismiss-btn';
    dismissBtn.textContent = 'Dismiss Team';
    dismissBtn.addEventListener('click', dismissTeam);
    dismissBar.appendChild(dismissBtn);
    cardsTarget.appendChild(dismissBar);

    if (attention.length) {
        cardsTarget.appendChild(renderTeamSection('Needs Attention', attention, captures, 'attention'));
    }
    if (active.length) {
        cardsTarget.appendChild(renderTeamSection('Active', active, captures, 'active'));
    }
    if (idle.length && !allIdle) {
        // Only show idle section when there's mixed state — strip handles all-idle
        const collapsed = idle.length > 1;
        cardsTarget.appendChild(renderTeamSection('Idle', idle, captures, 'idle', collapsed));
    }

    // Update system status summary
    const uiStates = uiPairs.map(p => p.ui);
    lastSystemSummary = deriveSystemSummary(allAgents, uiStates);
    updateSystemStatus(lastSystemSummary);
    if (updateActionBarCb) updateActionBarCb();

    // Auto-scroll attention section into view
    if (attention.length) {
        const attentionSection = cardsTarget.querySelector('.team-section.attention');
        if (attentionSection) {
            attentionSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    }

    // Restore selected agent highlight after re-render
    if (ctx.uiMode === 'desktop-multipane' && selectedAgentIndex >= 0) {
        restoreAgentSelection();
    }

    // Apply filters if active
    if (ctx.uiMode === 'desktop-multipane') {
        applyTeamFilters();
    }

    updateDispatchButtonState();
}

/**
 * Render a collapsible section of team cards.
 */
function renderTeamSection(title, uiPairs, captures, sectionType, collapsed = false) {
    const section = document.createElement('div');
    section.className = 'team-section ' + sectionType;

    // Header (clickable to toggle collapse)
    const header = document.createElement('div');
    header.className = 'team-section-header';

    const titleSpan = document.createElement('span');
    titleSpan.className = 'section-title';
    titleSpan.textContent = title;
    header.appendChild(titleSpan);

    const count = document.createElement('span');
    count.className = 'section-count';
    count.textContent = uiPairs.length;
    header.appendChild(count);

    const chevron = document.createElement('span');
    chevron.className = 'section-chevron';
    chevron.textContent = '\u25BC';
    header.appendChild(chevron);

    section.appendChild(header);

    // Body (card grid)
    const body = document.createElement('div');
    body.className = 'team-section-body' + (collapsed ? ' collapsed' : '');

    const grid = document.createElement('div');
    grid.className = 'team-cards-grid';

    uiPairs.forEach(({ agent, ui }) => {
        const capture = captures[agent.target_id] || {};
        grid.appendChild(createTeamCard(agent, capture, ui));
    });

    body.appendChild(grid);
    section.appendChild(body);

    // Toggle collapse on header click
    header.addEventListener('click', () => {
        body.classList.toggle('collapsed');
        section.classList.toggle('section-collapsed');
    });

    return section;
}

// ── System status ────────────────────────────────────────────────────

/**
 * Update the system status strip from summary data.
 */
function updateSystemStatus(summary) {
    if (!summary) return;

    // Hide single-agent header indicator when using system summary
    const headerPhase = document.getElementById('headerPhaseIndicator');
    if (headerPhase) headerPhase.classList.add('hidden');

    const strip = document.getElementById('systemStatusStrip');
    if (!strip) return;

    strip.classList.remove('hidden');

    // System icon + summary text
    const icon = document.getElementById('systemStateIcon');
    const summaryEl = document.getElementById('systemSummary');
    if (icon) icon.textContent = summary.icon;
    if (summaryEl) summaryEl.textContent = summary.text;

    // Leader state pill
    const leaderEl = document.getElementById('leaderState');
    if (leaderEl) {
        if (ctx.teamState && ctx.teamState.team && ctx.teamState.team.leader) {
            const leader = ctx.teamState.team.leader;
            const leaderPhase = leader.phase || 'idle';
            const labels = {
                working: 'Orchestrating',
                running_task: 'Orchestrating',
                planning: 'Planning',
                waiting: 'Waiting',
                idle: 'Idle'
            };
            leaderEl.textContent = labels[leaderPhase] || leaderPhase;
            leaderEl.classList.remove('hidden');
        } else {
            leaderEl.classList.add('hidden');
        }
    }

    // Approval count badge
    const approvalEl = document.getElementById('approvalCount');
    if (approvalEl) {
        if (summary.attentionCount > 0) {
            approvalEl.textContent = summary.attentionCount;
            approvalEl.classList.remove('hidden');
        } else {
            approvalEl.classList.add('hidden');
        }
    }
}

/**
 * Scroll to the first Needs Attention card in team view.
 */
export function scrollToFirstAttention() {
    const teamViewEl = document.getElementById('teamView');
    if (!teamViewEl) return;
    const attentionSection = teamViewEl.querySelector('.team-section.attention');
    if (attentionSection) {
        attentionSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

/** Get last system summary for updateActionBar in terminal.js */
export function getLastSystemSummary() {
    return lastSystemSummary;
}

// ── Dispatch bar ─────────────────────────────────────────────────────

export async function populateDispatchPlans() {
    const select = document.getElementById('dispatchPlanSelect');
    if (!select) return;

    try {
        const resp = await fetch(`/api/plans?token=${ctx.token}`);
        if (!resp.ok) throw new Error('Failed to load plans');
        const data = await resp.json();
        dispatchPlansCache = data.plans || [];
    } catch (e) {
        dispatchPlansCache = [];
    }

    const saved = localStorage.getItem('mto_dispatch_plan') || '';
    select.innerHTML = '<option value="">Select plan...</option>';
    dispatchPlansCache.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.filename;
        opt.textContent = p.title || p.filename;
        if (p.filename === saved) opt.selected = true;
        select.appendChild(opt);
    });
    updateDispatchButtonState();
}

function updateDispatchButtonState() {
    const select = document.getElementById('dispatchPlanSelect');
    const dispatchBtn = document.getElementById('dispatchBtn');
    const msgInput = document.getElementById('leaderMessageInput');
    const msgBtn = document.getElementById('leaderMessageBtn');
    const hasLeader = ctx.teamState && ctx.teamState.team && ctx.teamState.team.leader;

    if (dispatchBtn) {
        dispatchBtn.disabled = !select || !select.value || !hasLeader || dispatchInFlight;
    }
    if (msgBtn) {
        msgBtn.disabled = !msgInput || !msgInput.value.trim() || !hasLeader;
    }
}

async function dispatchToLeader() {
    const select = document.getElementById('dispatchPlanSelect');
    const btn = document.getElementById('dispatchBtn');
    if (!select || !select.value || !btn) return;

    dispatchInFlight = true;
    const origText = btn.textContent;
    btn.textContent = 'Sending...';
    btn.disabled = true;

    const plan = select.value;
    const sessParam = ctx.currentSession ? `&session=${encodeURIComponent(ctx.currentSession)}` : '';

    try {
        const resp = await fetchWithTimeoutCb(
            `/api/team/dispatch?plan_filename=${encodeURIComponent(plan)}&include_context=true&token=${ctx.token}${sessParam}`,
            { method: 'POST' },
            15000
        );
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.success) {
            let msg = `Dispatched to leader (${data.agents_count} agents)`;
            if (data.warning_main_agents && data.warning_main_agents.length > 0) {
                msg += ` -- WARNING: ${data.warning_main_agents.join(', ')} on main branch`;
            }
            showToast(msg, 'success');
            localStorage.setItem('mto_dispatch_plan', plan);
        } else {
            showToast(data.error || 'Dispatch failed', 'error');
        }
    } catch (e) {
        showToast('Dispatch failed: ' + e.message, 'error');
    }

    dispatchInFlight = false;
    btn.textContent = origText;
    updateDispatchButtonState();
    setTimeout(() => refreshTeamCards(), 2000);
}

async function sendLeaderMessage() {
    const input = document.getElementById('leaderMessageInput');
    if (!input || !input.value.trim()) return;
    if (!ctx.teamState || !ctx.teamState.team || !ctx.teamState.team.leader) return;

    const text = input.value.trim();
    const targetId = ctx.teamState.team.leader.target_id;

    await sendTeamInput(targetId, text);
    input.value = '';
    updateDispatchButtonState();
}

// ── Team card creation ───────────────────────────────────────────────

function createTeamCard(agent, capture, ui) {
    // Fall back to deriving UIState if not provided (backward compat)
    if (!ui) ui = deriveUIState(agent);

    const card = document.createElement('div');
    card.className = 'team-card';
    card.dataset.urgency = ui.urgency;

    // Apply urgency-based visual weight
    if (ui.urgency >= 10) {
        card.classList.add('urgency-critical');
    } else if (ui.urgency >= 8) {
        card.classList.add('urgency-high');
    } else if (ui.urgency <= 3) {
        card.classList.add('urgency-low');
    }

    // Header: phase badge + name + overflow menu
    const header = document.createElement('div');
    header.className = 'team-card-header';

    const badge = document.createElement('span');
    badge.className = 'team-card-badge badge-' + ui.badgeColor;
    badge.textContent = ui.badgeText;
    header.appendChild(badge);

    const headerRight = document.createElement('div');
    headerRight.className = 'team-card-header-right';

    const switchLink = document.createElement('button');
    switchLink.className = 'team-card-menu-btn';
    switchLink.textContent = '\u22EE';
    switchLink.title = 'Switch to terminal';
    switchLink.addEventListener('click', (e) => {
        e.stopPropagation();
        if (selectTargetCb) selectTargetCb(agent.target_id);
        if (switchToViewCb) switchToViewCb('terminal');
    });
    headerRight.appendChild(switchLink);
    header.appendChild(headerRight);

    card.appendChild(header);

    // Name + subtitle
    const info = document.createElement('div');
    info.className = 'team-card-info';

    const name = document.createElement('div');
    name.className = 'team-card-name';
    name.textContent = agent.agent_name || 'unknown';

    // Role badge or assign button
    if (agent.team_role === 'agent') {
        const roleEl = document.createElement('span');
        if (agent.assigned_role) {
            roleEl.className = `team-card-role role-${agent.assigned_role}`;
            roleEl.textContent = agent.assigned_role;
            roleEl.title = 'Click to change role';
        } else {
            roleEl.className = 'team-card-role-btn';
            roleEl.textContent = '+ role';
        }
        roleEl.addEventListener('click', (e) => {
            e.stopPropagation();
            showRoleSelector(e.target, agent.agent_name);
        });
        name.appendChild(roleEl);
    }

    info.appendChild(name);

    if (ui.subtitle) {
        const subtitle = document.createElement('div');
        subtitle.className = 'team-card-subtitle';
        subtitle.textContent = ui.subtitle;
        info.appendChild(subtitle);
    }

    card.appendChild(info);

    // Body: last 1-2 log lines (tap to switch to terminal)
    const content = (capture.content || '').trim();
    const lines = content.split('\n').filter(l => l.trim());
    if (lines.length > 0) {
        const body = document.createElement('div');
        body.className = 'team-card-body';
        const pre = document.createElement('pre');
        pre.textContent = lines.slice(-2).join('\n');
        body.appendChild(pre);
        body.addEventListener('click', () => {
            if (selectTargetCb) selectTargetCb(agent.target_id);
            if (switchToViewCb) switchToViewCb('terminal');
        });
        card.appendChild(body);
    }

    // Footer: branch + worktree (tiny)
    if (agent.git && agent.git.branch) {
        const footer = document.createElement('div');
        footer.className = 'team-card-footer-info';
        const branchText = agent.git.branch;
        const worktreeText = agent.git.worktree ? ' \u00B7 ' + agent.git.worktree : '';
        footer.textContent = branchText + worktreeText;
        if (agent.git.branch === 'main' || agent.git.branch === 'master') {
            footer.classList.add('danger');
        }
        card.appendChild(footer);
    }

    // Permission actions: full-width Allow/Deny
    if (ui.showPermissionActions) {
        const actions = document.createElement('div');
        actions.className = 'team-card-permission-actions';

        const allowBtn = document.createElement('button');
        allowBtn.className = 'team-card-action-btn allow';
        allowBtn.textContent = 'Allow';
        allowBtn.addEventListener('click', () => {
            allowBtn.disabled = true;
            denyBtn.disabled = true;
            allowBtn.textContent = 'Allowing...';
            sendTeamInput(agent.target_id, 'y');
        });

        const denyBtn = document.createElement('button');
        denyBtn.className = 'team-card-action-btn deny';
        denyBtn.textContent = 'Deny';
        denyBtn.addEventListener('click', () => {
            denyBtn.disabled = true;
            allowBtn.disabled = true;
            denyBtn.textContent = 'Denying...';
            sendTeamInput(agent.target_id, 'n');
        });

        actions.appendChild(allowBtn);
        actions.appendChild(denyBtn);
        card.appendChild(actions);
    }

    // Desktop hover actions
    if (ctx.uiMode === 'desktop-multipane') {
        addDesktopHoverActions(card, agent, ui);
    }

    return card;
}

function addDesktopHoverActions(card, agent, ui) {
    const actions = document.createElement('div');
    actions.className = 'team-card-hover-actions';

    const viewBtn = document.createElement('button');
    viewBtn.className = 'team-card-hover-btn';
    viewBtn.textContent = 'View';
    viewBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (selectTargetCb) selectTargetCb(agent.target_id);
        if (switchToViewCb) switchToViewCb('terminal');
    });
    actions.appendChild(viewBtn);

    if (ui.showPermissionActions) {
        const allowBtn = document.createElement('button');
        allowBtn.className = 'team-card-hover-btn allow-btn';
        allowBtn.textContent = 'Allow';
        allowBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            sendTeamInput(agent.target_id, 'y');
        });
        actions.appendChild(allowBtn);
    }

    card.appendChild(actions);
}

export async function sendTeamInput(targetId, text) {
    const sessParam = ctx.currentSession ? `&session=${encodeURIComponent(ctx.currentSession)}` : '';
    try {
        const resp = await fetchWithTimeoutCb(
            `/api/team/send?target_id=${encodeURIComponent(targetId)}&text=${encodeURIComponent(text)}&token=${ctx.token}${sessParam}`,
            { method: 'POST' },
            5000
        );
        if (resp.ok) {
            showToast(`Sent "${text}" to ${targetId}`, 'success');
        } else {
            const err = await resp.json().catch(() => ({}));
            showToast(err.error || 'Send failed', 'error');
        }
    } catch (e) {
        showToast('Send failed: ' + e.message, 'error');
    }
    // Refresh cards after a short delay to show updated state
    setTimeout(() => refreshTeamCards(), 1500);
}

// ── Role selector popover ────────────────────────────────────────────

function showRoleSelector(anchorEl, agentName) {
    // Remove any existing selector
    document.querySelector('.role-selector')?.remove();

    const sel = document.createElement('div');
    sel.className = 'role-selector';

    const roles = [
        { key: 'explorer', label: 'Explorer', desc: 'Search & investigate' },
        { key: 'planner', label: 'Planner', desc: 'Design & plan' },
        { key: 'executor', label: 'Executor', desc: 'Write & build' },
        { key: 'reviewer', label: 'Reviewer', desc: 'Review & test' },
        { key: null, label: 'Clear role', desc: 'Remove assignment' },
    ];

    for (const r of roles) {
        const btn = document.createElement('button');
        btn.className = 'role-selector-item';
        btn.innerHTML = `${escapeHtml(r.label)}<span class="role-desc">${escapeHtml(r.desc)}</span>`;
        btn.addEventListener('click', async () => {
            sel.remove();
            try {
                await fetch(`/api/team/role?token=${ctx.token}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ agent_name: agentName, role: r.key }),
                });
            } catch (e) {
                console.warn('Failed to set role:', e);
            }
            refreshTeamCards();
        });
        sel.appendChild(btn);
    }

    // Position near anchor
    const rect = anchorEl.getBoundingClientRect();
    sel.style.top = `${rect.bottom + 4}px`;
    sel.style.left = `${rect.left}px`;
    document.body.appendChild(sel);

    // Close on outside click
    const close = (e) => {
        if (!sel.contains(e.target) && e.target !== anchorEl) {
            sel.remove();
            document.removeEventListener('click', close);
        }
    };
    setTimeout(() => document.addEventListener('click', close), 0);
}

// ── Team filters (desktop) ───────────────────────────────────────────

export function setupTeamFilters() {
    const searchInput = document.getElementById('teamSearchInput');
    const filterBar = document.getElementById('teamFilterBar');

    if (searchInput) {
        searchInput.addEventListener('input', () => {
            teamFilterState.search = searchInput.value.toLowerCase();
            applyTeamFilters();
        });
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                searchInput.value = '';
                teamFilterState.search = '';
                searchInput.blur();
                applyTeamFilters();
            }
        });
    }

    if (filterBar) {
        filterBar.addEventListener('click', (e) => {
            const chip = e.target.closest('.team-filter-chip');
            if (!chip) return;
            const filter = chip.dataset.filter;
            if (filter) {
                teamFilterState.filter = filter;
                filterBar.querySelectorAll('.team-filter-chip').forEach(c => {
                    c.classList.toggle('active', c.dataset.filter === filter);
                });
                applyTeamFilters();
            }
        });
    }
}

function applyTeamFilters() {
    const cardsTarget = document.getElementById('teamCardsContainer');
    if (!cardsTarget) return;

    const cards = cardsTarget.querySelectorAll('.team-card');
    const sections = cardsTarget.querySelectorAll('.team-section');
    const search = teamFilterState.search;
    const filter = teamFilterState.filter;

    cards.forEach(card => {
        let visible = true;

        // Search filter
        if (search) {
            const name = (card.querySelector('.team-card-name')?.textContent || '').toLowerCase();
            const subtitle = (card.querySelector('.team-card-subtitle')?.textContent || '').toLowerCase();
            visible = name.includes(search) || subtitle.includes(search);
        }

        // Category filter
        if (visible && filter !== 'all') {
            const section = card.closest('.team-section');
            if (section) {
                const sectionType = section.classList.contains('attention') ? 'attention' :
                    section.classList.contains('active') ? 'working' :
                    section.classList.contains('idle') ? 'idle' : '';
                visible = sectionType === filter;
            }
        }

        card.style.display = visible ? '' : 'none';
    });

    // Hide sections where all cards are hidden
    sections.forEach(section => {
        const visibleCards = section.querySelectorAll('.team-card:not([style*="display: none"])');
        section.style.display = visibleCards.length > 0 ? '' : 'none';
    });
}

// ── Density toggle ───────────────────────────────────────────────────

function setupDensityToggle() {
    const toggle = document.getElementById('desktopDensityToggle');
    if (!toggle) return;

    toggle.addEventListener('click', (e) => {
        const btn = e.target.closest('.density-btn');
        if (!btn) return;
        const density = btn.dataset.density;
        if (density) {
            teamDensity = density;
            localStorage.setItem('mto_team_density', density);
            applyDensity(density);
        }
    });
}

export function applyDensity(density) {
    const app = document.querySelector('.app');
    if (!app) return;
    app.classList.remove('density-comfortable', 'density-compact', 'density-ultra');
    if (density !== 'compact') {
        app.classList.add('density-' + density);
    }
    // Update button active state
    const toggle = document.getElementById('desktopDensityToggle');
    if (toggle) {
        toggle.querySelectorAll('.density-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.density === density);
        });
    }
}

/** Get current team density setting */
export function getTeamDensity() {
    return teamDensity;
}

// ── Keyboard navigation (desktop) ───────────────────────────────────

function getVisibleTeamCards() {
    const container = document.getElementById('teamCardsContainer');
    if (!container) return [];
    return Array.from(container.querySelectorAll('.team-card:not([style*="display: none"])'));
}

function highlightSelectedAgent(cards) {
    if (!cards) cards = getVisibleTeamCards();
    // Remove all selections
    document.querySelectorAll('.team-card.agent-selected').forEach(c => c.classList.remove('agent-selected'));
    if (selectedAgentIndex >= 0 && selectedAgentIndex < cards.length) {
        cards[selectedAgentIndex].classList.add('agent-selected');
        cards[selectedAgentIndex].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
}

function restoreAgentSelection() {
    const cards = getVisibleTeamCards();
    if (selectedAgentIndex >= cards.length) selectedAgentIndex = cards.length - 1;
    highlightSelectedAgent(cards);
}

export function selectNextAgent() {
    const cards = getVisibleTeamCards();
    if (cards.length === 0) return;
    selectedAgentIndex = Math.min(selectedAgentIndex + 1, cards.length - 1);
    highlightSelectedAgent(cards);
}

export function selectPrevAgent() {
    const cards = getVisibleTeamCards();
    if (cards.length === 0) return;
    selectedAgentIndex = Math.max(selectedAgentIndex - 1, 0);
    highlightSelectedAgent(cards);
}

export function approveSelectedAgent() {
    const cards = getVisibleTeamCards();
    if (selectedAgentIndex < 0 || selectedAgentIndex >= cards.length) return;
    const card = cards[selectedAgentIndex];
    const allowBtn = card.querySelector('.team-card-action-btn.allow');
    if (allowBtn && !allowBtn.disabled) allowBtn.click();
}

export function denySelectedAgent() {
    const cards = getVisibleTeamCards();
    if (selectedAgentIndex < 0 || selectedAgentIndex >= cards.length) return;
    const card = cards[selectedAgentIndex];
    const denyBtn = card.querySelector('.team-card-action-btn.deny');
    if (denyBtn && !denyBtn.disabled) denyBtn.click();
}

export function openSelectedAgentTerminal() {
    const cards = getVisibleTeamCards();
    if (selectedAgentIndex < 0 || selectedAgentIndex >= cards.length) return;
    const card = cards[selectedAgentIndex];
    const menuBtn = card.querySelector('.team-card-menu-btn');
    if (menuBtn) menuBtn.click();
}

export function focusSearchInput() {
    const input = document.getElementById('teamSearchInput');
    if (input) input.focus();
}

// ── Dispatch event handlers ──────────────────────────────────────────

function setupDispatchHandlers() {
    const dispatchBtn = document.getElementById('dispatchBtn');
    const dispatchSelect = document.getElementById('dispatchPlanSelect');
    const leaderMsgInput = document.getElementById('leaderMessageInput');
    const leaderMsgBtn = document.getElementById('leaderMessageBtn');

    if (dispatchBtn) dispatchBtn.addEventListener('click', dispatchToLeader);
    if (dispatchSelect) dispatchSelect.addEventListener('change', () => {
        localStorage.setItem('mto_dispatch_plan', dispatchSelect.value);
        updateDispatchButtonState();
    });
    if (leaderMsgInput) {
        leaderMsgInput.addEventListener('keyup', (e) => {
            updateDispatchButtonState();
            if (e.key === 'Enter') sendLeaderMessage();
        });
    }
    if (leaderMsgBtn) leaderMsgBtn.addEventListener('click', sendLeaderMessage);
}

// ── Dismiss team ────────────────────────────────────────────────────

async function dismissTeam() {
    if (!confirm('Dismiss team? This closes all team windows.')) {
        return;
    }

    // Stop team card refresh BEFORE kill to prevent mid-teardown re-renders
    stopTeamCardRefresh();

    try {
        const resp = await fetch(`/api/team/kill?token=${ctx.token}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session: ctx.currentSession || '' }),
        });
        const data = await resp.json();

        // Clear team state atomically — one render, no cascading polls
        ctx.teamState = null;

        // Render empty state immediately
        const cardsTarget = document.getElementById('teamCardsContainer')
            || document.getElementById('teamView');
        if (cardsTarget) {
            cardsTarget.innerHTML = `
                <div class="team-no-team">
                    <div class="team-no-team-text">No team running</div>
                    <button class="no-team-launch-btn" id="noTeamLaunchBtn">Launch Team</button>
                </div>
            `;
            document.getElementById('noTeamLaunchBtn')?.addEventListener('click', showLaunchTeamModal);
        }

        // Hide system status strip
        const sysStrip = document.getElementById('systemStatusStrip');
        if (sysStrip) sysStrip.classList.add('hidden');

    } catch (err) {
        console.error('Dismiss team failed:', err);
        // Restart refresh on failure so team view doesn't go stale
        startTeamCardRefresh();
    }
}

// ── Public API ───────────────────────────────────────────────────────

/**
 * Activate team view: load plans and start refresh.
 * Called from switchToTeamView() in terminal.js.
 */
export function activateTeamView() {
    populateDispatchPlans();
    refreshTeamCards();
    startTeamCardRefresh();
}

/**
 * Bind team event listeners. Called once from DOMContentLoaded.
 * @param {Object} opts
 * @param {Function} opts.selectTarget - selectTarget(targetId) from terminal.js
 * @param {Function} opts.switchToView - switchToView(viewName) from terminal.js
 * @param {Function} opts.fetchWithTimeout - fetchWithTimeout(url, opts, ms) from terminal.js
 * @param {Function} opts.updateActionBar - updateActionBar() from terminal.js
 */
export function initTeam(opts = {}) {
    selectTargetCb = opts.selectTarget || null;
    switchToViewCb = opts.switchToView || null;
    fetchWithTimeoutCb = opts.fetchWithTimeout || null;
    updateActionBarCb = opts.updateActionBar || null;
    setupDispatchHandlers();
    setupDensityToggle();
    initTeamLauncher();
}
