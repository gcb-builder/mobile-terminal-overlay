/**
 * Shared application context — the single source of truth for cross-cutting state.
 * Feature modules import this and read from it.
 * Only terminal.js writes to it.
 */
const ctx = {
    // Auth & session
    token: null,
    clientId: null,
    currentSession: null,

    // Connection & terminal
    terminal: null,
    socket: null,

    // Targets
    activeTarget: null,
    targets: [],

    // Mode & view
    outputMode: 'tail',
    modeEpoch: 0,
    currentView: 'log',
    uiMode: 'mobile-single',

    // Team (read by MCP, Dispatch, Team modules)
    teamState: null,

    // Config
    config: null,

    // Shared utilities (set by terminal.js at init)
    apiFetch: null,
    showToast: null,
};

export default ctx;
