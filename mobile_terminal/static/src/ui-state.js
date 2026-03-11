/**
 * Pure state derivation functions for the team/agent UI.
 * No DOM access, no global state — pure input-to-output computation.
 */

/**
 * Derive UI rendering state from a single agent's server data.
 * Pure function — no side effects, no DOM access.
 * @param {Object} agent - Agent data from /api/team/state
 * @returns {Object} UIState with section, urgency, badge, subtitle, etc.
 */
export function deriveUIState(agent) {
    const ui = {
        section: 'active',
        urgency: 5,
        badgeText: '',
        badgeColor: '',
        subtitle: '',
        showPermissionActions: false,
        permissionInfo: null,
        needsAttention: false,
        isRunning: false,
    };

    // SACRED RULE: "Needs Attention" is for actionable-by-human states ONLY.
    if (agent.waiting_reason === 'permission') {
        ui.section = 'attention';
        ui.urgency = 10;
        ui.badgeText = 'Permission Required';
        ui.badgeColor = 'danger';
        ui.subtitle = (
            (agent.permission?.tool || 'Tool') + ': ' +
            (agent.permission?.target || '')
        ).slice(0, 60);
        ui.showPermissionActions = true;
        ui.permissionInfo = agent.permission || null;
        ui.needsAttention = true;
    } else if (agent.waiting_reason === 'question') {
        ui.section = 'attention';
        ui.urgency = 8;
        ui.badgeText = 'Needs Input';
        ui.badgeColor = 'warning';
        ui.subtitle = agent.detail || 'Waiting for answer';
        ui.needsAttention = true;
    } else if (agent.phase === 'working' || agent.phase === 'running_task') {
        ui.section = 'active';
        ui.urgency = 6;
        ui.badgeText = agent.phase === 'running_task' ? 'Running Task' : 'Working';
        ui.badgeColor = agent.phase === 'running_task' ? 'purple' : 'blue';
        ui.subtitle = agent.detail || 'Working...';
        ui.isRunning = true;
    } else if (agent.phase === 'planning') {
        ui.section = 'active';
        ui.urgency = 5;
        ui.badgeText = 'Planning';
        ui.badgeColor = 'amber';
        ui.subtitle = agent.detail || 'Planning...';
        ui.isRunning = true;
    } else if (agent.phase === 'waiting' && !agent.waiting_reason) {
        // Generic "waiting" WITHOUT a specific reason = NOT attention.
        // Could be inter-agent wait, rate limit — NOT human-actionable.
        ui.section = 'active';
        ui.urgency = 4;
        ui.badgeText = 'Waiting';
        ui.badgeColor = 'gray';
        ui.subtitle = agent.detail || 'Waiting...';
        ui.isRunning = true;
    } else {
        ui.section = 'idle';
        ui.urgency = 3;
        ui.badgeText = 'Idle';
        ui.badgeColor = 'gray';
        ui.subtitle = '';
        ui.isRunning = false;
    }

    // Role microcopy
    const roleCopy = {
        explorer: 'Exploring',
        planner: 'Planning',
        executor: 'Executing',
        reviewer: 'Reviewing',
    };
    if (agent.team_role === 'leader' && ui.isRunning) {
        ui.subtitle = ui.subtitle || 'Orchestrating';
    } else if (agent.assigned_role && roleCopy[agent.assigned_role] && ui.isRunning) {
        ui.subtitle = ui.subtitle || roleCopy[agent.assigned_role];
    } else if (agent.team_role === 'agent' && ui.isRunning) {
        ui.subtitle = ui.subtitle || 'Executing task';
    }

    return ui;
}

/**
 * Derive system-level summary from all agents' UIStates.
 * @param {Array} agents - Raw agent data array
 * @param {Array} uiStates - Corresponding UIState array
 * @returns {Object} { icon, text, level, attentionCount, runningCount }
 */
export function deriveSystemSummary(agents, uiStates) {
    const attentionCount = uiStates.filter(u => u.needsAttention).length;
    const runningCount = uiStates.filter(u => u.isRunning).length;
    const idleCount = uiStates.filter(u => u.section === 'idle').length;

    let icon, text, level;

    if (attentionCount > 0) {
        icon = '\u{1F7E1}';
        level = 'warning';
        const parts = [];
        if (runningCount > 0) parts.push(runningCount + ' running');
        parts.push(attentionCount + ' needs approval');
        text = parts.join(' \u00B7 ');
    } else if (runningCount > 0) {
        icon = '\u{1F7E2}';
        level = 'ok';
        text = runningCount + ' running';
        if (idleCount > 0) text += ' \u00B7 ' + idleCount + ' idle';
    } else {
        icon = '\u26AA';
        level = 'idle';
        const names = agents.map(a => a.agent_name || a.agent_type || '?');
        text = 'Idle \u00B7 ' + names.join(', ');
    }

    return { icon, text, level, attentionCount, runningCount };
}
