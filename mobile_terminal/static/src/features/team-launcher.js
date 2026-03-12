/**
 * Team Launcher — modal for creating team windows, starting Claude, assigning roles.
 * Separate module from team.js to avoid monolith growth.
 */

import ctx from '../context.js';

const VALID_ROLES = ['explorer', 'planner', 'executor', 'reviewer'];

let templatesCache = null;
let selectedTemplate = null;
let currentStep = 1;
let rosterAgents = [];

// ── Public API ─────────────────────────────────────────────────────

export function initTeamLauncher() {
    const closeBtn = document.getElementById('launchTeamClose');
    const nextBtn = document.getElementById('launchTeamNext');
    const backBtn = document.getElementById('launchTeamBack');
    const launchBtn = document.getElementById('launchTeamLaunch');
    const killBtn = document.getElementById('launchTeamKill');
    const addBtn = document.getElementById('launchTeamAddAgent');
    const planSelect = document.getElementById('launchTeamPlan');

    if (!closeBtn) return; // Modal not in DOM

    closeBtn.addEventListener('click', hideModal);
    nextBtn?.addEventListener('click', nextStep);
    backBtn?.addEventListener('click', prevStep);
    launchBtn?.addEventListener('click', executeLaunch);
    killBtn?.addEventListener('click', killTeam);
    addBtn?.addEventListener('click', addAgentRow);
    planSelect?.addEventListener('change', onPlanSelect);

    // Close on overlay click
    const modal = document.getElementById('launchTeamModal');
    modal?.addEventListener('click', (e) => {
        if (e.target === modal) hideModal();
    });
}

export function showLaunchTeamModal() {
    const modal = document.getElementById('launchTeamModal');
    if (!modal) return;

    // Reset state
    currentStep = 1;
    selectedTemplate = null;
    rosterAgents = [];
    setStatus('', '');

    // Show step 1, hide step 2
    document.getElementById('launchTeamStep1')?.classList.remove('hidden');
    document.getElementById('launchTeamStep2')?.classList.add('hidden');
    document.getElementById('launchTeamNext')?.classList.remove('hidden');
    document.getElementById('launchTeamLaunch')?.classList.add('hidden');
    document.getElementById('launchTeamBack')?.classList.add('hidden');
    document.getElementById('launchTeamKill')?.classList.add('hidden');

    // Clear goal
    const goalEl = document.getElementById('launchTeamGoal');
    if (goalEl) goalEl.value = '';

    modal.classList.remove('hidden');

    // Load data
    loadTemplates();
    loadPlans();
    checkExistingTeam();
}

// ── Template loading ───────────────────────────────────────────────

async function loadTemplates() {
    const grid = document.getElementById('launchTeamTemplates');
    if (!grid) return;

    if (!templatesCache) {
        try {
            const resp = await fetch(`/api/team/templates?token=${ctx.token}`);
            if (!resp.ok) throw new Error('Failed');
            templatesCache = await resp.json();
        } catch {
            grid.innerHTML = '<div class="launch-team-error">Failed to load templates</div>';
            return;
        }
    }

    grid.innerHTML = '';
    for (const [key, tmpl] of Object.entries(templatesCache)) {
        const card = document.createElement('div');
        card.className = 'launch-team-template-card';
        card.dataset.template = key;
        card.innerHTML = `
            <div class="template-card-label">${tmpl.label}</div>
            <div class="template-card-desc">${tmpl.description}</div>
            <div class="template-card-count">${tmpl.total_agents} agent${tmpl.total_agents !== 1 ? 's' : ''}</div>
        `;
        card.addEventListener('click', () => selectTemplate(key, tmpl));
        grid.appendChild(card);
    }
}

function selectTemplate(key, tmpl) {
    selectedTemplate = key;

    // Highlight selected card
    document.querySelectorAll('.launch-team-template-card').forEach(c => {
        c.classList.toggle('selected', c.dataset.template === key);
    });

    // Build roster from template
    rosterAgents = [];
    if (tmpl.requires_leader) {
        rosterAgents.push({ name: 'leader', role: 'planner' });
    }
    for (const a of tmpl.agents) {
        rosterAgents.push({ name: a.default_name, role: a.default_role });
    }
}

// ── Plan loading ───────────────────────────────────────────────────

async function loadPlans() {
    const select = document.getElementById('launchTeamPlan');
    if (!select) return;

    try {
        const resp = await fetch(`/api/plans?token=${ctx.token}`);
        if (!resp.ok) return;
        const data = await resp.json();
        const plans = data.plans || [];

        // Keep first option, clear rest
        select.innerHTML = '<option value="">No plan -- enter goal manually</option>';
        for (const p of plans) {
            const opt = document.createElement('option');
            opt.value = p.filename;
            opt.textContent = p.title || p.filename;
            select.appendChild(opt);
        }
    } catch {
        // Silently fail — plans are optional
    }
}

async function onPlanSelect() {
    const select = document.getElementById('launchTeamPlan');
    const goalEl = document.getElementById('launchTeamGoal');
    if (!select || !goalEl) return;

    const filename = select.value;
    if (!filename) return;

    // Try to read plan content to extract goal
    try {
        const resp = await fetch(`/api/plans/${encodeURIComponent(filename)}?token=${ctx.token}`);
        if (resp.ok) {
            const data = await resp.json();
            const content = data.content || '';
            // Extract first heading as goal
            for (const line of content.split('\n')) {
                if (line.startsWith('# ')) {
                    goalEl.value = line.slice(2).trim();
                    return;
                }
            }
        }
    } catch {
        // Fall through
    }
}

// ── Step navigation ────────────────────────────────────────────────

function nextStep() {
    if (!selectedTemplate) {
        setStatus('Select a template to continue', 'warning');
        return;
    }

    const goal = document.getElementById('launchTeamGoal')?.value.trim();
    if (!goal) {
        setStatus('Enter a goal for the team', 'warning');
        return;
    }

    setStatus('', '');
    currentStep = 2;

    document.getElementById('launchTeamStep1')?.classList.add('hidden');
    document.getElementById('launchTeamStep2')?.classList.remove('hidden');
    document.getElementById('launchTeamNext')?.classList.add('hidden');
    document.getElementById('launchTeamLaunch')?.classList.remove('hidden');
    document.getElementById('launchTeamBack')?.classList.remove('hidden');

    // Render summary
    const summary = document.getElementById('launchTeamSummary');
    if (summary) {
        const tmpl = templatesCache?.[selectedTemplate];
        summary.innerHTML = `<strong>${tmpl?.label || selectedTemplate}</strong> &mdash; ${goal}`;
    }

    renderRoster();
    updateAgentCount();
}

function prevStep() {
    currentStep = 1;
    setStatus('', '');

    document.getElementById('launchTeamStep1')?.classList.remove('hidden');
    document.getElementById('launchTeamStep2')?.classList.add('hidden');
    document.getElementById('launchTeamNext')?.classList.remove('hidden');
    document.getElementById('launchTeamLaunch')?.classList.add('hidden');
    document.getElementById('launchTeamBack')?.classList.add('hidden');
}

// ── Roster editing ─────────────────────────────────────────────────

function renderRoster() {
    const container = document.getElementById('launchTeamRoster');
    if (!container) return;
    container.innerHTML = '';

    rosterAgents.forEach((agent, idx) => {
        const row = document.createElement('div');
        row.className = 'launch-team-agent-row';

        const nameInput = document.createElement('input');
        nameInput.type = 'text';
        nameInput.className = 'launch-team-agent-name';
        nameInput.value = agent.name;
        nameInput.placeholder = 'agent name';
        nameInput.addEventListener('input', () => {
            rosterAgents[idx].name = nameInput.value.trim();
        });

        const roleSelect = document.createElement('select');
        roleSelect.className = 'launch-team-agent-role';
        for (const role of VALID_ROLES) {
            const opt = document.createElement('option');
            opt.value = role;
            opt.textContent = role;
            if (role === agent.role) opt.selected = true;
            roleSelect.appendChild(opt);
        }
        roleSelect.addEventListener('change', () => {
            rosterAgents[idx].role = roleSelect.value;
        });

        const removeBtn = document.createElement('button');
        removeBtn.className = 'launch-team-agent-remove';
        removeBtn.textContent = 'x';
        removeBtn.title = 'Remove agent';
        removeBtn.addEventListener('click', () => {
            rosterAgents.splice(idx, 1);
            renderRoster();
            updateAgentCount();
        });

        row.appendChild(nameInput);
        row.appendChild(roleSelect);
        row.appendChild(removeBtn);
        container.appendChild(row);
    });
}

function addAgentRow() {
    // Generate next available name
    const existing = new Set(rosterAgents.map(a => a.name));
    let n = 1;
    let name = 'a-new';
    while (existing.has(name)) {
        n++;
        name = `a-new${n}`;
    }
    rosterAgents.push({ name, role: 'executor' });
    renderRoster();
    updateAgentCount();
}

function updateAgentCount() {
    const el = document.getElementById('launchTeamCount');
    if (el) el.textContent = String(rosterAgents.length);
}

// ── Launch execution ───────────────────────────────────────────────

async function executeLaunch() {
    const goal = document.getElementById('launchTeamGoal')?.value.trim();
    const planFilename = document.getElementById('launchTeamPlan')?.value || '';
    const autoDispatch = document.getElementById('launchTeamAutoDispatch')?.checked ?? true;

    if (rosterAgents.length === 0) {
        setStatus('No agents in roster', 'error');
        return;
    }

    setStatus('Launching team...', 'info');
    const launchBtn = document.getElementById('launchTeamLaunch');
    if (launchBtn) launchBtn.disabled = true;

    try {
        const resp = await fetch(`/api/team/launch?token=${ctx.token}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                goal,
                template: selectedTemplate || '',
                plan_filename: planFilename,
                session: ctx.currentSession || '',
                agents: rosterAgents,
                auto_dispatch: autoDispatch,
                dry_run: false,
            }),
        });

        const data = await resp.json();

        if (data.success) {
            setStatus('Team launched successfully!', 'success');
            setTimeout(() => hideModal(), 1500);
        } else {
            // Show per-step errors
            const failedSteps = (data.steps || []).filter(s => !s.ok);
            const failedAgents = (data.agents || []).filter(a => !a.ok);

            let msg = 'Launch partially failed.';
            if (failedSteps.length) {
                msg += ' Steps: ' + failedSteps.map(s => `${s.name}: ${s.error || 'failed'}`).join('; ');
            }
            if (failedAgents.length) {
                msg += ' Agents: ' + failedAgents.map(a => `${a.name}: ${a.error || 'failed'}`).join('; ');
            }
            setStatus(msg, 'error');
        }
    } catch (err) {
        setStatus(`Launch error: ${err.message}`, 'error');
    } finally {
        if (launchBtn) launchBtn.disabled = false;
    }
}

// ── Kill team ──────────────────────────────────────────────────────

async function killTeam() {
    if (!confirm('Kill all team windows (leader + a-*)? This cannot be undone.')) {
        return;
    }

    setStatus('Killing team...', 'info');

    try {
        const resp = await fetch(`/api/team/kill?token=${ctx.token}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session: ctx.currentSession || '' }),
        });

        const data = await resp.json();

        if (data.success) {
            const killed = data.killed || [];
            setStatus(`Killed ${killed.length} window${killed.length !== 1 ? 's' : ''}: ${killed.join(', ')}`, 'success');
            document.getElementById('launchTeamKill')?.classList.add('hidden');
        } else {
            setStatus(`Kill errors: ${(data.errors || []).join('; ')}`, 'error');
        }
    } catch (err) {
        setStatus(`Kill error: ${err.message}`, 'error');
    }
}

// ── Check for existing team ────────────────────────────────────────

async function checkExistingTeam() {
    const killBtn = document.getElementById('launchTeamKill');
    if (!killBtn) return;

    try {
        const resp = await fetch(`/api/team/state?token=${ctx.token}`);
        if (resp.ok) {
            const data = await resp.json();
            if (data.has_team) {
                killBtn.classList.remove('hidden');
            } else {
                killBtn.classList.add('hidden');
            }
        }
    } catch {
        killBtn.classList.add('hidden');
    }
}

// ── Helpers ────────────────────────────────────────────────────────

function hideModal() {
    document.getElementById('launchTeamModal')?.classList.add('hidden');
}

function setStatus(msg, type) {
    const el = document.getElementById('launchTeamStatus');
    if (!el) return;

    if (!msg) {
        el.classList.add('hidden');
        el.textContent = '';
        el.className = 'launch-team-status hidden';
        return;
    }

    el.textContent = msg;
    el.className = `launch-team-status ${type || ''}`;
    el.classList.remove('hidden');
}
