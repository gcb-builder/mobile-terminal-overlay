/**
 * Mobile Terminal Overlay - Terminal Client
 *
 * Connects xterm.js to the WebSocket backend for tmux relay.
 */

// Get token from URL (may be null if --no-auth)
const urlParams = new URLSearchParams(window.location.search);
const token = urlParams.get('token') || '';

// State
let terminal = null;
let socket = null;
let isControlUnlocked = true;  // Controls always enabled (no lock)
let config = null;
let currentSession = null;

// Reconnection with exponential backoff
let reconnectDelay = 300;
const MAX_RECONNECT_DELAY = 10000;  // Cap at 10s (was 30s) - mobile needs fast recovery
const INITIAL_RECONNECT_DELAY = 300;  // Faster initial reconnect (was 500ms)
const MIN_CONNECTION_INTERVAL = 300;  // Minimum ms between connection attempts
const RECONNECT_OVERLAY_GRACE_MS = 2500;  // Don't show overlay for brief disconnects
let intentionalClose = false;  // Track intentional closes to skip auto-reconnect
let isConnecting = false;  // Prevent concurrent connection attempts
let reconnectTimer = null;  // Track pending reconnect
let reconnectOverlayTimer = null;  // Delayed overlay (grace period)
let lastConnectionAttempt = 0;  // Timestamp of last connection attempt
let reconnectAttempts = 0;  // Track consecutive failed reconnects
const SHOW_HARD_REFRESH_AFTER = 3;  // Show hard refresh button after N failures
let hasConnectedOnce = false;  // Track if we've ever connected (to detect reconnects)
let reconcileInFlight = false;  // Prevent overlapping reconciliations

// Hello handshake
const HELLO_TIMEOUT = 2000;  // Expect hello within 2s of connection
let helloTimer = null;
let helloReceived = false;

// Heartbeat for connection health monitoring
const HEARTBEAT_INTERVAL = 15000;  // Send ping every 15s (was 30s) - faster detection
const HEARTBEAT_TIMEOUT = 5000;    // Expect pong within 5s (was 10s)
let heartbeatTimer = null;
let heartbeatTimeoutTimer = null;
let lastPongTime = 0;
let lastDataReceived = 0;  // Track last data from server

// Activity-based keepalive - detect stale connections
const IDLE_THRESHOLD = 20000;  // If no data for 20s, send a ping to verify connection
let idleCheckTimer = null;

// Local command history (persisted to localStorage)
const MAX_HISTORY_SIZE = 100;
let commandHistory = JSON.parse(localStorage.getItem('terminalHistory') || '[]');
let historyIndex = -1;
let currentInput = '';

// DOM elements (initialized in DOMContentLoaded)
let terminalContainer, controlBarsContainer;
let collapseToggle, controlBar, roleBar, inputBar, viewBar;
let statusOverlay, statusText, repoBtn, repoLabel, repoDropdown;
let targetBtn, targetLabel, targetDropdown, targetLockBtn, targetLockIcon;
let cwdMismatchBanner, cwdMismatchText, cwdFixBtn, cwdDismissBtn;
let searchBtn, searchModal, searchInput, searchClose, searchResults;
let composeBtn, composeModal;
let composeInput, composeClose, composeClear, composePaste, composeInsert, composeRun;
let composeAttach, composeFileInput, composeThinkMode, composeAttachments;
let selectCopyBtn, drawersBtn, challengeBtn;
let challengeModal, challengeClose, challengeResult, challengeStatus, challengeRun;
let terminalViewBtn, transcriptViewBtn, transcriptContainer, transcriptContent, transcriptSearch, transcriptSearchCount;
let contextViewBtn, touchViewBtn, contextContainer, contextContent, touchContainer, touchContent;
let logView, logInput, logSend, logContent, refreshBtn;
let terminalView, terminalRefresh;

// Attachments state for compose modal
let pendingAttachments = [];

// Last activity timestamp tracking
let lastActivityTime = 0;
let lastActivityElement = null;
let activityUpdateTimer = null;

// Force scroll to bottom flag (used during resize)
let forceScrollToBottom = false;

// Queue state
let queueItems = [];
let queuePaused = false;

// Unified drawer state
let drawerOpen = false;

// Terminal busy state - when busy, input box shows Q instead of Enter
let terminalBusy = false;

// Tool collapse state for log view
let lastCollapseHash = '';
let expandedGroups = new Set();  // Stores group keys that user expanded
const scheduleIdle = window.requestIdleCallback || ((cb) => setTimeout(cb, 100));

// Scroll tracking for log view - only auto-scroll if user is at bottom
let userAtBottom = true;
let newContentIndicator = null;

// Super-collapse state for grouping many tool calls into single row
const SUPER_COLLAPSE_THRESHOLD = 6;  // Minimum tools to trigger super-collapse
let lastSuperCollapseHash = '';
let expandedSuperGroups = new Set();  // Stores group keys for expanded super-groups

// Preview mode state
let previewMode = null;          // null = live, string = snapshot_id
let previewSnapshot = null;      // Full snapshot data when in preview
let previewSnapshots = [];       // Cached list of snapshots
let previewFilter = 'all';       // Current filter: all, user_send, tool_call, claude_done, error

// Target selector state (for multi-pane sessions)
let targets = [];                // List of panes in current session
let activeTarget = null;         // Currently selected target pane ID (e.g., "0:0")
let expectedRepoPath = null;     // Expected repo path from config
let targetLocked = true;         // Lock mode (true = locked, false = follow active pane)

function initDOMElements() {
    terminalContainer = document.getElementById('terminal-container');
    controlBarsContainer = document.getElementById('controlBarsContainer');
    collapseToggle = document.getElementById('collapseToggle');
    controlBar = document.getElementById('controlBar');
    roleBar = document.getElementById('roleBar');
    inputBar = document.getElementById('inputBar');
    viewBar = document.getElementById('viewBar');
    statusOverlay = document.getElementById('statusOverlay');
    statusText = document.getElementById('statusText');
    repoBtn = document.getElementById('repoBtn');
    repoLabel = document.getElementById('repoLabel');
    repoDropdown = document.getElementById('repoDropdown');
    targetBtn = document.getElementById('targetBtn');
    targetLabel = document.getElementById('targetLabel');
    targetDropdown = document.getElementById('targetDropdown');
    targetLockBtn = document.getElementById('targetLockBtn');
    targetLockIcon = document.getElementById('targetLockIcon');
    cwdMismatchBanner = document.getElementById('cwdMismatchBanner');
    cwdMismatchText = document.getElementById('cwdMismatchText');
    cwdFixBtn = document.getElementById('cwdFixBtn');
    cwdDismissBtn = document.getElementById('cwdDismissBtn');
    searchBtn = document.getElementById('searchBtn');
    searchModal = document.getElementById('searchModal');
    searchInput = document.getElementById('searchInput');
    searchClose = document.getElementById('searchClose');
    searchResults = document.getElementById('searchResults');
    composeBtn = document.getElementById('composeBtn');
    composeModal = document.getElementById('composeModal');
    composeInput = document.getElementById('composeInput');
    composeClose = document.getElementById('composeClose');
    composeClear = document.getElementById('composeClear');
    composePaste = document.getElementById('composePaste');
    composeInsert = document.getElementById('composeInsert');
    composeRun = document.getElementById('composeRun');
    composeAttach = document.getElementById('composeAttach');
    composeFileInput = document.getElementById('composeFileInput');
    composeThinkMode = document.getElementById('composeThinkMode');
    composeAttachments = document.getElementById('composeAttachments');
    selectCopyBtn = document.getElementById('selectCopyBtn');
    drawersBtn = document.getElementById('drawersBtn');
    challengeBtn = document.getElementById('challengeBtn');
    challengeModal = document.getElementById('challengeModal');
    challengeClose = document.getElementById('challengeClose');
    challengeResult = document.getElementById('challengeResult');
    challengeStatus = document.getElementById('challengeStatus');
    challengeRun = document.getElementById('challengeRun');
    terminalViewBtn = document.getElementById('terminalViewBtn');
    transcriptViewBtn = document.getElementById('transcriptViewBtn');
    transcriptContainer = document.getElementById('transcriptContainer');
    transcriptContent = document.getElementById('transcriptContent');
    transcriptSearch = document.getElementById('transcriptSearch');
    transcriptSearchCount = document.getElementById('transcriptSearchCount');
    contextViewBtn = document.getElementById('contextViewBtn');
    touchViewBtn = document.getElementById('touchViewBtn');
    contextContainer = document.getElementById('contextContainer');
    contextContent = document.getElementById('contextContent');
    touchContainer = document.getElementById('touchContainer');
    touchContent = document.getElementById('touchContent');
    lastActivityElement = document.getElementById('lastActivity');
    logView = document.getElementById('logView');
    logInput = document.getElementById('logInput');
    logSend = document.getElementById('logSend');
    logContent = document.getElementById('logContent');
    refreshBtn = document.getElementById('refreshBtn');
    terminalView = document.getElementById('terminalView');
    terminalRefresh = document.getElementById('terminalRefresh');
    terminalBlock = document.getElementById('terminalBlock');
    activePromptContent = document.getElementById('activePromptContent');
    quickResponses = document.getElementById('quickResponses');
    // Queue elements (now inside unified drawer)
    queueList = document.getElementById('queueList');
    queueCount = document.getElementById('queueCount');
    queueBadge = document.getElementById('queueBadge');
    queueTabBadge = document.getElementById('queueTabBadge');
    queuePauseBtn = document.getElementById('queuePauseBtn');
    queueSendNext = document.getElementById('queueSendNext');
    queueFlush = document.getElementById('queueFlush');
}

// Additional DOM elements
let terminalBlock, activePromptContent, quickResponses;
let queueList, queueCount, queueBadge, queueTabBadge;
let queuePauseBtn, queueSendNext, queueFlush;

/**
 * Initialize the terminal
 * Uses fit addon to auto-size based on container width
 */
let fitAddon = null;

function initTerminal() {
    terminal = new Terminal({
        cursorBlink: false,
        cursorStyle: 'bar',
        cursorInactiveStyle: 'none',
        fontSize: 14,
        fontFamily: 'Menlo, Monaco, "Courier New", monospace',
        scrollback: 10000,
        smoothScrollDuration: 100,
        overviewRulerWidth: 0,
        theme: {
            background: '#0b0f14',
            foreground: '#e6edf3',
            cursor: '#0b0f14',  // Same as background = invisible
            cursorAccent: '#0b0f14',
            selection: 'rgba(88, 166, 255, 0.3)',
            black: '#0b0f14',
            red: '#f85149',
            green: '#3fb950',
            yellow: '#d29922',
            blue: '#58a6ff',
            magenta: '#bc8cff',
            cyan: '#39c5cf',
            white: '#e6edf3',
            brightBlack: '#6e7681',
            brightRed: '#ff7b72',
            brightGreen: '#56d364',
            brightYellow: '#e3b341',
            brightBlue: '#79c0ff',
            brightMagenta: '#d2a8ff',
            brightCyan: '#56d4dd',
            brightWhite: '#ffffff',
        },
        allowProposedApi: true,
    });

    // Fit addon to auto-size terminal to container
    fitAddon = new FitAddon.FitAddon();
    terminal.loadAddon(fitAddon);

    // Web links addon for clickable URLs
    const webLinksAddon = new WebLinksAddon.WebLinksAddon();
    terminal.loadAddon(webLinksAddon);

    terminal.open(terminalContainer);

    // Fit to container after opening
    fitAddon.fit();

    // Handle terminal input (only when unlocked)
    // Send as binary for faster processing (bypasses JSON parsing on server)
    const encoder = new TextEncoder();

    // Simple composition handling - no incremental sending to avoid doubles
    let isComposing = false;

    terminal.textarea.addEventListener('compositionstart', () => {
        isComposing = true;
    });

    terminal.textarea.addEventListener('compositionend', () => {
        isComposing = false;
    });

    // Reset composition state on blur (prevents stuck state after focus changes)
    terminal.textarea.addEventListener('blur', () => {
        isComposing = false;
    });

    // Also reset on focus to ensure clean state
    terminal.textarea.addEventListener('focus', () => {
        isComposing = false;
    });

    terminal.onData((data) => {
        if (isControlUnlocked && !isPreviewMode() && socket && socket.readyState === WebSocket.OPEN) {
            // Skip during active composition - wait for compositionend then onData fires
            if (isComposing) {
                return;
            }
            socket.send(encoder.encode(data));
        }
    });

    // Send fixed size once after short delay (no dynamic resizing)
    setTimeout(() => {
        sendResize();
    }, 100);
}

/**
 * Active Prompt - shows current screen state from tmux
 * Refreshes every 300ms to show what Claude is currently asking
 */
const ACTIVE_PROMPT_LINES = 15;  // Lines to capture from current screen
const ACTIVE_PROMPT_INTERVAL = 300;  // Refresh every 300ms
let activePromptTimer = null;

async function refreshActivePrompt() {
    if (!activePromptContent) return;

    // Don't refresh if user has text selected (would lose selection)
    const selection = window.getSelection();
    if (selection && selection.toString().length > 0) {
        return;
    }

    try {
        // Use capture endpoint with small line count (current screen, not scrollback)
        const response = await fetch(`/api/terminal/capture?token=${token}&lines=${ACTIVE_PROMPT_LINES}`);
        if (!response.ok) return;

        const data = await response.json();
        if (!data.content) return;

        // Strip ANSI codes and clean up clutter
        let content = stripAnsi(data.content);
        content = cleanTerminalOutput(content);

        // Update content (no auto-scroll - let user control scroll position)
        activePromptContent.textContent = content;

        // Try to extract and suggest command
        extractAndSuggestCommand(content);

        // Check if prompt is visible - if so, terminal is ready
        const extracted = extractPromptContent(content);
        if (extracted !== null) {
            setTerminalBusy(false);
        }

    } catch (error) {
        console.debug('Active prompt refresh failed:', error);
    }
}

function startActivePrompt() {
    stopActivePrompt();
    refreshActivePrompt();  // Initial fetch
    activePromptTimer = setInterval(refreshActivePrompt, ACTIVE_PROMPT_INTERVAL);
}

function stopActivePrompt() {
    if (activePromptTimer) {
        clearInterval(activePromptTimer);
        activePromptTimer = null;
    }
}

/**
 * Extract suggestion from terminal output and pre-fill input box
 */
let lastSuggestion = '';

function extractAndSuggestCommand(content) {
    if (!logInput) return;

    // Don't overwrite if user is typing
    if (document.activeElement === logInput && logInput.value.length > 0) {
        return;
    }

    const lines = content.split('\n');
    let suggestion = '';

    for (const line of lines) {
        const trimmed = line.trim();

        // Primary: Command prompt line with ❯ chevron (Claude Code's prompt)
        // Format: "❯ command text" or "❯ command text    ↵ send"
        if (/^❯\s+(.+)/.test(trimmed)) {
            const match = trimmed.match(/^❯\s+(.+)/);
            if (match) {
                // Remove trailing "↵ send" or similar UI elements
                suggestion = match[1].replace(/\s*↵\s*\w*\s*$/, '').trim();
                if (suggestion) break;
            }
        }

        // Numbered options: [1] Do something or 1) Do something
        if (/^\[?[1-3]\]?\)?\.?\s+(.+)/.test(trimmed)) {
            // Just show "1" for numbered options
            const numMatch = trimmed.match(/^\[?([1-3])/);
            if (numMatch) {
                suggestion = numMatch[1];
                break;
            }
        }

        // Yes/No prompts
        if (/\(y\/n\)/i.test(trimmed) || /\[yes\/no\]/i.test(trimmed)) {
            suggestion = 'y';
            break;
        }
    }

    // Only update if suggestion changed and input is empty
    if (suggestion && suggestion !== lastSuggestion && !logInput.value) {
        lastSuggestion = suggestion;
        logInput.value = suggestion;
        logInput.dataset.autoSuggestion = 'true';
        logInput.select();  // Select so user can easily replace
    } else if (!suggestion && lastSuggestion) {
        // Clear auto-suggestion if no suggestion found
        if (logInput.dataset.autoSuggestion === 'true') {
            logInput.value = '';
            lastSuggestion = '';
        }
    }
}

/**
 * Extract editable content from terminal prompt line.
 * Supports multiple prompt patterns: Claude Code, bash, zsh, python, node.
 * @param {string} content - Terminal capture (ANSI stripped)
 * @returns {string|null} - Content after prompt marker, or null if no prompt found
 */
function extractPromptContent(content) {
    const lines = content.split('\n');

    // Prompt patterns in priority order
    const patterns = [
        /^❯\s*(.*)$/,                                                  // Claude Code: ❯ cmd
        /^(?:\([^)]+\)\s*)?[\w.-]+@[\w.-]+[:\s][^$#]*[$#]\s*(.*)$/,   // bash: user@host:~$
        /^[$#]\s*(.*)$/,                                               // Simple: $ or #
        /^>>>\s*(.*)$/,                                                // Python REPL
        /^>\s+(.*)$/,                                                  // Node REPL
    ];

    // Search from bottom (most recent line first)
    for (let i = lines.length - 1; i >= 0; i--) {
        const line = lines[i].trim();
        if (!line) continue;

        for (const regex of patterns) {
            const match = line.match(regex);
            if (match) {
                // Remove trailing UI elements like "↵ send"
                return (match[1] || '').replace(/\s*↵\s*\w*\s*$/, '').trim();
            }
        }
    }
    return null;
}

/**
 * Sync terminal prompt content to input box
 */
async function syncPromptToInput() {
    if (!logInput) return;

    try {
        const response = await fetch(`/api/terminal/capture?token=${token}&lines=5`);
        if (!response.ok) return;

        const data = await response.json();
        const content = stripAnsi(data.content || '');
        const extracted = extractPromptContent(content);

        if (extracted !== null) {
            logInput.value = extracted;
            logInput.dataset.autoSuggestion = 'false';
            logInput.focus();
            logInput.setSelectionRange(logInput.value.length, logInput.value.length);
            // Prompt detected - terminal is ready
            setTerminalBusy(false);
        }
    } catch (e) {
        console.debug('Sync failed:', e);
    }
}

/**
 * Set terminal busy state and update send button accordingly
 */
function setTerminalBusy(busy) {
    terminalBusy = busy;
    updateSendButton();
}

/**
 * Update send button appearance based on terminal busy state
 * When idle: blue Enter button (⏎) - sends immediately
 * When busy: yellow Q button - queues for later
 */
function updateSendButton() {
    if (!logSend) return;

    if (terminalBusy) {
        logSend.textContent = 'Q';
        logSend.classList.add('queue-mode');
    } else {
        logSend.textContent = '⏎';
        logSend.classList.remove('queue-mode');
    }
}

/**
 * Send key to terminal and sync result to input box
 * @param {string} key - ANSI key code to send
 * @param {number} delay - ms to wait before capture (default 100)
 */
async function sendKeyWithSync(key, delay = 100) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;

    sendInput(key);
    await new Promise(r => setTimeout(r, delay));
    await syncPromptToInput();
}

// Client-side debounce for key sends
const KEY_DEBOUNCE_MS = 150;
let lastKeySendTime = 0;
let pendingKeyTimer = null;

/**
 * Debounced key send to prevent rapid-fire key presses.
 * Ctrl+C bypasses debounce for immediate interrupt.
 * @param {string} key - Key/ANSI code to send
 * @param {boolean} force - Skip debounce (for critical keys)
 */
function sendKeyDebounced(key, force = false) {
    const now = Date.now();
    const elapsed = now - lastKeySendTime;

    // Ctrl+C always immediate
    if (force || key === '\x03') {
        if (pendingKeyTimer) {
            clearTimeout(pendingKeyTimer);
            pendingKeyTimer = null;
        }
        sendInput(key);
        lastKeySendTime = now;
        return;
    }

    if (elapsed >= KEY_DEBOUNCE_MS) {
        sendInput(key);
        lastKeySendTime = now;
    } else {
        if (pendingKeyTimer) {
            clearTimeout(pendingKeyTimer);
        }
        pendingKeyTimer = setTimeout(() => {
            sendInput(key);
            lastKeySendTime = Date.now();
            pendingKeyTimer = null;
        }, KEY_DEBOUNCE_MS - elapsed);
    }
}

/**
 * Start heartbeat ping/pong for connection health monitoring
 */
function startHeartbeat() {
    stopHeartbeat();
    lastPongTime = Date.now();

    heartbeatTimer = setInterval(() => {
        if (socket && socket.readyState === WebSocket.OPEN) {
            // Send ping
            socket.send(JSON.stringify({ type: 'ping' }));

            // Set timeout for pong response
            heartbeatTimeoutTimer = setTimeout(() => {
                console.log('Heartbeat timeout - no pong received, reconnecting');
                // Connection is dead, force reconnect
                if (socket) {
                    socket.close();
                }
            }, HEARTBEAT_TIMEOUT);
        }
    }, HEARTBEAT_INTERVAL);
}

/**
 * Stop heartbeat timers
 */
function stopHeartbeat() {
    if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
    }
    if (heartbeatTimeoutTimer) {
        clearTimeout(heartbeatTimeoutTimer);
        heartbeatTimeoutTimer = null;
    }
    if (idleCheckTimer) {
        clearInterval(idleCheckTimer);
        idleCheckTimer = null;
    }
}

/**
 * Start idle connection check - detects stale connections faster
 * If no data received for IDLE_THRESHOLD, send a ping to verify connection
 */
function startIdleCheck() {
    if (idleCheckTimer) clearInterval(idleCheckTimer);
    lastDataReceived = Date.now();

    idleCheckTimer = setInterval(() => {
        if (!socket || socket.readyState !== WebSocket.OPEN) return;

        const idle = Date.now() - lastDataReceived;
        if (idle > IDLE_THRESHOLD) {
            // No data for a while - send a ping to verify connection is alive
            console.log(`Connection idle for ${idle}ms, sending keepalive ping`);
            socket.send(JSON.stringify({ type: 'ping' }));

            // If we don't get a pong soon, heartbeat timeout will catch it
            // But also set a shorter timeout for this specific check
            setTimeout(() => {
                const stillIdle = Date.now() - lastDataReceived;
                if (stillIdle > IDLE_THRESHOLD + HEARTBEAT_TIMEOUT) {
                    console.log('Connection appears stale, forcing reconnect');
                    if (socket) socket.close();
                }
            }, HEARTBEAT_TIMEOUT);
        }
    }, 5000);  // Check every 5s
}

/**
 * Handle pong response from server
 */
function handlePong() {
    lastPongTime = Date.now();
    lastDataReceived = Date.now();  // Pong counts as data received
    if (heartbeatTimeoutTimer) {
        clearTimeout(heartbeatTimeoutTimer);
        heartbeatTimeoutTimer = null;
    }
    updateConnectionIndicator('connected');
}

/**
 * Connection watchdog - catches stuck states
 */
let watchdogTimer = null;

function startConnectionWatchdog() {
    if (watchdogTimer) clearInterval(watchdogTimer);

    watchdogTimer = setInterval(() => {
        // Check if we're in a stuck state:
        // - Not connecting
        // - Socket is null or not OPEN
        // - No reconnect timer scheduled
        // - Overlay is hidden (user thinks they're connected)
        const isStuck = (
            !isConnecting &&
            (!socket || socket.readyState !== WebSocket.OPEN) &&
            !reconnectTimer &&
            statusOverlay.classList.contains('hidden')
        );

        if (isStuck) {
            console.warn('Watchdog: Connection stuck, forcing reconnect');
            // Aggressively reset ALL state
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
            isConnecting = false;
            intentionalClose = false;
            if (socket && socket.readyState !== WebSocket.CLOSED) {
                try { socket.close(); } catch (e) {}
            }
            socket = null;
            reconnectDelay = INITIAL_RECONNECT_DELAY;
            connect();
        }
    }, 10000);  // Check every 10s
}

/**
 * Update last activity timestamp when terminal receives data
 */
function updateLastActivity() {
    lastActivityTime = Date.now();
    updateActivityDisplay();
}

/**
 * Update the activity display with relative time
 */
function updateActivityDisplay() {
    if (!lastActivityElement || !lastActivityTime) return;

    const elapsed = Date.now() - lastActivityTime;
    let display;

    if (elapsed < 5000) {
        display = 'now';
    } else if (elapsed < 60000) {
        display = Math.floor(elapsed / 1000) + 's';
    } else if (elapsed < 3600000) {
        display = Math.floor(elapsed / 60000) + 'm';
    } else {
        display = Math.floor(elapsed / 3600000) + 'h';
    }

    lastActivityElement.textContent = display;
}

/**
 * Start periodic activity display updates
 */
function startActivityUpdates() {
    if (activityUpdateTimer) return;
    activityUpdateTimer = setInterval(updateActivityDisplay, 5000);
}

/**
 * Update connection status indicator in header
 */
function updateConnectionIndicator(status) {
    const indicator = document.getElementById('connectionIndicator');
    if (!indicator) return;

    indicator.className = 'connection-indicator ' + status;
    indicator.title = status === 'connected' ? 'Connected' : 'Disconnected';
}

/**
 * Manual reconnect triggered by user
 */
function manualReconnect() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    reconnectDelay = INITIAL_RECONNECT_DELAY;
    connect();
}

/**
 * Hard refresh - clears SW cache and reloads
 */
async function hardRefresh() {
    try {
        // Unregister service worker
        if ('serviceWorker' in navigator) {
            const registrations = await navigator.serviceWorker.getRegistrations();
            for (const reg of registrations) {
                await reg.unregister();
            }
        }
        // Clear caches
        if ('caches' in window) {
            const cacheNames = await caches.keys();
            for (const name of cacheNames) {
                await caches.delete(name);
            }
        }
        // Force reload bypassing cache
        location.reload(true);
    } catch (e) {
        console.error('Hard refresh failed:', e);
        location.reload(true);
    }
}

/**
 * Connect to WebSocket
 */
function connect() {
    // Prevent concurrent connection attempts
    if (isConnecting) {
        console.log('Connection already in progress, skipping');
        return;
    }

    // Enforce minimum interval between connection attempts
    const now = Date.now();
    const elapsed = now - lastConnectionAttempt;
    if (elapsed < MIN_CONNECTION_INTERVAL) {
        console.log(`Throttling connection, waiting ${MIN_CONNECTION_INTERVAL - elapsed}ms`);
        if (!reconnectTimer) {
            reconnectTimer = setTimeout(connect, MIN_CONNECTION_INTERVAL - elapsed);
        }
        return;
    }
    lastConnectionAttempt = now;

    // Clear any pending reconnect timer
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }

    // Close existing socket if any (any state except CLOSED)
    if (socket && socket.readyState !== WebSocket.CLOSED) {
        intentionalClose = true;
        socket.close();
        socket = null;
    }

    isConnecting = true;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/terminal?token=${token}`;

    statusText.textContent = 'Connecting...';
    statusOverlay.classList.remove('hidden');

    // Hide reconnect button while connecting
    const reconnectBtn = document.getElementById('reconnectBtn');
    if (reconnectBtn) reconnectBtn.classList.add('hidden');

    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
        console.log('WebSocket connected');
        isConnecting = false;

        // Cancel overlay timer and hide overlay (may not have shown yet due to grace period)
        if (reconnectOverlayTimer) {
            clearTimeout(reconnectOverlayTimer);
            reconnectOverlayTimer = null;
        }
        statusOverlay.classList.add('hidden');

        // Reset reconnect state on successful connection
        reconnectDelay = INITIAL_RECONNECT_DELAY;
        reconnectAttempts = 0;
        helloReceived = false;
        const hardRefreshBtn = document.getElementById('hardRefreshBtn');
        if (hardRefreshBtn) hardRefreshBtn.classList.add('hidden');

        // Start hello timeout - expect server hello within 2s
        if (helloTimer) clearTimeout(helloTimer);
        helloTimer = setTimeout(() => {
            if (!helloReceived) {
                console.warn('Hello timeout - server did not send hello, forcing reconnect');
                if (socket) socket.close();
            }
        }, HELLO_TIMEOUT);

        // Fit terminal to container (don't clear buffer - server will replay history)
        if (terminal && fitAddon) {
            fitAddon.fit();
        }

        sendResize();
        startHeartbeat();
        startIdleCheck();  // Start idle connection monitoring
        startConnectionWatchdog();  // Catch stuck states
        updateConnectionIndicator('connected');

        // Reconcile queue and log on reconnect (not initial connect)
        const isReconnect = hasConnectedOnce;
        hasConnectedOnce = true;

        if (isReconnect && !reconcileInFlight) {
            reconcileInFlight = true;
            (async () => {
                try {
                    console.log('Reconnect detected, syncing queue and log...');
                    await reconcileQueue();
                    await refreshLogContent();
                } catch (e) {
                    console.warn('Post-reconnect sync failed:', e);
                } finally {
                    reconcileInFlight = false;
                }
            })();
        }
    };

    socket.onmessage = (event) => {
        // Track all incoming data for idle detection
        lastDataReceived = Date.now();

        if (event.data instanceof Blob) {
            event.data.arrayBuffer().then((buffer) => {
                terminal.write(new Uint8Array(buffer));
                updateLastActivity();
            });
        } else {
            // Check for JSON messages (pong, queue updates, server ping, hello, etc.)
            if (event.data.startsWith('{')) {
                try {
                    const msg = JSON.parse(event.data);

                    // Server hello handshake - confirms connection is fully established
                    if (msg.type === 'hello') {
                        console.log('Received hello:', msg);
                        helloReceived = true;
                        if (helloTimer) {
                            clearTimeout(helloTimer);
                            helloTimer = null;
                        }
                        return;
                    }

                    if (msg.type === 'pong') {
                        handlePong();
                        return;
                    }
                    // Server-initiated ping - respond with pong to keep connection alive
                    if (msg.type === 'server_ping') {
                        socket.send(JSON.stringify({ type: 'pong' }));
                        return;
                    }
                    // Handle queue messages
                    if (msg.type === 'queue_update' || msg.type === 'queue_sent' || msg.type === 'queue_state') {
                        handleQueueMessage(msg);
                        return;
                    }
                } catch (e) {
                    // Not JSON, treat as terminal data
                }
            }
            terminal.write(event.data);
            updateLastActivity();
        }
    };

    socket.onclose = (event) => {
        console.log('WebSocket closed:', event.code, event.reason);
        isConnecting = false;
        stopHeartbeat();
        updateConnectionIndicator('disconnected');

        // Clear hello timer
        if (helloTimer) {
            clearTimeout(helloTimer);
            helloTimer = null;
        }

        const reconnectBtn = document.getElementById('reconnectBtn');

        // Handle special close codes
        if (intentionalClose) {
            // Client-initiated close (e.g., repo switch) - don't auto-reconnect
            // but repo switch handles its own reconnect
            intentionalClose = false;
            return;
        }

        if (event.code === 4002) {
            // Replaced by another connection - show manual reconnect option
            console.log('Connection replaced by another client');
            statusText.textContent = 'Replaced by another connection. Tap to reconnect.';
            statusOverlay.classList.remove('hidden');
            if (reconnectBtn) reconnectBtn.classList.remove('hidden');
            return;
        }

        if (event.code === 4003) {
            // Repo switch in progress - switch-repo handles reconnect
            // But add fallback in case switch-repo fails
            statusText.textContent = 'Switching repository...';
            statusOverlay.classList.remove('hidden');
            // Fallback reconnect after 5s if switch-repo doesn't reconnect
            reconnectTimer = setTimeout(() => {
                if (!socket || socket.readyState !== WebSocket.OPEN) {
                    console.log('Repo switch fallback: reconnecting');
                    reconnectDelay = INITIAL_RECONNECT_DELAY;
                    connect();
                }
            }, 5000);
            return;
        }

        // Rate limited (4004) - wait longer before retry
        if (event.code === 4004) {
            console.log('Rate limited by server, waiting before retry');
            reconnectDelay = Math.max(reconnectDelay, 2000);
        }

        // PTY died (4500) - terminal process died, will be recreated on reconnect
        if (event.code === 4500) {
            console.warn('PTY died - terminal process ended');
            statusText.textContent = 'Terminal process ended. Reconnecting...';
            statusOverlay.classList.remove('hidden');
            // Reconnect immediately - server will recreate PTY
            reconnectDelay = INITIAL_RECONNECT_DELAY;
        }

        // Track reconnect attempts
        reconnectAttempts++;

        // Clear any existing overlay timer before scheduling new one
        if (reconnectOverlayTimer) {
            clearTimeout(reconnectOverlayTimer);
            reconnectOverlayTimer = null;
        }

        // Grace period: delay showing overlay for brief disconnects
        // This prevents flicker during background/foreground transitions
        reconnectOverlayTimer = setTimeout(() => {
            // Guard: only show if still disconnected
            if (!socket || socket.readyState !== WebSocket.OPEN) {
                statusText.textContent = `Reconnecting...`;
                statusOverlay.classList.remove('hidden');
                if (reconnectBtn) reconnectBtn.classList.remove('hidden');

                // Show hard refresh button after multiple failures
                const hardRefreshBtn = document.getElementById('hardRefreshBtn');
                if (hardRefreshBtn && reconnectAttempts >= SHOW_HARD_REFRESH_AFTER) {
                    hardRefreshBtn.classList.remove('hidden');
                }
            }
            reconnectOverlayTimer = null;
        }, RECONNECT_OVERLAY_GRACE_MS);

        // Reconnect with exponential backoff (starts immediately, overlay is delayed)
        reconnectTimer = setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
    };

    socket.onerror = (error) => {
        console.error('WebSocket error:', error);
        isConnecting = false;
        statusText.textContent = 'Connection error';
    };
}

/**
 * Send terminal dimensions to server
 */
function sendResize() {
    if (terminal && fitAddon) {
        fitAddon.fit();
    }
    if (socket && socket.readyState === WebSocket.OPEN && terminal) {
        socket.send(JSON.stringify({
            type: 'resize',
            cols: terminal.cols,
            rows: terminal.rows,
        }));
    }
}

/**
 * Send input to terminal (binary format, same as main terminal)
 */
const inputEncoder = new TextEncoder();

function sendInput(data) {
    if (isPreviewMode()) return;  // No input in preview mode
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(inputEncoder.encode(data));
    }
}

/**
 * Toggle control lock
 */
// Lock functionality removed - controls always enabled

/**
 * Toggle control bars collapse state
 */
function toggleControlBarsCollapse() {
    if (!controlBarsContainer || !collapseToggle) return;

    const isCollapsed = controlBarsContainer.classList.toggle('collapsed');
    // Update button icon state
    collapseToggle.classList.toggle('collapsed', isCollapsed);
    // Also collapse/expand the view bar (Select, Stop, Challenge, Compose)
    if (viewBar) {
        viewBar.classList.toggle('collapsed', isCollapsed);
    }

    // When expanding in log or terminal view, also remove 'hidden' to ensure visibility
    // (hidden might be present if view was switched while collapsed)
    if (!isCollapsed && (currentView === 'log' || currentView === 'terminal')) {
        controlBarsContainer.classList.remove('hidden');
        if (viewBar) {
            viewBar.classList.remove('hidden');
        }
    }

    // Don't resize - keeps terminal stable, prevents tmux reflow/corruption
}

/**
 * Setup terminal focus handling
 */
function setupTerminalFocus() {
    // Disable mobile IME composition - send characters directly without preview
    terminal.textarea.setAttribute('autocomplete', 'off');
    terminal.textarea.setAttribute('autocorrect', 'off');
    terminal.textarea.setAttribute('autocapitalize', 'off');
    terminal.textarea.setAttribute('spellcheck', 'false');
    terminal.textarea.setAttribute('inputmode', 'text');

    // Tap terminal to focus and show keyboard
    terminalContainer.addEventListener('click', () => {
        if (isControlUnlocked) {
            terminal.focus();
        }
    });
}

/**
 * Load configuration
 */
async function loadConfig() {
    try {
        const response = await fetch(`/config?token=${token}`);
        if (!response.ok) {
            console.error('Failed to load config');
            return;
        }
        config = await response.json();
        populateUI();
    } catch (error) {
        console.error('Error loading config:', error);
    }
}

/**
 * Load current session from server
 */
async function loadCurrentSession() {
    try {
        const response = await fetch(`/current-session?token=${token}`);
        if (response.ok) {
            const data = await response.json();
            currentSession = data.session;
        }
    } catch (error) {
        console.error('Error loading current session:', error);
    }
}

/**
 * Populate UI from config
 */
function populateUI() {
    if (!config) return;

    // Populate role buttons - send directly to terminal
    if (config.role_prefixes && config.role_prefixes.length > 0) {
        roleBar.innerHTML = '';
        config.role_prefixes.forEach((role) => {
            const btn = document.createElement('button');
            btn.className = 'role-btn';
            btn.textContent = role.label;
            btn.addEventListener('click', () => {
                if (isControlUnlocked) {
                    // Ensure terminal is focused/active before sending input
                    if (terminal) terminal.focus();
                    sendInput(role.insert);
                }
            });
            roleBar.appendChild(btn);
        });
    }

    // Populate repo dropdown
    populateRepoDropdown();

    // Load target selector (for multi-pane sessions)
    loadTargets();
}

/**
 * Populate repo dropdown from config
 */
function populateRepoDropdown() {
    if (!config || !config.repos || config.repos.length === 0) {
        // No repos configured, hide the dropdown arrow
        repoBtn.querySelector('.repo-arrow').style.display = 'none';
        repoLabel.textContent = config?.session_name || 'Terminal';
        return;
    }

    // Show dropdown arrow
    repoBtn.querySelector('.repo-arrow').style.display = '';

    // Update label to show current repo
    const currentRepo = config.repos.find(r => r.session === currentSession);
    if (currentRepo) {
        repoLabel.textContent = currentRepo.label;
    } else {
        repoLabel.textContent = config.session_name || 'Terminal';
    }

    // Populate dropdown options
    repoDropdown.innerHTML = '';

    // Add default session option if not in repos list
    const defaultInRepos = config.repos.some(r => r.session === config.session_name);
    if (!defaultInRepos) {
        const defaultOpt = document.createElement('button');
        defaultOpt.className = 'repo-option' + (currentSession === config.session_name ? ' active' : '');
        defaultOpt.innerHTML = `<span>${config.session_name}</span><span class="repo-path">Default</span>`;
        defaultOpt.addEventListener('click', () => switchRepo(config.session_name));
        repoDropdown.appendChild(defaultOpt);
    }

    // Add configured repos
    config.repos.forEach((repo) => {
        const opt = document.createElement('button');
        opt.className = 'repo-option' + (currentSession === repo.session ? ' active' : '');
        opt.innerHTML = `<span>${repo.label}</span><span class="repo-path">${repo.path}</span>`;
        opt.addEventListener('click', () => switchRepo(repo.session));
        repoDropdown.appendChild(opt);
    });
}

/**
 * Switch to a different repo/session
 */
async function switchRepo(session) {
    if (session === currentSession) {
        repoDropdown.classList.add('hidden');
        return;
    }

    statusText.textContent = 'Switching...';
    statusOverlay.classList.remove('hidden');
    repoDropdown.classList.add('hidden');

    // Set intentional close BEFORE API call - server will close WebSocket
    intentionalClose = true;
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    reconnectDelay = INITIAL_RECONNECT_DELAY;

    try {
        const response = await fetch(`/switch-repo?session=${encodeURIComponent(session)}&token=${token}`, {
            method: 'POST',
        });

        if (!response.ok) {
            throw new Error('Failed to switch repo');
        }

        currentSession = session;

        // Clear target selection (pane IDs are session-specific)
        activeTarget = null;
        localStorage.removeItem('mto_active_target');
        targetBtn.classList.add('hidden');
        targetLockBtn.classList.add('hidden');
        cwdMismatchBanner.classList.add('hidden');

        // Update UI
        const currentRepo = config.repos.find(r => r.session === session);
        if (currentRepo) {
            repoLabel.textContent = currentRepo.label;
        } else {
            repoLabel.textContent = session;
        }

        // Re-render dropdown to update active state
        populateRepoDropdown();

        // Clear terminal and log content immediately (don't show old session's output)
        if (term) {
            term.clear();
        }
        if (logContent) {
            logContent.innerHTML = '<div class="loading">Switching session...</div>';
        }
        // Reset log state to force fresh load
        logLoaded = false;
        lastLogModified = 0;

        // Server already closed WebSocket, reconnect after cleanup delay
        setTimeout(() => {
            connect();
            // Refresh log, targets, and queue after connection established
            setTimeout(async () => {
                await loadTargets();
                refreshLogContent();
                loadContextFile();
                await reconcileQueue();  // Reconcile queue for new session
            }, 500);
        }, 1000);

    } catch (error) {
        console.error('Error switching repo:', error);
        intentionalClose = false;  // Reset on error
        statusText.textContent = 'Switch failed';
        setTimeout(() => {
            statusOverlay.classList.add('hidden');
        }, 2000);
    }
}

/**
 * Toggle repo dropdown visibility
 */
function toggleRepoDropdown() {
    if (!config || !config.repos || config.repos.length === 0) {
        return; // No repos to show
    }
    repoDropdown.classList.toggle('hidden');
}

/**
 * Setup repo dropdown event listeners
 */
function setupRepoDropdown() {
    // Toggle dropdown on button click
    repoBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        // Re-populate dropdown every time to ensure correct active state
        populateRepoDropdown();
        toggleRepoDropdown();
    });

    // Close dropdown when clicking outside
    document.addEventListener('click', (e) => {
        if (!repoDropdown.contains(e.target) && e.target !== repoBtn) {
            repoDropdown.classList.add('hidden');
        }
    });
}

/**
 * Target Selector Functions (for multi-pane sessions)
 */

/**
 * Load available targets (panes) in current session
 */
async function loadTargets() {
    try {
        const response = await fetch(`/api/targets?token=${token}`);
        if (!response.ok) return;

        const data = await response.json();
        targets = data.targets || [];
        activeTarget = data.active;

        // Get expected repo path from current repo config
        if (config && config.repos) {
            const currentRepo = config.repos.find(r => r.session === currentSession);
            expectedRepoPath = currentRepo ? currentRepo.path : null;
        }

        // Show target button and lock button only if multiple panes exist
        if (targets.length > 1) {
            targetBtn.classList.remove('hidden');
            targetLockBtn.classList.remove('hidden');
            updateTargetLabel();
        } else {
            targetBtn.classList.add('hidden');
            targetLockBtn.classList.add('hidden');
        }

        // Check if locked target still exists
        if (targetLocked && activeTarget && !data.active_exists) {
            showTargetMissingWarning();
        }

        // Check for cwd mismatch
        checkCwdMismatch(data.resolution);

        // Check for multi-project session without explicit target
        checkMultiProjectWarning(data);

        renderTargetDropdown();
    } catch (error) {
        console.error('Error loading targets:', error);
    }
}

/**
 * Show warning when locked target pane no longer exists
 */
function showTargetMissingWarning() {
    cwdMismatchText.textContent = 'Target pane no longer exists. Please select a new target.';
    cwdMismatchBanner.classList.remove('hidden');
    cwdFixBtn.style.display = 'none';  // Hide cd button, not relevant here

    // Clear the invalid target
    activeTarget = null;
    localStorage.removeItem('mto_active_target');
}

/**
 * Check if pane cwd matches expected repo and show warning if not
 */
function checkCwdMismatch(resolution) {
    if (!resolution || !expectedRepoPath) {
        cwdMismatchBanner.classList.add('hidden');
        return;
    }

    // Restore cd button visibility (may have been hidden by showTargetMissingWarning)
    cwdFixBtn.style.display = '';

    const currentCwd = resolution.path;
    // Check if cwd is inside expected repo path
    if (currentCwd && !currentCwd.startsWith(expectedRepoPath)) {
        const shortExpected = expectedRepoPath.replace(/^\/home\/[^/]+/, '~');
        cwdMismatchText.textContent = `Target pane is not inside ${shortExpected}. Open that repo or cd into it.`;
        cwdMismatchBanner.classList.remove('hidden');
    } else {
        cwdMismatchBanner.classList.add('hidden');
    }
}

/**
 * Check for multi-project session without explicit target selection
 * Shows warning if session contains multiple projects but no target is pinned
 */
function checkMultiProjectWarning(data) {
    const multiProjectBanner = document.getElementById('multiProjectBanner');
    if (!multiProjectBanner) return;

    // Show warning if: multi-project session AND (no explicit target OR using fallback)
    const needsWarning = data.multi_project &&
        (!data.active || data.resolution?.is_fallback);

    if (needsWarning) {
        const count = data.unique_projects || 'multiple';
        multiProjectBanner.querySelector('.multi-project-text').textContent =
            `Session has ${count} projects. Select a target to avoid mistakes.`;
        multiProjectBanner.classList.remove('hidden');
    } else {
        multiProjectBanner.classList.add('hidden');
    }
}

/**
 * Update target label in header
 */
function updateTargetLabel() {
    if (activeTarget) {
        targetLabel.textContent = activeTarget;
    } else if (targets.length > 0) {
        targetLabel.textContent = targets[0].id;
    }
}

/**
 * Render target dropdown options
 */
function renderTargetDropdown() {
    targetDropdown.innerHTML = '';

    if (targets.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'target-option';
        empty.textContent = 'No panes found';
        targetDropdown.appendChild(empty);
        return;
    }

    targets.forEach((target) => {
        const opt = document.createElement('button');
        const isActive = target.id === activeTarget;
        opt.className = 'target-option' + (isActive ? ' active' : '');

        const shortPath = target.cwd.replace(/^\/home\/[^/]+/, '~');

        opt.innerHTML = `
            <span class="target-project">${target.project}</span>
            <span class="target-pane-info">${target.window_name || ''} • ${target.pane_id}</span>
            <span class="target-path">${shortPath}</span>
        `;
        opt.addEventListener('click', () => selectTarget(target.id));
        targetDropdown.appendChild(opt);
    });
}

/**
 * Select a target pane
 */
async function selectTarget(targetId) {
    targetDropdown.classList.add('hidden');

    if (targetId === activeTarget) return;

    try {
        const response = await fetch(`/api/target/select?target_id=${encodeURIComponent(targetId)}&token=${token}`, {
            method: 'POST',
        });

        if (response.status === 409) {
            // Target no longer exists
            showToast('Target pane not found', 'error');
            loadTargets();
            return;
        }

        if (!response.ok) throw new Error('Failed to select target');

        activeTarget = targetId;
        localStorage.setItem('mto_active_target', targetId);
        updateTargetLabel();
        renderTargetDropdown();

        // Reload targets to check cwd mismatch
        await loadTargets();

        // Refresh context-dependent views
        refreshLogContent();
        loadContextFile();

    } catch (error) {
        console.error('Error selecting target:', error);
        showToast('Failed to select target', 'error');
    }
}

/**
 * CD to expected repo root in the target pane
 */
async function cdToRepoRoot() {
    if (!expectedRepoPath) return;

    // Confirm before sending cd command
    if (!confirm(`Send "cd ${expectedRepoPath}" to terminal?`)) return;

    try {
        // Send cd command via WebSocket
        const cdCommand = `cd ${expectedRepoPath}\r`;
        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(cdCommand);
            showToast('Sent cd command', 'success');
            // Reload targets after a delay to check new cwd
            setTimeout(loadTargets, 1000);
        }
    } catch (error) {
        console.error('Error sending cd command:', error);
    }
}

/**
 * Setup target selector event listeners
 */
function setupTargetSelector() {
    // Toggle dropdown on button click
    targetBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        targetDropdown.classList.toggle('hidden');
        // Refresh targets when opening
        if (!targetDropdown.classList.contains('hidden')) {
            loadTargets();
        }
    });

    // Close dropdown when clicking outside
    document.addEventListener('click', (e) => {
        if (!targetDropdown.contains(e.target) && e.target !== targetBtn) {
            targetDropdown.classList.add('hidden');
        }
    });

    // Lock toggle button
    targetLockBtn.addEventListener('click', toggleTargetLock);

    // CWD mismatch banner buttons
    cwdFixBtn.addEventListener('click', cdToRepoRoot);
    cwdDismissBtn.addEventListener('click', () => {
        cwdMismatchBanner.classList.add('hidden');
    });

    // Multi-project banner select button
    const multiProjectSelectBtn = document.getElementById('multiProjectSelectBtn');
    if (multiProjectSelectBtn) {
        multiProjectSelectBtn.addEventListener('click', () => {
            targetDropdown.classList.remove('hidden');
            loadTargets();
        });
    }

    // Restore saved state on startup
    const savedTarget = localStorage.getItem('mto_active_target');
    const savedLocked = localStorage.getItem('mto_target_locked');

    // Default to locked if not set
    targetLocked = savedLocked !== 'false';
    updateLockUI();

    if (savedTarget) {
        selectTarget(savedTarget);
    }
}

/**
 * Get target params for API calls (session + pane_id)
 */
function getTargetParams() {
    const params = new URLSearchParams();
    if (currentSession) params.append('session', currentSession);
    if (activeTarget) params.append('pane_id', activeTarget);
    return params.toString();
}

/**
 * Toggle target lock mode
 */
function toggleTargetLock() {
    targetLocked = !targetLocked;
    localStorage.setItem('mto_target_locked', targetLocked);
    updateLockUI();

    if (targetLocked) {
        showToast('Target locked - stays on selected pane', 'success');
    } else {
        showToast('Follow mode - follows tmux active pane', 'warning');
    }
}

/**
 * Update lock button UI
 */
function updateLockUI() {
    if (targetLocked) {
        targetLockIcon.textContent = '🔒';
        targetLockBtn.classList.remove('unlocked');
        targetLockBtn.title = 'Target locked (click to follow active)';
    } else {
        targetLockIcon.textContent = '👁';
        targetLockBtn.classList.add('unlocked');
        targetLockBtn.title = 'Following active pane (click to lock)';
    }
}

/**
 * File Search Functions
 */
let searchDebounceTimer = null;

function openSearchModal() {
    searchModal.classList.remove('hidden');
    searchInput.value = '';
    searchResults.innerHTML = '<div class="search-empty">Type to search files...</div>';
    setTimeout(() => searchInput.focus(), 100);
}

function closeSearchModal() {
    searchModal.classList.add('hidden');
    searchInput.blur();
}

async function performSearch(query) {
    if (!query || query.length < 1) {
        searchResults.innerHTML = '<div class="search-empty">Type to search files...</div>';
        return;
    }

    searchResults.innerHTML = '<div class="search-empty">Searching...</div>';

    try {
        const response = await fetch(`/api/files/search?q=${encodeURIComponent(query)}&token=${token}`);
        if (!response.ok) {
            throw new Error('Search failed');
        }

        const data = await response.json();

        if (!data.files || data.files.length === 0) {
            searchResults.innerHTML = '<div class="search-empty">No files found</div>';
            return;
        }

        // Render results
        searchResults.innerHTML = '';
        data.files.forEach((filePath) => {
            const btn = document.createElement('button');
            btn.className = 'search-result';

            // Split into path and filename for highlighting
            const lastSlash = filePath.lastIndexOf('/');
            const fileName = lastSlash >= 0 ? filePath.slice(lastSlash + 1) : filePath;
            const dirPath = lastSlash >= 0 ? filePath.slice(0, lastSlash + 1) : '';

            btn.innerHTML = `<span class="file-path">${dirPath}</span><span class="file-name">${fileName}</span>`;

            btn.addEventListener('click', () => {
                insertFilePath(filePath);
            });

            searchResults.appendChild(btn);
        });

    } catch (error) {
        console.error('Search error:', error);
        searchResults.innerHTML = '<div class="search-empty">Search failed</div>';
    }
}

function insertFilePath(filePath) {
    closeSearchModal();

    // Insert the file path into the terminal
    if (isControlUnlocked && socket && socket.readyState === WebSocket.OPEN) {
        sendInput(filePath);
        terminal.focus();
    }
}

function setupFileSearch() {
    // Open modal on search button click
    searchBtn.addEventListener('click', openSearchModal);

    // Close modal on close button click
    searchClose.addEventListener('click', closeSearchModal);

    // Close modal on backdrop click
    searchModal.addEventListener('click', (e) => {
        if (e.target === searchModal) {
            closeSearchModal();
        }
    });

    // Debounced search on input
    searchInput.addEventListener('input', (e) => {
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => {
            performSearch(e.target.value);
        }, 200);
    });

    // Close on Escape key
    searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeSearchModal();
        }
    });
}

/**
 * Setup event listeners
 */
function setupEventListeners() {
    // Collapse toggle for control bars
    if (collapseToggle) {
        let collapseHandled = false;
        const handleCollapseToggle = (e) => {
            if (collapseHandled) return;
            collapseHandled = true;
            e.preventDefault();
            e.stopPropagation();
            toggleControlBarsCollapse();
            setTimeout(() => { collapseHandled = false; }, 300);
        };
        collapseToggle.addEventListener('touchstart', handleCollapseToggle, { passive: false });
        collapseToggle.addEventListener('click', handleCollapseToggle);
    }

    // Key mapping for control and quick buttons
    const keyMap = {
        'ctrl-b': '\x02',     // tmux prefix
        'ctrl-c': '\x03',     // Interrupt
        'ctrl-d': '\x04',     // EOF
        'ctrl-l': '\x0C',     // Clear screen
        'ctrl-z': '\x1A',     // Suspend
        'ctrl-a': '\x01',     // Beginning of line
        'ctrl-e': '\x05',     // End of line
        'ctrl-w': '\x17',     // Delete word backward
        'ctrl-u': '\x15',     // Delete to start of line
        'ctrl-k': '\x0B',     // Delete to end of line
        'ctrl-r': '\x12',     // Reverse search history
        'ctrl-o': '\x0F',     // Operate-and-get-next / nano save
        'tab': '\t',
        'enter': '\r',
        'esc': '\x1b',
        'up': '\x1b[A',
        'down': '\x1b[B',
        'left': '\x1b[D',
        'right': '\x1b[C',
        '1': '1\r',
        '2': '2\r',
        '3': '3\r',
        'y': 'y\r',
        'n': 'n\r',
        'slash': '/',
    };

    // Control key buttons - use pointerup for better mobile support
    controlBar.querySelectorAll('.ctrl-key').forEach((btn) => {
        btn.addEventListener('pointerup', (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (isControlUnlocked) {
                // Ensure terminal is focused/active before sending input
                if (terminal) terminal.focus();
                const keyName = btn.dataset.key;
                const key = keyMap[keyName] || keyName;
                sendInput(key);
            }
        });
    });

    // Input buttons (numbers, arrows, y/n/enter) - use pointerup for better mobile support
    inputBar.querySelectorAll('.quick-btn').forEach((btn) => {
        btn.addEventListener('pointerup', (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (!isControlUnlocked) return;

            // Ensure terminal is focused/active before sending input
            if (terminal) terminal.focus();
            const keyName = btn.dataset.key;
            const key = keyMap[keyName] || keyName;

            // Clear: clear input box and terminal command line
            if (keyName === 'clear') {
                // Clear input box
                if (logInput) {
                    logInput.value = '';
                    logInput.dataset.autoSuggestion = 'false';
                }
                // Send Ctrl+U to clear terminal command line
                sendInput('\x15');
                return;
            }

            // Queue: add input box content to queue
            if (keyName === 'queue') {
                const input = document.getElementById('logInput');
                const text = input?.value?.trim();
                if (text) {
                    enqueueCommand(text).then(success => {
                        if (success) {
                            input.value = '';
                            showToast('Added to queue', 'success');
                        } else {
                            showToast('Failed to add to queue', 'error');
                        }
                    });
                } else {
                    showToast('Enter a command first', 'error');
                }
                return;
            }

            // Up/Down/Tab: send with sync-back to input box
            if (keyName === 'up' || keyName === 'down') {
                sendKeyWithSync(key, 100);
            } else if (keyName === 'tab') {
                sendKeyWithSync(key, 200);  // Tab completion needs more time
            } else if (keyName === 'ctrl-c') {
                // Ctrl+C - immediate, no confirmation
                sendKeyDebounced(key, true);  // Force immediate (no debounce)
                showToast('Interrupt sent', 'success');
            } else {
                sendKeyDebounced(key);
            }
        });
    });

    // Prevent zoom on double-tap (but not on scrollable areas or buttons)
    document.addEventListener('touchend', (e) => {
        // Don't interfere with button taps or scrollable areas
        if (e.target.closest('button')) return;
        if (e.target.closest('.terminal-container')) return;
        if (e.target.closest('.transcript-content')) return;
        if (e.target.closest('.search-results')) return;

        const now = Date.now();
        if (now - lastTouchEnd <= 300) {
            e.preventDefault();
        }
        lastTouchEnd = now;
    }, { passive: false });
}

let lastTouchEnd = 0;

// Setup viewport and orientation handling
function setupViewportHandler() {
    // Disable Android back button navigation
    history.pushState(null, '', window.location.href);
    window.addEventListener('popstate', (e) => {
        history.pushState(null, '', window.location.href);
    });

    // Resize on orientation change
    window.addEventListener('orientationchange', () => {
        setTimeout(sendResize, 100);
    });

    // Scroll terminal into view when keyboard opens (only if already at bottom)
    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', () => {
            // Only auto-scroll if user was already at bottom (don't interrupt reading)
            const viewport = terminal.element?.querySelector('.xterm-viewport');
            if (viewport) {
                const nearBottom = (viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight) < 50;
                if (nearBottom) {
                    terminal.scrollToBottom();
                }
            }
        });
    }

    // Reconnect immediately when returning to app (visibility change)
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
            // Render cached UI immediately (before reconnect completes)
            // This gives instant feedback while connection is being restored
            renderQueueList();  // Show cached queue items
            // Note: log content is already in DOM, no need to re-render

            // If disconnected, reconnect immediately instead of waiting for backoff
            if (!socket || socket.readyState !== WebSocket.OPEN) {
                console.log('Page visible, reconnecting immediately');

                // Clear any pending timers to avoid races
                if (reconnectTimer) {
                    clearTimeout(reconnectTimer);
                    reconnectTimer = null;
                }
                if (reconnectOverlayTimer) {
                    clearTimeout(reconnectOverlayTimer);
                    reconnectOverlayTimer = null;
                }

                reconnectDelay = INITIAL_RECONNECT_DELAY;
                connect();
            }
        }
    });

    // Handle network state changes (mobile networks are flaky)
    window.addEventListener('online', () => {
        console.log('Network online - checking connection');
        if (!socket || socket.readyState !== WebSocket.OPEN) {
            console.log('Network back, reconnecting immediately');

            // Clear any pending timers
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
            if (reconnectOverlayTimer) {
                clearTimeout(reconnectOverlayTimer);
                reconnectOverlayTimer = null;
            }

            reconnectDelay = INITIAL_RECONNECT_DELAY;
            connect();
        }
    });

    window.addEventListener('offline', () => {
        console.log('Network offline');
        updateConnectionIndicator('disconnected');
        // Stop heartbeat to avoid timeout triggers while offline
        stopHeartbeat();
    });
}

// Enable paste from clipboard
function setupClipboard() {
    document.addEventListener('paste', (e) => {
        if (!isControlUnlocked) return;

        const text = e.clipboardData.getData('text');
        if (text) {
            e.preventDefault();
            sendInput(text);
            terminal.focus();
        }
    });
}

/**
 * Setup jump-to-bottom FAB
 */
function setupJumpToBottom() {
    // Note: xterm.js scrollback doesn't work while tmux is running
    // (tmux manages its own scrollback via copy mode)
    // This just tracks position for auto-scroll behavior

    let isAtBottom = true;

    // Track scroll position using xterm's onScroll event
    terminal.onScroll((scrollPos) => {
        const maxScroll = terminal.buffer.active.length - terminal.rows;
        isAtBottom = scrollPos >= maxScroll - 1;
    });

    // Auto-scroll on new output
    // Use requestAnimationFrame to debounce rapid writes during resize
    const originalWrite = terminal.write.bind(terminal);
    let scrollPending = false;

    terminal.write = (data) => {
        const shouldScroll = isAtBottom || forceScrollToBottom;

        originalWrite(data, () => {
            if (shouldScroll && !scrollPending) {
                scrollPending = true;
                requestAnimationFrame(() => {
                    terminal.scrollToBottom();
                    scrollPending = false;
                });
            }
        });
    };
}

/**
 * Setup compose mode (predictive text + speech-to-text + image upload)
 */
function setupComposeMode() {
    // Open compose modal
    composeBtn.addEventListener('click', () => {
        composeModal.classList.remove('hidden');
        composeInput.value = '';
        clearAttachments();
        setTimeout(() => {
            composeInput.focus();
        }, 100);
    });

    // Close compose modal
    composeClose.addEventListener('click', closeComposeModal);

    // Close on backdrop click
    composeModal.addEventListener('click', (e) => {
        if (e.target === composeModal) {
            closeComposeModal();
        }
    });

    // Clear input and attachments
    composeClear.addEventListener('click', () => {
        composeInput.value = '';
        clearAttachments();
        composeInput.focus();
    });

    // Paste from clipboard
    if (composePaste) {
        composePaste.addEventListener('click', async () => {
            try {
                const text = await navigator.clipboard.readText();
                if (text) {
                    // Insert at cursor position or append
                    const start = composeInput.selectionStart;
                    const end = composeInput.selectionEnd;
                    const before = composeInput.value.substring(0, start);
                    const after = composeInput.value.substring(end);
                    composeInput.value = before + text + after;
                    // Move cursor to end of pasted text
                    const newPos = start + text.length;
                    composeInput.setSelectionRange(newPos, newPos);
                    composeInput.focus();
                }
            } catch (err) {
                console.debug('Clipboard read failed:', err);
            }
        });
    }

    // Send to terminal (text + attachment paths)
    // Insert: insert text only (no Enter)
    // Run: insert text + Enter (execute command)
    function sendComposedText(withEnter = false) {
        let text = composeInput.value;

        // Append attachment paths to the message
        if (pendingAttachments.length > 0) {
            const paths = pendingAttachments.map(a => a.path).join(' ');
            text = text ? `${text} ${paths}` : paths;
        }

        if (text && socket && socket.readyState === WebSocket.OPEN) {
            // Ensure terminal is focused/active before sending input
            if (terminal) terminal.focus();
            // Send text first
            sendInput(text);
            // Then send Enter separately (as terminal expects discrete keypress)
            if (withEnter) {
                sendInput('\r');
            }
            closeComposeModal();
        }
    }

    function queueComposedText() {
        let text = composeInput.value;

        // Append attachment paths to the message
        if (pendingAttachments.length > 0) {
            const paths = pendingAttachments.map(a => a.path).join(' ');
            text = text ? `${text} ${paths}` : paths;
        }

        if (text) {
            enqueueCommand(text).then(success => {
                if (success) {
                    closeComposeModal();
                }
            });
        }
    }

    // Insert button - insert text only (no Enter)
    composeInsert.addEventListener('click', () => {
        sendComposedText(false);
    });

    // Run button - insert text + Enter (execute)
    composeRun.addEventListener('click', () => {
        sendComposedText(true);
    });

    // Queue button - add to queue instead of sending
    const composeQueue = document.getElementById('composeQueue');
    if (composeQueue) {
        composeQueue.addEventListener('click', () => {
            queueComposedText();
        });
    }

    // Send on Ctrl+Enter or Cmd+Enter
    composeInput.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            composeInsert.click();
        }
        if (e.key === 'Escape') {
            closeComposeModal();
        }
    });

    // Attach button - trigger file input (Android shows picker for camera/files/gallery)
    if (composeAttach) {
        composeAttach.addEventListener('click', () => {
            composeFileInput.click();
        });
    }

    // Handle file selection from attach button
    if (composeFileInput) {
        composeFileInput.addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            composeFileInput.value = '';
            await uploadAttachment(file, composeAttach);
        });
    }

    // Think mode dropdown - prepend selected mode to text
    if (composeThinkMode) {
        composeThinkMode.addEventListener('change', () => {
            const mode = composeThinkMode.value;
            if (!mode) return;

            // Remove any existing mode prefix
            let currentText = composeInput.value.trim();
            currentText = currentText.replace(/^(ultrathink|think hard|think|plan):\s*/i, '');

            // Prepend selected mode
            composeInput.value = mode + ': ' + currentText;
            composeInput.focus();

            // Reset dropdown to show "Mode" again
            composeThinkMode.value = '';
        });
    }

    // Handle paste - detect images and auto-upload, ensure text shows immediately
    composeInput.addEventListener('paste', async (e) => {
        const items = e.clipboardData?.items;
        if (!items) return;

        for (const item of items) {
            if (item.type.startsWith('image/')) {
                e.preventDefault();
                const file = item.getAsFile();
                if (file) {
                    await uploadAttachment(file);
                }
                return;
            }
        }
        // Text paste - ensure immediate display (fixes Android timing issues)
        setTimeout(() => {
            composeInput.dispatchEvent(new Event('input', { bubbles: true }));
        }, 0);
    });
}

/**
 * Setup challenge modal (Problem-focused AI code review)
 */
function setupChallenge() {
    if (!challengeBtn || !challengeModal) return;

    const challengeModelSelect = document.getElementById('challengeModel');
    const challengeProblem = document.getElementById('challengeProblem');
    const challengeIncludeTerminal = document.getElementById('challengeIncludeTerminal');
    const challengeIncludeDiff = document.getElementById('challengeIncludeDiff');
    const challengeIncludePlan = document.getElementById('challengeIncludePlan');
    const challengePreview = document.getElementById('challengePreview');
    const challengePreviewContent = document.getElementById('challengePreviewContent');
    const challengeInputSection = document.getElementById('challengeInputSection');
    const challengeInputLabel = document.getElementById('challengeInputLabel');
    const challengeResultContent = document.getElementById('challengeResultContent');
    const challengeCopy = document.getElementById('challengeCopy');
    const challengeToCompose = document.getElementById('challengeToCompose');

    // Store last response for copy/export
    let lastResponseText = '';

    let modelsLoaded = false;

    // Fetch available models
    async function loadModels() {
        if (modelsLoaded) return;

        try {
            const response = await fetch(`/api/challenge/models?token=${token}`);
            if (!response.ok) {
                throw new Error('Failed to load models');
            }
            const data = await response.json();

            challengeModelSelect.innerHTML = '';
            if (data.models && data.models.length > 0) {
                data.models.forEach(model => {
                    const option = document.createElement('option');
                    option.value = model.key;
                    option.textContent = model.name;
                    if (model.key === data.default) {
                        option.selected = true;
                    }
                    challengeModelSelect.appendChild(option);
                });
                modelsLoaded = true;
            } else {
                challengeModelSelect.innerHTML = '<option value="">No models available</option>';
            }
        } catch (error) {
            console.error('Failed to load challenge models:', error);
            challengeModelSelect.innerHTML = '<option value="">Error loading models</option>';
        }
    }

    // Load preview content
    async function loadPreview() {
        if (!challengePreviewContent) return;

        let preview = '';

        // Problem statement
        const problem = challengeProblem?.value?.trim() || '(No problem described)';
        preview += `## Problem Statement\n${problem}\n\n`;

        // Terminal content
        if (challengeIncludeTerminal?.checked) {
            try {
                const response = await fetch(`/api/terminal/capture?token=${token}&lines=50`);
                const data = await response.json();
                if (data.content) {
                    preview += `## Terminal (last 50 lines)\n${data.content.slice(-2000)}\n\n`;
                }
            } catch (e) {
                preview += `## Terminal\n(Failed to capture)\n\n`;
            }
        }

        // Git diff indicator
        if (challengeIncludeDiff?.checked) {
            preview += `## Git Diff\n(Will include uncommitted changes)\n\n`;
        }

        // Active plan
        if (challengeIncludePlan?.checked) {
            try {
                const response = await fetch(`/api/plan/active?token=${token}&preview=true`);
                const data = await response.json();
                if (data.exists && data.content) {
                    preview += `## Active Plan (${data.filename})\n${data.content}\n\n`;
                } else {
                    preview += `## Active Plan\n(No active plan found)\n\n`;
                }
            } catch (e) {
                preview += `## Active Plan\n(Failed to load)\n\n`;
            }
        }

        preview += `## Git Status\n(Will include current status)`;

        challengePreviewContent.textContent = preview;
    }

    // Open modal
    challengeBtn.addEventListener('click', () => {
        challengeModal.classList.remove('hidden');
        challengeResult.classList.add('hidden');
        // Reset input section to open state
        if (challengeInputSection) {
            challengeInputSection.open = true;
            challengeInputLabel.textContent = 'Describe your problem';
        }
        // Hide Copy/Edit buttons until there's a result
        if (challengeCopy) challengeCopy.classList.add('hidden');
        if (challengeToCompose) challengeToCompose.classList.add('hidden');
        lastResponseText = '';
        loadModels();
        loadPreview();
    });

    // Close modal
    challengeClose.addEventListener('click', () => {
        challengeModal.classList.add('hidden');
    });

    // Close on backdrop click
    challengeModal.addEventListener('click', (e) => {
        if (e.target === challengeModal) {
            challengeModal.classList.add('hidden');
        }
    });

    // Update preview when options change
    if (challengeIncludeTerminal) {
        challengeIncludeTerminal.addEventListener('change', loadPreview);
    }
    if (challengeIncludeDiff) {
        challengeIncludeDiff.addEventListener('change', loadPreview);
    }
    if (challengeIncludePlan) {
        challengeIncludePlan.addEventListener('change', loadPreview);
    }
    if (challengeProblem) {
        let previewDebounce = null;
        challengeProblem.addEventListener('input', () => {
            clearTimeout(previewDebounce);
            previewDebounce = setTimeout(loadPreview, 500);
        });
    }

    // Refresh preview when details opens
    if (challengePreview) {
        challengePreview.addEventListener('toggle', () => {
            if (challengePreview.open) {
                loadPreview();
            }
        });
    }

    // Run challenge with problem-focused context
    challengeRun.addEventListener('click', async () => {
        const selectedModel = challengeModelSelect.value;
        if (!selectedModel) {
            challengeResultContent.innerHTML = '<p style="color: var(--danger);">No model selected</p>';
            challengeResult.classList.remove('hidden');
            return;
        }

        const problem = challengeProblem?.value?.trim() || '';
        const includeTerminal = challengeIncludeTerminal?.checked ?? true;
        const includeDiff = challengeIncludeDiff?.checked ?? true;
        const includePlan = challengeIncludePlan?.checked ?? false;

        const modelName = challengeModelSelect.options[challengeModelSelect.selectedIndex]?.text || selectedModel;

        challengeRun.disabled = true;
        challengeRun.textContent = 'Running...';
        challengeResult.classList.remove('hidden');
        challengeResultContent.innerHTML = `<div class="loading">Analyzing with ${modelName}...</div>`;
        challengeResult.classList.add('loading');
        challengeStatus.textContent = '';

        try {
            const params = new URLSearchParams({
                token: token,
                model: selectedModel,
                problem: problem,
                include_terminal: includeTerminal,
                terminal_lines: 50,
                include_diff: includeDiff,
                include_plan: includePlan,
            });

            const response = await fetch(`/api/challenge?${params}`, {
                method: 'POST',
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Challenge failed');
            }

            // Store raw response for copy/export
            lastResponseText = data.content || 'No response received';

            // Format the result with markdown-like headers
            let content = lastResponseText
                .replace(/^(\d+\.\s*Problem Analysis:)/gm, '<h3>Problem Analysis</h3>')
                .replace(/^(\d+\.\s*Potential Causes:)/gm, '<h3>Potential Causes</h3>')
                .replace(/^(\d+\.\s*Suggested Fix:)/gm, '<h3>Suggested Fix</h3>')
                .replace(/^(\d+\.\s*Risks\/Edge Cases:)/gm, '<h3>Risks/Edge Cases</h3>');

            challengeResultContent.innerHTML = content;
            challengeResult.classList.remove('loading');

            // Auto-collapse input section after success
            if (challengeInputSection) {
                challengeInputSection.open = false;
                // Update label with problem snippet
                const snippet = problem.slice(0, 50) + (problem.length > 50 ? '...' : '');
                challengeInputLabel.textContent = snippet || 'General review';
            }

            // Show Copy and Edit buttons
            if (challengeCopy) challengeCopy.classList.remove('hidden');
            if (challengeToCompose) challengeToCompose.classList.remove('hidden');

            // Show stats
            const usage = data.usage || {};
            const stats = [];
            if (data.model_name) stats.push(data.model_name);
            if (data.bundle_chars) stats.push(`${Math.round(data.bundle_chars / 1000)}k ctx`);
            if (usage.total_tokens) stats.push(`${usage.total_tokens} tok`);
            challengeStatus.textContent = stats.join(' | ');

        } catch (error) {
            console.error('Challenge error:', error);
            challengeResultContent.innerHTML = `<p style="color: var(--danger);">Error: ${error.message}</p>`;
            challengeResult.classList.remove('loading');
            challengeStatus.textContent = '';
        } finally {
            challengeRun.disabled = false;
            challengeRun.textContent = 'Run';
        }
    });

    // Copy response to clipboard
    if (challengeCopy) {
        challengeCopy.addEventListener('click', async () => {
            if (!lastResponseText) return;
            try {
                await navigator.clipboard.writeText(lastResponseText);
                const originalText = challengeCopy.textContent;
                challengeCopy.textContent = 'Copied!';
                setTimeout(() => {
                    challengeCopy.textContent = originalText;
                }, 1500);
            } catch (e) {
                console.error('Failed to copy:', e);
            }
        });
    }

    // Export to compose modal
    if (challengeToCompose) {
        challengeToCompose.addEventListener('click', () => {
            if (!lastResponseText) return;
            // Close challenge modal
            challengeModal.classList.add('hidden');
            // Open compose modal with response
            if (composeModal && composeInput) {
                composeModal.classList.remove('hidden');
                composeInput.value = lastResponseText;
                composeInput.focus();
            }
        });
    }
}

/**
 * Extract clarifying questions from DeepSeek challenge response
 * @param {string} content - Raw challenge response content
 * @returns {string} - Formatted questions for Claude
 */
function extractClarifyingQuestions(content) {
    // Look for the "Clarifying questions" section
    const questionsMatch = content.match(/Clarifying questions[^:]*:([\s\S]*?)(?:$|(?=\n\n[A-Z]))/i);

    if (questionsMatch && questionsMatch[1]) {
        // Extract the questions portion
        let questions = questionsMatch[1].trim();

        // Clean up numbered/bulleted list formatting
        questions = questions
            .split('\n')
            .map(line => line.trim())
            .filter(line => line.length > 0)
            .map(line => {
                // Remove leading numbers, bullets, dashes
                return line.replace(/^[\d\.\-\*\)]+\s*/, '').trim();
            })
            .filter(q => q.length > 0)
            .join('\n- ');

        if (questions) {
            return `DeepSeek asked these questions about the code:\n- ${questions}\n\nPlease address these concerns.`;
        }
    }

    // Fallback: return a generic prompt with the full content
    return `DeepSeek's code review raised these points:\n\n${content}\n\nPlease address the concerns above.`;
}

/**
 * Upload a file attachment
 * @param {File} file - The file to upload
 * @param {HTMLElement} [triggerBtn] - Optional button to show uploading state on
 */
async function uploadAttachment(file, triggerBtn) {
    // Show uploading state on the trigger button if provided
    const originalContent = triggerBtn?.textContent;
    if (triggerBtn) {
        triggerBtn.classList.add('uploading');
    }

    try {
        const formData = new FormData();
        formData.append('file', file);

        const response = await fetch(`/api/upload?token=${token}`, {
            method: 'POST',
            body: formData,
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Upload failed');
        }

        const data = await response.json();

        // Add to pending attachments
        pendingAttachments.push({
            path: data.path,
            filename: data.filename,
            size: data.size,
            localUrl: URL.createObjectURL(file),
        });

        renderAttachments();

    } catch (error) {
        console.error('Upload error:', error);
        alert(`Upload failed: ${error.message}`);
    } finally {
        if (triggerBtn) {
            triggerBtn.classList.remove('uploading');
            triggerBtn.textContent = originalContent;
        }
    }
}

/**
 * Render attachment previews
 */
function renderAttachments() {
    if (!composeAttachments) return;

    if (pendingAttachments.length === 0) {
        composeAttachments.classList.add('hidden');
        composeAttachments.innerHTML = '';
        return;
    }

    composeAttachments.classList.remove('hidden');
    composeAttachments.innerHTML = pendingAttachments.map((att, idx) => `
        <div class="attachment-item">
            <img src="${att.localUrl}" alt="" class="attachment-thumb">
            <div class="attachment-info">
                <span class="attachment-path">${att.path}</span>
                <span class="attachment-size">${formatFileSize(att.size)}</span>
            </div>
            <button class="attachment-remove" data-idx="${idx}">&times;</button>
        </div>
    `).join('');

    // Add remove handlers
    composeAttachments.querySelectorAll('.attachment-remove').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const idx = parseInt(e.target.dataset.idx, 10);
            removeAttachment(idx);
        });
    });
}

/**
 * Remove an attachment by index
 */
function removeAttachment(idx) {
    if (pendingAttachments[idx]) {
        URL.revokeObjectURL(pendingAttachments[idx].localUrl);
        pendingAttachments.splice(idx, 1);
        renderAttachments();
    }
}

/**
 * Clear all attachments
 */
function clearAttachments() {
    pendingAttachments.forEach(att => URL.revokeObjectURL(att.localUrl));
    pendingAttachments = [];
    renderAttachments();
}

/**
 * Format file size for display
 */
function formatFileSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function closeComposeModal() {
    composeModal.classList.add('hidden');
    composeInput.blur();
    // Note: Don't clear attachments here - user might reopen modal
}

/**
 * Setup select mode and copy buttons for terminal
 * Select mode: tap start point, tap end point to select text
 */
let isSelectMode = false;
let selectStart = null;  // {row, col}

function setupCopyButton() {
    // Select/Copy button states: 'select' | 'tap-start' | 'tap-end' | 'copy'
    let buttonState = 'select';

    const resetState = () => {
        buttonState = 'select';
        isSelectMode = false;
        selectStart = null;
        if (selectCopyBtn) {
            selectCopyBtn.classList.remove('active');
            selectCopyBtn.textContent = 'Select';
        }
        setTimeout(() => terminal.focus(), 100);
    };

    const fallbackCopy = (text) => {
        try {
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0;';
            textarea.setAttribute('readonly', '');
            document.body.appendChild(textarea);
            textarea.select();
            textarea.setSelectionRange(0, text.length);
            const success = document.execCommand('copy');
            document.body.removeChild(textarea);
            return success;
        } catch (e) {
            return false;
        }
    };

    const handleCopy = () => {
        const selection = terminal.getSelection();
        if (!selection) {
            resetState();
            return;
        }

        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(selection).then(() => {
                selectCopyBtn.textContent = 'Copied!';
                setTimeout(resetState, 1000);
            }).catch(() => {
                const success = fallbackCopy(selection);
                selectCopyBtn.textContent = success ? 'Copied!' : 'Failed';
                setTimeout(resetState, 1000);
            }).finally(() => {
                terminal.clearSelection();
            });
        } else {
            const success = fallbackCopy(selection);
            selectCopyBtn.textContent = success ? 'Copied!' : 'Failed';
            terminal.clearSelection();
            setTimeout(resetState, 1000);
        }
    };

    // Button click handler - behavior depends on state
    if (selectCopyBtn) {
        selectCopyBtn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();

            if (buttonState === 'select') {
                // Enter select mode
                buttonState = 'tap-start';
                isSelectMode = true;
                selectStart = null;
                selectCopyBtn.classList.add('active');
                selectCopyBtn.textContent = 'Tap start';
                terminal.clearSelection();
            } else if (buttonState === 'tap-start' || buttonState === 'tap-end') {
                // Cancel selection
                resetState();
            } else if (buttonState === 'copy') {
                // Copy selection
                handleCopy();
            }
        });
    }

    // Handle taps on terminal for selection
    let lastSelectionTap = 0;
    terminalContainer.addEventListener('click', (e) => {
        if (!isSelectMode) return;

        // Debounce to prevent double-firing
        const now = Date.now();
        if (now - lastSelectionTap < 300) return;
        lastSelectionTap = now;

        try {
            const clientX = e.clientX;
            const clientY = e.clientY;

            // Get terminal cell dimensions
            const cellWidth = terminal._core._renderService.dimensions.css.cell.width;
            const cellHeight = terminal._core._renderService.dimensions.css.cell.height;

            // Get position relative to terminal viewport
            const screen = terminalContainer.querySelector('.xterm-screen');
            if (!screen) return;
            const rect = screen.getBoundingClientRect();
            const x = clientX - rect.left;
            const y = clientY - rect.top;

            // Convert to row/col
            const col = Math.floor(x / cellWidth);
            const row = Math.floor(y / cellHeight) + terminal.buffer.active.viewportY;

            if (!selectStart) {
                // First tap - set start point
                selectStart = { row, col };
                buttonState = 'tap-end';
                selectCopyBtn.textContent = 'Tap end';
            } else {
                // Second tap - set end point and select
                const startRow = Math.min(selectStart.row, row);
                const endRow = Math.max(selectStart.row, row);

                if (startRow === endRow) {
                    const startCol = Math.min(selectStart.col, col);
                    const length = Math.abs(col - selectStart.col) + 1;
                    terminal.select(startCol, startRow, length);
                } else {
                    terminal.selectLines(startRow, endRow);
                }

                // Transition to copy state
                buttonState = 'copy';
                isSelectMode = false;
                selectStart = null;
                selectCopyBtn.classList.add('active');
                selectCopyBtn.textContent = 'Copy';
            }
        } catch (err) {
            console.error('Selection error:', err);
            resetState();
        }
    });

}


/**
 * Setup local command history
 */
function setupCommandHistory() {
    // Track input for history
    let inputBuffer = '';

    terminal.onKey(({ key, domEvent }) => {
        if (!isControlUnlocked) return;

        // Enter key - save to history
        if (domEvent.key === 'Enter') {
            if (inputBuffer.trim()) {
                // Add to history (avoid duplicates)
                if (commandHistory[commandHistory.length - 1] !== inputBuffer) {
                    commandHistory.push(inputBuffer);
                    if (commandHistory.length > MAX_HISTORY_SIZE) {
                        commandHistory.shift();
                    }
                    localStorage.setItem('terminalHistory', JSON.stringify(commandHistory));
                }
            }
            inputBuffer = '';
            historyIndex = -1;
        }
        // Arrow up - previous in history
        else if (domEvent.key === 'ArrowUp' && commandHistory.length > 0) {
            if (historyIndex === -1) {
                currentInput = inputBuffer;
            }
            if (historyIndex < commandHistory.length - 1) {
                historyIndex++;
                // Clear current line and insert history item
                // This works with bash-style line editing
            }
        }
        // Arrow down - next in history
        else if (domEvent.key === 'ArrowDown' && historyIndex >= 0) {
            historyIndex--;
            if (historyIndex === -1) {
                // Restore original input
            }
        }
        // Regular character - add to buffer
        else if (key.length === 1 && !domEvent.ctrlKey && !domEvent.metaKey) {
            inputBuffer += key;
        }
        // Backspace - remove from buffer
        else if (domEvent.key === 'Backspace') {
            inputBuffer = inputBuffer.slice(0, -1);
        }
        // Ctrl+C or Ctrl+U - clear buffer
        else if (domEvent.ctrlKey && (domEvent.key === 'c' || domEvent.key === 'u')) {
            inputBuffer = '';
            historyIndex = -1;
        }
    });
}

/**
 * View toggle: Log | Terminal | Context | Touch
 */
let currentView = 'log';  // 'log', 'terminal', 'context', 'touch'
let transcriptText = '';  // Cached transcript text

// Auto-refresh timer for log view
let logAutoRefreshTimer = null;
const LOG_AUTO_REFRESH_INTERVAL = 2000;  // Refresh every 2 seconds for real-time feel

// Active Prompt functions are defined earlier - these are aliases for compatibility
function startTailViewport() { startActivePrompt(); }
function stopTailViewport() { stopActivePrompt(); }
function updateTailViewport() { refreshActivePrompt(); }

function setupViewToggle() {
    // Views are now: log (primary), terminal, context, touch
    // Tab buttons removed - using swipe and dots now

    // Refresh buttons for context and touch views
    const contextRefresh = document.getElementById('contextRefresh');
    const touchRefresh = document.getElementById('touchRefresh');
    if (contextRefresh) {
        contextRefresh.addEventListener('click', fetchContext);
    }
    if (touchRefresh) {
        touchRefresh.addEventListener('click', fetchTouch);
    }

    // Log input handling
    setupLogInput();
}

// Tab order for swipe navigation
const tabOrder = ['log', 'terminal', 'context', 'touch'];

function clearAllTabActive() {
    // Tab buttons removed from header - dots handle indication now
    // This function is kept for compatibility but no longer needed
}

/**
 * Update the dot indicator to reflect current view
 */
function updateTabIndicator() {
    const dots = document.querySelectorAll('.tab-dot');
    dots.forEach(dot => {
        dot.classList.remove('active');
        if (dot.dataset.view === currentView) {
            dot.classList.add('active');
        }
    });
}

/**
 * Switch to next tab (swipe left)
 */
function switchToNextTab() {
    const currentIndex = tabOrder.indexOf(currentView);
    if (currentIndex < tabOrder.length - 1) {
        const nextView = tabOrder[currentIndex + 1];
        switchToView(nextView);
    }
}

/**
 * Switch to previous tab (swipe right)
 */
function switchToPrevTab() {
    const currentIndex = tabOrder.indexOf(currentView);
    if (currentIndex > 0) {
        const prevView = tabOrder[currentIndex - 1];
        switchToView(prevView);
    }
}

/**
 * Switch to a specific view by name
 */
function switchToView(viewName) {
    switch (viewName) {
        case 'log':
            switchToLogView();
            break;
        case 'terminal':
            switchToTerminalView();
            break;
        case 'context':
            switchToContextView();
            break;
        case 'touch':
            switchToTouchView();
            break;
    }
}

/**
 * Setup swipe gesture detection for tab navigation
 */
function setupSwipeNavigation() {
    const containers = [
        document.getElementById('logView'),
        document.getElementById('terminalView'),
        document.getElementById('contextContainer'),
        document.getElementById('touchContainer'),
    ];

    const SWIPE_THRESHOLD = 80;    // Minimum px to trigger
    const SWIPE_TIMEOUT = 300;     // Max ms for swipe
    const DIRECTION_RATIO = 1.5;   // deltaX must be > deltaY * ratio

    let touchStartX = 0;
    let touchStartY = 0;
    let touchStartTime = 0;

    const handleTouchStart = (e) => {
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
        touchStartTime = Date.now();
    };

    const handleTouchEnd = (e) => {
        const deltaX = e.changedTouches[0].clientX - touchStartX;
        const deltaY = e.changedTouches[0].clientY - touchStartY;
        const deltaTime = Date.now() - touchStartTime;

        // Validate swipe
        if (deltaTime > SWIPE_TIMEOUT) return;
        if (Math.abs(deltaX) < SWIPE_THRESHOLD) return;
        if (Math.abs(deltaY) * DIRECTION_RATIO > Math.abs(deltaX)) return;

        if (deltaX < 0) {
            // Swipe left - next tab
            switchToNextTab();
        } else {
            // Swipe right - previous tab
            switchToPrevTab();
        }
    };

    containers.forEach(container => {
        if (container) {
            container.addEventListener('touchstart', handleTouchStart, { passive: true });
            container.addEventListener('touchend', handleTouchEnd, { passive: true });
        }
    });

    // Click handlers for dots
    document.querySelectorAll('.tab-dot').forEach(dot => {
        dot.addEventListener('click', () => {
            const viewName = dot.dataset.view;
            if (viewName && viewName !== currentView) {
                switchToView(viewName);
            }
        });
    });
}

function hideAllContainers() {
    if (logView) logView.classList.add('hidden');
    if (terminalView) terminalView.classList.add('hidden');
    if (transcriptContainer) transcriptContainer.classList.add('hidden');
    contextContainer.classList.add('hidden');
    touchContainer.classList.add('hidden');
    // Stop auto-refresh when leaving log view
    stopLogAutoRefresh();
    stopTailViewport();
}

function switchToLogView() {
    currentView = 'log';
    hideAllContainers();
    if (logView) logView.classList.remove('hidden');
    viewBar.classList.remove('hidden');  // Show action bar (Select, Stop, Challenge, Compose)
    // Show control bars if unlocked (same as terminal view)
    if (isControlUnlocked) {
        controlBarsContainer.classList.remove('hidden');
    }
    updateTabIndicator();
    // Reset and load log content fresh
    logLoaded = false;
    loadLogContent();
    // Start auto-refresh
    startLogAutoRefresh();
    // Start tail viewport refresh
    startTailViewport();
    // Check for active plan
    checkActivePlan();
}

function switchToTerminalView() {
    currentView = 'terminal';
    hideAllContainers();
    if (terminalView) terminalView.classList.remove('hidden');
    viewBar.classList.remove('hidden');  // Show action bar in terminal view
    // Only show control bars if unlocked
    if (isControlUnlocked) {
        controlBarsContainer.classList.remove('hidden');
    }
    updateTabIndicator();
    // Resize terminal to fit container
    setTimeout(() => {
        if (fitAddon) fitAddon.fit();
        sendResize();
    }, 100);
}

async function switchToContextView() {
    currentView = 'context';
    hideAllContainers();
    contextContainer.classList.remove('hidden');
    viewBar.classList.add('hidden');
    controlBarsContainer.classList.add('hidden');
    updateTabIndicator();

    await fetchContext();
}

async function switchToTouchView() {
    currentView = 'touch';
    hideAllContainers();
    touchContainer.classList.remove('hidden');
    viewBar.classList.add('hidden');
    controlBarsContainer.classList.add('hidden');
    updateTabIndicator();

    await fetchTouch();
}

async function fetchContext() {
    const cacheKey = `cache_context_${currentSession || 'default'}`;
    const cached = localStorage.getItem(cacheKey);

    // Show cached content immediately if available
    if (cached) {
        try {
            const { content } = JSON.parse(cached);
            contextContent.innerHTML = marked.parse(content);
        } catch (e) {
            // Invalid cache, ignore
        }
    } else {
        contextContent.textContent = 'Loading CONTEXT.md...';
    }

    try {
        const response = await fetch(`/api/context?token=${token}`);
        if (!response.ok) {
            throw new Error('Failed to fetch context');
        }
        const data = await response.json();

        if (!data.exists) {
            contextContent.innerHTML = '<p class="no-content">No CONTEXT.md file found in .claude/ directory.</p>';
            localStorage.removeItem(cacheKey);
            return;
        }

        contextContent.innerHTML = marked.parse(data.content);
        localStorage.setItem(cacheKey, JSON.stringify({
            content: data.content,
            timestamp: Date.now()
        }));
    } catch (error) {
        console.error('Context error:', error);
        if (!cached) {
            contextContent.innerHTML = '<p class="error-content">Error loading context: ' + error.message + '</p>';
        }
    }
}

async function fetchTouch() {
    const cacheKey = `cache_touch_${currentSession || 'default'}`;
    const cached = localStorage.getItem(cacheKey);

    // Show cached content immediately if available
    if (cached) {
        try {
            const { content } = JSON.parse(cached);
            touchContent.innerHTML = marked.parse(content);
        } catch (e) {
            // Invalid cache, ignore
        }
    } else {
        touchContent.textContent = 'Loading touch-summary.md...';
    }

    try {
        const response = await fetch(`/api/touch?token=${token}`);
        if (!response.ok) {
            throw new Error('Failed to fetch touch summary');
        }
        const data = await response.json();

        if (!data.exists) {
            touchContent.innerHTML = '<p class="no-content">No touch-summary.md file found in .claude/ directory.</p>';
            localStorage.removeItem(cacheKey);
            return;
        }

        touchContent.innerHTML = marked.parse(data.content);
        localStorage.setItem(cacheKey, JSON.stringify({
            content: data.content,
            timestamp: Date.now()
        }));
    } catch (error) {
        console.error('Touch error:', error);
        if (!cached) {
            touchContent.innerHTML = '<p class="error-content">Error loading touch summary: ' + error.message + '</p>';
        }
    }
}

let transcriptSource = '';  // 'log' or 'capture'

async function fetchTranscript() {
    const cacheKey = `cache_log_${currentSession || 'default'}`;
    const cached = localStorage.getItem(cacheKey);

    // Show cached content immediately if available
    if (cached) {
        try {
            const { content } = JSON.parse(cached);
            transcriptText = content;
            renderTranscript(transcriptText);
        } catch (e) {
            // Invalid cache, ignore
        }
    } else {
        transcriptContent.textContent = 'Loading Claude log...';
    }
    transcriptSearchCount.textContent = '';

    try {
        // Use new /api/log endpoint for Claude conversation logs
        const response = await fetch(`/api/log?token=${token}`);
        if (!response.ok) {
            throw new Error('Failed to fetch log');
        }
        const data = await response.json();

        if (!data.exists) {
            transcriptContent.innerHTML = '<p class="no-content">No Claude log found for this project.</p>';
            localStorage.removeItem(cacheKey);
            return;
        }

        transcriptText = data.content || '';
        transcriptSource = 'log';

        // Show truncation indicator if applicable
        const statusLabel = data.truncated ? 'Truncated' : 'Full';
        transcriptSearchCount.textContent = statusLabel;

        renderTranscript(transcriptText);

        // Cache the content
        localStorage.setItem(cacheKey, JSON.stringify({
            content: transcriptText,
            timestamp: Date.now()
        }));
    } catch (error) {
        console.error('Log error:', error);
        if (!cached) {
            transcriptContent.innerHTML = '<p class="error-content">Error loading log: ' + error.message + '</p>';
        }
    }
}

/**
 * Clean terminal output by removing clutter
 * - Collapse multiple blank lines
 * - Remove spinner lines (Braille spinners, etc.)
 * - Remove progress-only lines
 * - Clean up carriage return artifacts
 */
function cleanTerminalOutput(text) {
    // Split into lines
    let lines = text.split('\n');

    // Spinner characters (Braille pattern used by Claude)
    const spinnerChars = /[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷]/;

    // Box drawing characters
    const boxDrawing = /^[─│┌┐└┘├┤┬┴┼━┃╭╮╯╰═║╔╗╚╝╠╣╦╩╬\s]+$/;

    // Filter and clean lines
    const cleanedLines = [];
    let prevWasBlank = false;

    for (let line of lines) {
        // Handle carriage return (keep only last segment)
        if (line.includes('\r')) {
            const parts = line.split('\r');
            line = parts[parts.length - 1];
        }

        const trimmed = line.trim();

        // Skip lines that are just spinners
        if (trimmed.length <= 3 && spinnerChars.test(trimmed)) {
            continue;
        }

        // Skip lines that are just box drawing (borders)
        if (trimmed.length > 0 && boxDrawing.test(trimmed)) {
            continue;
        }

        // Skip lines that are mostly progress bar
        if (trimmed.length > 0 && trimmed.replace(/[█▓▒░▏▎▍▌▋▊▉\s\[\]%0-9\/]/g, '').length < 3) {
            continue;
        }

        // Skip "working..." type status lines that repeat
        if (/^(working|thinking|processing|loading)\.{0,3}$/i.test(trimmed)) {
            continue;
        }

        // Skip specific Claude Code status hints (not questions/options)
        // Only filter "accept edits", "shift+tab to cycle" - NOT interactive prompts
        if (/^[⏵▶►→]{1,2}\s*(accept|shift\+tab|tab to|esc to|ctrl\+)/i.test(trimmed)) {
            continue;
        }

        // Skip "Context left until auto-compact" lines
        if (/context left|auto-compact/i.test(trimmed)) {
            continue;
        }

        // Collapse multiple blank lines
        const isBlank = trimmed === '';
        if (isBlank && prevWasBlank) {
            continue;
        }
        prevWasBlank = isBlank;

        cleanedLines.push(line);
    }

    return cleanedLines.join('\n');
}

// Strip ANSI escape codes from text
function stripAnsi(text) {
    return text
        // Full ANSI CSI sequences: ESC [ (optional ?) ... letter
        .replace(/\x1b\[\??[0-9;]*[a-zA-Z]/g, '')
        // Orphaned CSI sequences (missing ESC): [?2026l, [0m, etc.
        .replace(/\[\??[0-9;]*[a-zA-Z]/g, '')
        // Standalone DEC sequences: ?2026l, ?2026h, etc.
        .replace(/\?[0-9]+[a-zA-Z]/g, '')
        // RGB color codes that got split: 38;2;R;G;Bm or 48;2;R;G;Bm
        .replace(/\b[34]8;2;[0-9;]+m/g, '')
        // Simple color codes: 0m, 1m, 32m, etc.
        .replace(/\b[0-9;]+m\b/g, '')
        // OSC sequences (ESC ] ... BEL)
        .replace(/\x1b\][^\x07]*\x07/g, '')
        // OSC sequences with ST terminator
        .replace(/\x1b\][^\x1b]*\x1b\\/g, '')
        // Other escape sequences
        .replace(/\x1b[PX^_][^\x1b]*\x1b\\/g, '')
        .replace(/\x1b[\x40-\x5F]/g, '')
        // Control characters (except tab, newline, carriage return)
        .replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, '')
        // Normalize line endings
        .replace(/\r\n/g, '\n')
        .replace(/\r/g, '\n');
}

function renderTranscript(text, searchTerm = '') {
    // Strip ANSI codes for clean display
    text = stripAnsi(text);

    // Pre-process: merge continuation lines for better word wrap
    text = mergeTranscriptLines(text);

    const lines = text.split('\n');
    let html = '';
    let searchCount = 0;
    let lastWasEmpty = false;

    // Collapsible output block tracking
    let outputBuffer = [];
    let outputContext = '';  // What triggered this output (tool name or command)

    // Patterns for detecting different line types
    const promptPattern = /^(\s*)([\$#>❯]|\w+@[\w.-]+[:\$#]|\([\w-]+\)\s*[\$#])/;
    const toolCallPattern = /^(\s*)[•●]\s*\w+[\(:\[]/;  // • Bash(, • Read:, etc.
    const bulletPattern = /^(\s*)[•●-]\s+/;  // Any bullet point
    const hrPattern = /^[\s]*[_\-=]{3,}[\s]*$/;  // Horizontal rules: ___, ---, ===
    const pathPattern = /(\/[\w./-]+|~\/[\w./-]*)/g;
    const flagPattern = /(\s--?[\w-]+)/g;
    const stringPattern = /("[^"]*"|'[^']*')/g;
    const codePattern = /`([^`]+)`/g;  // Inline code in backticks

    // Helper to flush output buffer as collapsible block
    function flushOutputBuffer() {
        if (outputBuffer.length === 0) return;

        const lineCount = outputBuffer.length;
        const preview = outputBuffer[0].text.slice(0, 50) + (outputBuffer[0].text.length > 50 ? '...' : '');
        const summary = outputContext ? `${outputContext} output` : `${lineCount} line${lineCount > 1 ? 's' : ''}`;

        // Only collapse if more than 3 lines
        if (lineCount > 3) {
            html += `<details class="output-block"><summary class="output-summary">${summary}</summary><div class="output-content">`;
            for (const item of outputBuffer) {
                html += item.html;
            }
            html += '</div></details>';
        } else {
            // Small output - don't collapse
            for (const item of outputBuffer) {
                html += item.html;
            }
        }

        outputBuffer = [];
        outputContext = '';
    }

    for (const line of lines) {
        const isEmpty = line.trim() === '';

        // Collapse consecutive blank lines
        if (isEmpty) {
            if (!lastWasEmpty) {
                if (outputBuffer.length > 0) {
                    outputBuffer.push({ text: '', html: '<div class="transcript-line empty"></div>' });
                } else {
                    html += '<div class="transcript-line empty"></div>';
                }
            }
            lastWasEmpty = true;
            continue;
        }
        lastWasEmpty = false;

        // Horizontal rule - flush buffer first
        if (hrPattern.test(line)) {
            flushOutputBuffer();
            html += '<hr class="transcript-hr">';
            continue;
        }

        const isPromptLine = promptPattern.test(line);
        const isToolCall = toolCallPattern.test(line);
        const isBullet = bulletPattern.test(line);
        const isStructural = isPromptLine || isToolCall || isBullet;

        // If we hit a structural line, flush any pending output
        if (isStructural) {
            flushOutputBuffer();
        }

        let escaped = line
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');

        // Apply shell syntax highlighting ONLY to command lines (not Claude output)
        if (isPromptLine) {
            // Highlight paths, flags, strings first (before adding HTML)
            escaped = escaped.replace(pathPattern, '\x00PATH\x01$1\x00/PATH\x01');
            escaped = escaped.replace(flagPattern, '\x00FLAG\x01$1\x00/FLAG\x01');
            escaped = escaped.replace(stringPattern, '\x00STR\x01$1\x00/STR\x01');

            // Highlight the prompt itself
            escaped = escaped.replace(
                /^(\s*)([\$#&gt;❯]|[\w]+@[\w.-]+[:\$#]|\([\w-]+\)\s*[\$#])/,
                '$1\x00PROMPT\x01$2\x00/PROMPT\x01'
            );

            // Convert placeholders to HTML
            escaped = escaped
                .replace(/\x00PATH\x01/g, '<span class="path">')
                .replace(/\x00\/PATH\x01/g, '</span>')
                .replace(/\x00FLAG\x01/g, '<span class="flag">')
                .replace(/\x00\/FLAG\x01/g, '</span>')
                .replace(/\x00STR\x01/g, '<span class="string">')
                .replace(/\x00\/STR\x01/g, '</span>')
                .replace(/\x00PROMPT\x01/g, '<span class="prompt">')
                .replace(/\x00\/PROMPT\x01/g, '</span>');
        } else if (isToolCall) {
            // Extract tool name for context
            const toolMatch = line.match(/[•●]\s*(\w+)/);
            outputContext = toolMatch ? toolMatch[1] : '';

            // Highlight tool name: • ToolName(
            escaped = escaped.replace(
                /^(\s*)([•●]\s*)(\w+)([\(:\[])/,
                '$1<span class="tool-bullet">$2</span><span class="tool-name">$3</span>$4'
            );
        } else if (isBullet) {
            // Highlight bullet points
            escaped = escaped.replace(
                /^(\s*)([•●-])(\s+)/,
                '$1<span class="bullet">$2</span>$3'
            );
        }

        // Highlight inline code (backticks) - safe for all lines
        escaped = escaped.replace(codePattern, '<code class="inline-code">$1</code>');

        // Apply search highlighting if searching
        if (searchTerm) {
            const regex = new RegExp(`(${escapeRegExp(searchTerm)})`, 'gi');
            const matches = escaped.match(regex);
            if (matches) searchCount += matches.length;
            escaped = escaped.replace(regex, '<span class="highlight">$1</span>');
        }

        // Determine line class
        let lineClass = 'transcript-line output';
        if (isPromptLine) {
            lineClass = 'transcript-line command';
        } else if (isToolCall) {
            lineClass = 'transcript-line tool-call';
        } else if (isBullet) {
            lineClass = 'transcript-line bullet-item';
        }

        const lineHtml = `<div class="${lineClass}">${escaped}</div>`;

        // Collect output lines into buffer, structural lines go directly to html
        if (!isStructural) {
            outputBuffer.push({ text: line, html: lineHtml });
        } else {
            html += lineHtml;
        }
    }

    // Flush any remaining output
    flushOutputBuffer();

    transcriptContent.innerHTML = html;

    if (searchTerm) {
        transcriptSearchCount.textContent = searchCount > 0 ? `${searchCount} match${searchCount === 1 ? '' : 'es'}` : 'No matches';
        const firstMatch = transcriptContent.querySelector('.highlight');
        if (firstMatch) {
            firstMatch.classList.add('current');
            firstMatch.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    } else {
        // Show source indicator when not searching
        const sourceLabel = transcriptSource === 'log' ? 'Live Log' : 'Snapshot';
        transcriptSearchCount.textContent = sourceLabel;
    }
}

/**
 * Merge continuation lines for better word wrap
 * Lines that are just wrapped text (don't start with special patterns) get merged
 */
function mergeTranscriptLines(text) {
    const lines = text.split('\n');
    const merged = [];

    // Patterns that indicate a new logical line (not a continuation)
    const newLinePatterns = [
        /^[\s]*$/,                           // Empty line
        /^[\s]*[•●-]\s/,                     // Bullet point
        /^[\s]*[\$#>❯]/,                     // Prompt
        /^[\s]*\w+@[\w.-]+[:\$#]/,           // user@host prompt
        /^[\s]*\([^)]+\)\s*[\$#]/,           // (env) $ prompt
        /^[\s]*[_\-=]{3,}[\s]*$/,            // Horizontal rule
        /^[\s]*\d+\.\s/,                     // Numbered list
        /^[\s]*[A-Z][a-z]+:\s/,              // Label: value
        /^[\s]*```/,                         // Code fence
        /^[\s]*#+\s/,                        // Markdown header
        /^[\s]*\|/,                          // Table row
        /^\s{4,}/,                           // Heavily indented (code block)
    ];

    const isNewLogicalLine = (line) => {
        return newLinePatterns.some(p => p.test(line));
    };

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        // If this is a new logical line or first line, start fresh
        if (merged.length === 0 || isNewLogicalLine(line)) {
            merged.push(line);
        } else {
            // This is a continuation - merge with previous line
            const prev = merged[merged.length - 1];
            // Only merge if previous line doesn't end with punctuation that suggests completion
            if (prev && !prev.match(/[.!?:]\s*$/) && line.trim()) {
                merged[merged.length - 1] = prev + ' ' + line.trim();
            } else {
                merged.push(line);
            }
        }
    }

    return merged.join('\n');
}

function escapeRegExp(string) {
    return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function setupTranscriptSearch() {
    let searchDebounce = null;

    if (!transcriptSearch) return;

    transcriptSearch.addEventListener('input', (e) => {
        clearTimeout(searchDebounce);
        searchDebounce = setTimeout(() => {
            renderTranscript(transcriptText, e.target.value);
        }, 200);
    });

    // Clear search on Escape
    transcriptSearch.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            transcriptSearch.value = '';
            renderTranscript(transcriptText, '');
        }
    });
}

/**
 * Load log content for the hybrid view
 */
let logLoaded = false;

async function loadLogContent() {
    if (!logContent) return;

    // Only load once per session (refresh button can reload)
    if (logLoaded) return;

    logContent.innerHTML = '<div class="log-loading">Loading context...</div>';

    try {
        const response = await fetch(`/api/log?token=${token}`);
        if (!response.ok) {
            throw new Error('Failed to fetch log');
        }
        const data = await response.json();

        if (!data.exists || !data.content) {
            logContent.innerHTML = '<div class="log-empty">No recent activity</div>';
            logLoaded = true;
            return;
        }

        // Parse and render log entries
        renderLogEntries(data.content, data.cached);
        logLoaded = true;

        // Update last modified time for change detection
        lastLogModified = data.modified || 0;

        // Load suggestions from terminal capture (not JSONL log)
        loadTerminalSuggestions();

    } catch (error) {
        console.error('Log error:', error);
        logContent.innerHTML = `<div class="log-error">Error loading log: ${error.message}</div>`;
    }
}

/**
 * Render log entries in the hybrid view log section
 * Parses conversation format: $ user | • Tool: | assistant text
 * @param {string} content - The log content to render
 * @param {boolean} cached - Whether content is from cache (after /clear)
 */
function renderLogEntries(content, cached = false) {
    if (!logContent) return;

    // Strip ANSI codes
    content = stripAnsi(content);

    // Split by double newline to get message blocks
    const blocks = content.split('\n\n').filter(b => b.trim());

    if (blocks.length === 0) {
        logContent.innerHTML = '<div class="log-empty">No recent activity</div>';
        return;
    }

    // Show cached indicator if serving from cache
    let cachedBanner = '';
    if (cached) {
        cachedBanner = '<div class="log-cached-banner">Showing cached log (session was cleared)</div>';
    }

    // Group consecutive messages by role
    const messages = [];
    let currentGroup = null;

    for (const block of blocks) {
        const trimmed = block.trim();
        if (!trimmed) continue;

        let role, text;

        if (trimmed.startsWith('$ ')) {
            // User message
            role = 'user';
            text = trimmed.slice(2);  // Remove "$ " prefix
        } else if (trimmed.startsWith('• ')) {
            // Tool call
            role = 'tool';
            text = trimmed;
        } else {
            // Assistant message
            role = 'assistant';
            text = trimmed;
        }

        // Group consecutive assistant/tool messages together
        if (currentGroup && (
            (currentGroup.role === 'assistant' && role === 'assistant') ||
            (currentGroup.role === 'assistant' && role === 'tool') ||
            (currentGroup.role === 'tool' && role === 'tool')
        )) {
            currentGroup.blocks.push({ role, text });
        } else {
            // Start new group
            if (currentGroup) messages.push(currentGroup);
            currentGroup = {
                role: role === 'tool' ? 'assistant' : role,  // Tools are part of assistant turn
                blocks: [{ role, text }]
            };
        }
    }
    if (currentGroup) messages.push(currentGroup);

    // Render message cards
    let html = '';
    for (const msg of messages) {
        const roleLabel = msg.role === 'user' ? 'You' : 'Claude';
        const roleClass = msg.role === 'user' ? 'user' : 'assistant';

        html += `<div class="log-card ${roleClass}">`;
        html += `<div class="log-card-header"><span class="log-role-badge">${roleLabel}</span></div>`;
        html += `<div class="log-card-body">`;

        for (const block of msg.blocks) {
            if (block.role === 'tool') {
                // Render tool call as collapsible
                const toolMatch = block.text.match(/^• (\w+):?\s*(.*)/s);
                if (toolMatch) {
                    const toolName = toolMatch[1];
                    const toolDetail = toolMatch[2] || '';
                    // Truncate long details for summary
                    const summary = toolDetail.length > 60 ? toolDetail.slice(0, 60) + '...' : toolDetail;
                    const summaryKey = (summary || toolName).slice(0, 40).replace(/[^a-zA-Z0-9]/g, '_');
                    html += `<details class="log-tool" data-tool="${toolName}" data-tool-key="${toolName}:${summaryKey}"><summary class="log-tool-summary"><span class="log-tool-name">${toolName}</span> <span class="log-tool-detail">${escapeHtml(summary)}</span></summary>`;
                    html += `<div class="log-tool-content">${escapeHtml(toolDetail)}</div></details>`;
                } else {
                    html += `<div class="log-tool-inline">${escapeHtml(block.text)}</div>`;
                }
            } else if (block.role === 'user') {
                // User text - plain with highlighting
                html += `<div class="log-text user-text">${escapeHtml(block.text)}</div>`;
            } else {
                // Assistant text - render with markdown
                try {
                    html += `<div class="log-text assistant-text">${marked.parse(block.text)}</div>`;
                } catch (e) {
                    html += `<div class="log-text assistant-text">${escapeHtml(block.text)}</div>`;
                }
            }
        }

        html += `</div></div>`;
    }

    logContent.innerHTML = cachedBanner + html;

    // Schedule tool collapsing for idle time (non-blocking)
    scheduleCollapse();

    // Schedule super-collapse for runs of many tools (after regular collapse)
    scheduleSuperCollapse();

    // Schedule plan file preview detection
    schedulePlanPreviews();

    // Extract and show suggestions from last message
    extractAndShowSuggestions(content);

    // Scroll to bottom (render is only called when user is at bottom or explicitly requested)
    logContent.scrollTop = logContent.scrollHeight;
}

/**
 * Schedule tool collapse for idle time
 * Computes hash to skip if content unchanged
 */
function scheduleCollapse() {
    if (!logContent) return;

    const tools = logContent.querySelectorAll('.log-tool');
    if (tools.length < 2) return;  // Nothing to collapse

    // Hash: count + last tool key + html length
    const lastTool = tools[tools.length - 1];
    const hash = `${tools.length}:${lastTool?.dataset.toolKey || ''}:${logContent.innerHTML.length}`;

    if (hash === lastCollapseHash) return;  // Content unchanged

    scheduleIdle(() => {
        try {
            collapseRepeatedTools(hash);
        } catch (e) {
            console.warn('Collapse failed:', e);
            // Graceful degradation - log still visible
        }
    }, { timeout: 500 });
}

/**
 * Single-pass collapse of consecutive duplicate tools
 * Adds badge to first, hides rest unless expanded
 */
function collapseRepeatedTools(hash) {
    const tools = logContent.querySelectorAll('.log-tool');
    if (tools.length < 2) return;

    // Clean previous collapse state
    logContent.querySelectorAll('.collapse-count').forEach(b => b.remove());
    logContent.querySelectorAll('.collapsed-duplicate').forEach(t =>
        t.classList.remove('collapsed-duplicate'));

    let i = 0;
    while (i < tools.length) {
        const toolName = tools[i].dataset.tool;
        const groupKey = tools[i].dataset.toolKey;

        // Count consecutive same-tool entries
        let count = 1;
        let j = i + 1;
        while (j < tools.length && tools[j].dataset.tool === toolName) {
            count++;
            j++;
        }

        if (count > 1) {
            // Add/update badge on first tool
            const summary = tools[i].querySelector('.log-tool-summary');
            if (summary) {
                const badge = document.createElement('span');
                badge.className = 'collapse-count';
                badge.dataset.groupKey = groupKey;
                badge.textContent = `×${count}`;
                summary.appendChild(badge);
            }

            // Hide duplicates unless group is expanded
            if (!expandedGroups.has(groupKey)) {
                for (let k = i + 1; k < j; k++) {
                    tools[k].classList.add('collapsed-duplicate');
                }
            }
        }

        i = j;  // Skip to next group
    }

    // Verify hash still valid (content didn't change during execution)
    const tools2 = logContent.querySelectorAll('.log-tool');
    const lastTool = tools2[tools2.length - 1];
    const currentHash = `${tools2.length}:${lastTool?.dataset.toolKey || ''}:${logContent.innerHTML.length}`;

    if (currentHash === hash) {
        lastCollapseHash = hash;
    }
    // If hash changed, next render will re-trigger collapse
}

/**
 * Schedule super-collapse for idle time
 * Groups runs of many tool calls into single summary row
 */
function scheduleSuperCollapse() {
    if (!logContent) return;

    // Delay slightly to let regular collapse complete first
    setTimeout(() => {
        const tools = logContent.querySelectorAll('.log-tool');
        if (tools.length < SUPER_COLLAPSE_THRESHOLD) return;

        // Hash based on tool count and innerHTML length
        const hash = `super:${tools.length}:${logContent.innerHTML.length}`;
        if (hash === lastSuperCollapseHash) return;

        scheduleIdle(() => {
            try {
                applySuperCollapse(hash);
            } catch (e) {
                console.warn('Super-collapse failed:', e);
            }
        }, { timeout: 700 });
    }, 150);  // Wait for regular collapse to finish
}

/**
 * Apply super-collapse to runs of consecutive tool blocks
 * Creates summary header and hides individual tools
 */
function applySuperCollapse(hash) {
    if (!logContent) return;

    // Remove existing super-group headers
    logContent.querySelectorAll('.tool-supergroup').forEach(g => g.remove());
    // Unhide all tools first
    logContent.querySelectorAll('.super-collapsed').forEach(t =>
        t.classList.remove('super-collapsed'));

    // Find all log cards (message groups)
    const cards = logContent.querySelectorAll('.log-card');

    for (const card of cards) {
        const cardBody = card.querySelector('.log-card-body');
        if (!cardBody) continue;

        // Find runs of consecutive tool elements within this card
        const children = Array.from(cardBody.children);
        let runStart = -1;
        let runTools = [];

        for (let i = 0; i <= children.length; i++) {
            const child = children[i];
            const isTool = child?.classList?.contains('log-tool');

            if (isTool) {
                if (runStart === -1) runStart = i;
                runTools.push(child);
            } else {
                // End of run (or end of children)
                if (runTools.length >= SUPER_COLLAPSE_THRESHOLD) {
                    createSuperGroup(cardBody, runTools, runStart);
                }
                runStart = -1;
                runTools = [];
            }
        }
    }

    // Update hash
    const tools = logContent.querySelectorAll('.log-tool');
    const currentHash = `super:${tools.length}:${logContent.innerHTML.length}`;
    if (currentHash === hash) {
        lastSuperCollapseHash = hash;
    }
}

/**
 * Create a super-group header for a run of tool elements
 */
function createSuperGroup(container, tools, insertIndex) {
    // Generate stable group key from first tool
    const firstTool = tools[0];
    const firstKey = firstTool.dataset.toolKey || firstTool.dataset.tool || 'tools';
    const groupKey = `supergroup:${firstKey}:${tools.length}`;

    // Create header element
    const header = document.createElement('div');
    header.className = 'tool-supergroup';
    header.dataset.groupKey = groupKey;

    const isExpanded = expandedSuperGroups.has(groupKey);
    const arrow = isExpanded ? '▼' : '▶';

    header.innerHTML = `<button class="tool-supergroup-toggle">🔧 ${tools.length} tool operations ${arrow}</button>`;

    // Insert header before the run
    const firstChild = tools[0];
    container.insertBefore(header, firstChild);

    // Hide tools if not expanded
    if (!isExpanded) {
        for (const tool of tools) {
            tool.classList.add('super-collapsed');
        }
    }
}

/**
 * Setup super-collapse toggle via event delegation
 */
function setupSuperCollapseHandler() {
    if (!logContent) return;

    logContent.addEventListener('click', (e) => {
        const toggle = e.target.closest('.tool-supergroup-toggle');
        if (!toggle) return;

        const header = toggle.closest('.tool-supergroup');
        if (!header) return;

        const groupKey = header.dataset.groupKey;
        if (!groupKey) return;

        // Toggle expanded state
        if (expandedSuperGroups.has(groupKey)) {
            expandedSuperGroups.delete(groupKey);
        } else {
            expandedSuperGroups.add(groupKey);
        }

        // Force re-apply super-collapse
        lastSuperCollapseHash = '';
        scheduleSuperCollapse();
    });
}

/**
 * Setup collapse toggle via event delegation
 * Click on collapse badge toggles expanded state
 */
function setupCollapseHandler() {
    if (!logContent) return;

    logContent.addEventListener('click', (e) => {
        const badge = e.target.closest('.collapse-count');
        if (!badge) return;

        const groupKey = badge.dataset.groupKey;
        if (!groupKey) return;

        // Toggle expanded state
        if (expandedGroups.has(groupKey)) {
            expandedGroups.delete(groupKey);
        } else {
            expandedGroups.add(groupKey);
        }

        // Re-run collapse to show/hide
        lastCollapseHash = '';  // Force re-collapse
        scheduleCollapse();
    });
}

/**
 * Setup scroll tracking for log view
 * Tracks if user is at bottom to control auto-scroll
 */
function setupScrollTracking() {
    if (!logContent) return;

    logContent.addEventListener('scroll', () => {
        // Consider "at bottom" if within 50px of bottom
        const scrollBottom = logContent.scrollHeight - logContent.scrollTop - logContent.clientHeight;
        userAtBottom = scrollBottom < 50;

        // If user scrolled to bottom and there's pending content, render it
        if (userAtBottom) {
            hideNewContentIndicator();
            if (pendingLogContent) {
                renderLogEntries(pendingLogContent);
                pendingLogContent = null;
                // Scroll to actual bottom after render
                logContent.scrollTop = logContent.scrollHeight;
            }
        }
    });
}

/**
 * Show "new content" indicator when content arrives while user is reading above
 */
function showNewContentIndicator() {
    if (!logContent) return;

    // Create indicator if it doesn't exist
    if (!newContentIndicator) {
        newContentIndicator = document.createElement('div');
        newContentIndicator.className = 'new-content-indicator';
        newContentIndicator.innerHTML = '↓ New content';
        newContentIndicator.addEventListener('click', () => {
            // Render pending content first, then scroll to bottom
            if (pendingLogContent) {
                renderLogEntries(pendingLogContent);
                pendingLogContent = null;
            }
            logContent.scrollTop = logContent.scrollHeight;
            userAtBottom = true;
            hideNewContentIndicator();
        });
        logContent.parentElement.appendChild(newContentIndicator);
    }

    newContentIndicator.classList.add('visible');
}

/**
 * Hide new content indicator
 */
function hideNewContentIndicator() {
    if (newContentIndicator) {
        newContentIndicator.classList.remove('visible');
    }
}

// Track which plan files have been processed
let processedPlanRefs = new Set();

/**
 * Schedule plan file preview detection for idle time
 */
function schedulePlanPreviews() {
    if (!logContent) return;

    scheduleIdle(() => {
        try {
            detectAndReplacePlanRefs();
        } catch (e) {
            console.warn('Plan preview detection failed:', e);
        }
    }, { timeout: 600 });
}

/**
 * Detect plan file references in log and replace with expandable previews
 * Looks for paths like ~/.claude/plans/foo.md or /home/user/.claude/plans/foo.md
 */
function detectAndReplacePlanRefs() {
    if (!logContent) return;

    // Find all text nodes that might contain plan file references
    const walker = document.createTreeWalker(
        logContent,
        NodeFilter.SHOW_TEXT,
        null,
        false
    );

    const planPathRegex = /(?:~|\/home\/\w+)\/\.claude\/plans\/([\w\-\.]+\.md)/g;
    const nodesToReplace = [];

    let node;
    while (node = walker.nextNode()) {
        const text = node.textContent;
        if (planPathRegex.test(text)) {
            planPathRegex.lastIndex = 0;  // Reset regex
            nodesToReplace.push(node);
        }
    }

    // Replace each text node with plan link elements
    for (const textNode of nodesToReplace) {
        const text = textNode.textContent;
        const fragment = document.createDocumentFragment();
        let lastIndex = 0;
        let match;

        planPathRegex.lastIndex = 0;
        while ((match = planPathRegex.exec(text)) !== null) {
            // Add text before match
            if (match.index > lastIndex) {
                fragment.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
            }

            // Create plan preview link
            const filename = match[1];
            const fullPath = match[0];

            // Check if already processed
            if (!processedPlanRefs.has(fullPath)) {
                const planLink = document.createElement('span');
                planLink.className = 'plan-file-ref';
                planLink.dataset.filename = filename;
                planLink.innerHTML = `<span class="plan-file-icon">📋</span> ${filename} <span class="plan-expand-hint">(tap to preview)</span>`;
                fragment.appendChild(planLink);
                processedPlanRefs.add(fullPath);
            } else {
                // Already processed, just show as text
                fragment.appendChild(document.createTextNode(fullPath));
            }

            lastIndex = match.index + match[0].length;
        }

        // Add remaining text
        if (lastIndex < text.length) {
            fragment.appendChild(document.createTextNode(text.slice(lastIndex)));
        }

        textNode.parentNode.replaceChild(fragment, textNode);
    }
}

/**
 * Setup event delegation for plan file preview clicks
 */
function setupPlanPreviewHandler() {
    if (!logContent) return;

    logContent.addEventListener('click', async (e) => {
        const planRef = e.target.closest('.plan-file-ref');
        if (!planRef) return;

        const filename = planRef.dataset.filename;
        if (!filename) return;

        // Toggle preview
        const existingPreview = planRef.querySelector('.plan-preview');
        if (existingPreview) {
            existingPreview.remove();
            planRef.classList.remove('expanded');
            return;
        }

        // Fetch and show preview
        planRef.classList.add('loading');
        try {
            const response = await fetch(`/api/plan?token=${token}&filename=${encodeURIComponent(filename)}&preview=true`);
            const data = await response.json();

            if (data.exists && data.content) {
                const preview = document.createElement('div');
                preview.className = 'plan-preview';
                preview.innerHTML = `<pre>${escapeHtml(data.content)}</pre>`;
                planRef.appendChild(preview);
                planRef.classList.add('expanded');
            } else {
                const preview = document.createElement('div');
                preview.className = 'plan-preview error';
                preview.textContent = 'Plan file not found';
                planRef.appendChild(preview);
            }
        } catch (err) {
            console.error('Failed to fetch plan preview:', err);
        } finally {
            planRef.classList.remove('loading');
        }
    });
}

/**
 * Check for active plan and show/hide the Plan button
 * Shows button for: found, ambiguous, or fallback plans
 */
async function checkActivePlan() {
    const planBtn = document.getElementById('planBtn');
    const logHeader = document.getElementById('logHeader');
    if (!planBtn || !logHeader) return;

    try {
        const response = await fetch(`/api/plan/active?token=${token}&preview=true`);
        const data = await response.json();

        if (data.exists) {
            planBtn.dataset.filename = data.filename || '';
            planBtn.dataset.status = data.status || 'none';
            // Indicate ambiguous state on button
            if (data.status === 'ambiguous') {
                planBtn.textContent = 'Plan...';
            } else {
                planBtn.textContent = 'Plan';
            }
            logHeader.classList.remove('hidden');
        } else {
            logHeader.classList.add('hidden');
        }
    } catch (e) {
        console.error('Failed to check active plan:', e);
        logHeader.classList.add('hidden');
    }
}

/**
 * Setup plan button and modal handlers
 * Handles: found (with/without link), ambiguous (candidate selection), none
 */
function setupPlanButton() {
    const planBtn = document.getElementById('planBtn');
    const planModal = document.getElementById('planModal');
    const planModalClose = document.getElementById('planModalClose');
    const planModalTitle = document.getElementById('planModalTitle');
    const planModalBody = document.getElementById('planModalBody');

    if (!planBtn || !planModal) return;

    // Helper to link a plan to current repo
    async function linkPlan(filename) {
        try {
            const response = await fetch(`/api/plan/link?token=${token}&filename=${encodeURIComponent(filename)}`, { method: 'POST' });
            const data = await response.json();
            if (data.success) {
                // Refresh modal to show linked state
                planBtn.click();
            }
        } catch (e) {
            console.error('Failed to link plan:', e);
        }
    }

    // Helper to unlink plan from current repo
    async function unlinkPlan() {
        try {
            const response = await fetch(`/api/plan/link?token=${token}`, { method: 'DELETE' });
            const data = await response.json();
            if (data.success) {
                planBtn.click();
            }
        } catch (e) {
            console.error('Failed to unlink plan:', e);
        }
    }

    // Render plan content with optional unlink button
    function renderPlanContent(data) {
        let header = '';
        if (data.linked) {
            header = `<div class="plan-link-status"><span class="plan-linked-badge">Linked</span> <button class="plan-unlink-btn" id="unlinkPlanBtn">Unpin</button></div>`;
        } else if (data.status === 'found') {
            header = `<div class="plan-link-status"><button class="plan-link-btn" id="linkPlanBtn">Pin to this repo</button></div>`;
        }

        let content;
        try {
            content = marked.parse(data.content);
        } catch (e) {
            content = `<pre>${escapeHtml(data.content)}</pre>`;
        }

        planModalBody.innerHTML = header + content;

        // Wire up link/unlink buttons
        const linkBtn = document.getElementById('linkPlanBtn');
        if (linkBtn) {
            linkBtn.addEventListener('click', () => linkPlan(data.filename));
        }
        const unlinkBtn = document.getElementById('unlinkPlanBtn');
        if (unlinkBtn) {
            unlinkBtn.addEventListener('click', unlinkPlan);
        }
    }

    // Render candidate list for ambiguous state or browse all
    function renderCandidates(candidates, showScore = true, hint = 'Multiple plans match this repo. Select one to pin:') {
        planModalTitle.textContent = 'Select a Plan';
        let html = `<div class="plan-candidates"><p class="plan-candidates-hint">${hint}</p>`;
        for (const c of candidates) {
            const date = new Date(c.modified * 1000).toLocaleDateString();
            const meta = showScore ? `Score: ${c.score} | ${date}` : date;
            const title = c.title || c.filename;
            html += `
                <div class="plan-candidate" data-filename="${escapeHtml(c.filename)}">
                    <div class="plan-candidate-header">
                        <span class="plan-candidate-name">${escapeHtml(title)}</span>
                        <span class="plan-candidate-meta">${meta}</span>
                    </div>
                    <div class="plan-candidate-preview">${escapeHtml(c.preview)}</div>
                </div>
            `;
        }
        html += '</div>';
        planModalBody.innerHTML = html;

        // Wire up candidate clicks
        planModalBody.querySelectorAll('.plan-candidate').forEach(el => {
            el.addEventListener('click', () => {
                linkPlan(el.dataset.filename);
            });
        });
    }

    // Browse all plans
    async function browseAllPlans() {
        planModalBody.innerHTML = '<div class="loading">Loading all plans...</div>';
        try {
            const response = await fetch(`/api/plans?token=${token}`);
            const data = await response.json();
            if (data.plans && data.plans.length > 0) {
                renderCandidates(data.plans, false, 'All plans (tap to pin):');
            } else {
                planModalBody.innerHTML = '<p class="plan-none">No plan files found</p>';
            }
        } catch (e) {
            console.error('Failed to load plans:', e);
            planModalBody.innerHTML = '<p style="color: var(--danger);">Error loading plans</p>';
        }
    }

    // Add browse all button to current content
    function addBrowseAllButton() {
        const existing = planModalBody.querySelector('.plan-browse-all-btn');
        if (existing) return;
        const btn = document.createElement('button');
        btn.className = 'plan-browse-all-btn';
        btn.textContent = 'Browse all plans';
        btn.addEventListener('click', browseAllPlans);
        planModalBody.appendChild(btn);
    }

    planBtn.addEventListener('click', async () => {
        planModal.classList.remove('hidden');
        planModalBody.innerHTML = '<div class="loading">Loading plan...</div>';

        try {
            const response = await fetch(`/api/plan/active?token=${token}&preview=false`);
            const data = await response.json();

            if (data.status === 'found') {
                planModalTitle.textContent = data.filename;
                renderPlanContent(data);
                addBrowseAllButton();
            } else if (data.status === 'ambiguous') {
                renderCandidates(data.candidates);
                addBrowseAllButton();
            } else if (data.status === 'none' && data.fallback) {
                // Show fallback with warning
                planModalTitle.textContent = data.filename + ' (global)';
                planModalBody.innerHTML = `
                    <div class="plan-fallback-warning">No plan matches this repo. Showing most recent:</div>
                    ${marked.parse(data.content)}
                `;
                addBrowseAllButton();
            } else {
                planModalBody.innerHTML = '<p class="plan-none">No active plan found</p>';
                addBrowseAllButton();
            }
        } catch (e) {
            console.error('Failed to load plan:', e);
            planModalBody.innerHTML = '<p style="color: var(--danger);">Error loading plan</p>';
        }
    });

    if (planModalClose) {
        planModalClose.addEventListener('click', () => {
            planModal.classList.add('hidden');
        });
    }

    // Close on backdrop click
    planModal.addEventListener('click', (e) => {
        if (e.target === planModal) {
            planModal.classList.add('hidden');
        }
    });
}

/**
 * Extract last complete Claude message from terminal output
 * Detects message boundaries via prompt (❯) reappearance
 */
function extractLastClaudeMessage(content) {
    // Split by prompt markers to find message boundaries
    // Claude's prompt is ❯, user input follows
    const lines = content.split('\n');

    // Find the last prompt line (indicates Claude finished)
    let lastPromptIdx = -1;
    let secondLastPromptIdx = -1;

    for (let i = lines.length - 1; i >= 0; i--) {
        const line = lines[i].trim();
        // Prompt patterns: "❯ " at start, or line is just "❯"
        if (line.startsWith('❯') || line === '❯') {
            if (lastPromptIdx === -1) {
                lastPromptIdx = i;
            } else {
                secondLastPromptIdx = i;
                break;
            }
        }
    }

    // If we found two prompts, extract content between them (Claude's last message)
    if (secondLastPromptIdx !== -1 && lastPromptIdx !== -1) {
        // Get lines between prompts, skip user input line
        const messageLines = lines.slice(secondLastPromptIdx + 2, lastPromptIdx);
        return messageLines.join('\n');
    }

    // Fallback: return last 1500 chars
    return content.slice(-1500);
}

/**
 * Extract suggestions using hybrid heuristic scoring
 * Returns: { questions: [], commands: [], actions: [], confirmations: [] }
 */
function extractSuggestionsHeuristic(content) {
    const suggestions = {
        questions: [],      // Blocking questions needing response
        commands: [],       // Explicit command suggestions
        actions: [],        // Implied next actions
        confirmations: []   // Yes/no/continue prompts
    };

    // Get last Claude message block
    const lastMessage = extractLastClaudeMessage(content);
    if (!lastMessage || lastMessage.length < 10) return suggestions;

    const lowerMessage = lastMessage.toLowerCase();

    // Split into sentences (rough)
    const sentences = lastMessage.split(/(?<=[.!?])\s+/);

    for (const sentence of sentences) {
        const lower = sentence.toLowerCase().trim();
        if (lower.length < 5) continue;

        let score = 0;
        let type = null;

        // === QUESTIONS (highest priority) ===
        if (sentence.includes('?')) {
            score += 3;
            if (/do you want|should i|would you like|can i|is it okay|shall i/i.test(lower)) {
                score += 3;
                type = 'question';
            }
        }

        // === CONFIRMATIONS ===
        if (/proceed\??|continue\??|ready\??|let me know|when you're ready/i.test(lower)) {
            score += 2;
            type = type || 'confirmation';
        }

        // === EXPLICIT COMMANDS ===
        // Only extract commands that look like actual shell/CLI commands, not code

        // Slash commands (highest confidence) - /compact, /help, etc.
        const slashCmd = sentence.match(/\s(\/[a-z][a-z0-9-]{1,20})\b/i);
        if (slashCmd) {
            suggestions.commands.push(slashCmd[1]);
            score += 2;
        }

        // "Run X", "Try X", "Execute X" - but filter out code patterns
        const runMatch = sentence.match(/(?:run|try|execute)\s+[`"']?([a-z][a-z0-9_-]{1,30})[`"']?/i);
        if (runMatch) {
            const cmd = runMatch[1];
            // Filter out obvious code patterns (function calls, camelCase, etc.)
            if (!/[A-Z]/.test(cmd) && !/\(/.test(cmd) && !/^(the|this|it|that|a|an)$/i.test(cmd)) {
                suggestions.commands.push(cmd);
                score += 2;
            }
        }

        // Backtick commands - but only if they look like CLI commands
        // Must be: lowercase, no parens, no camelCase, short
        const backtickCmd = sentence.match(/`([a-z][a-z0-9 _-]{1,25})`/);
        if (backtickCmd) {
            const cmd = backtickCmd[1];
            // Filter: no parens (not function), no camelCase, no dots
            if (!/[A-Z()\.]/.test(cmd) && cmd.split(' ').length <= 3) {
                suggestions.commands.push(cmd);
            }
        }

        // === IMPLIED ACTIONS ===
        if (/next,?\s|next step|you'll want to|you should|you can now|try\s|test\s|refresh/i.test(lower)) {
            score += 1;
            type = type || 'action';
        }

        // === Add to appropriate category ===
        if (score >= 3 && type) {
            if (type === 'question') {
                suggestions.questions.push(sentence.trim());
            } else if (type === 'confirmation') {
                suggestions.confirmations.push(sentence.trim());
            } else if (type === 'action') {
                suggestions.actions.push(sentence.trim());
            }
        }
    }

    // Dedupe commands
    suggestions.commands = [...new Set(suggestions.commands)].slice(0, 3);

    return suggestions;
}

/**
 * Legacy function for backward compatibility
 */
function extractDynamicSuggestion(content) {
    const suggestions = extractSuggestionsHeuristic(content);

    // Return first command or confirmation response
    if (suggestions.commands.length > 0) {
        return suggestions.commands[0];
    }
    if (suggestions.confirmations.length > 0 || suggestions.questions.length > 0) {
        return 'yes';
    }

    // Fallback: check for common phrases
    const lower = content.toLowerCase().slice(-1000);
    if (lower.includes('refresh') || lower.includes('try it') || lower.includes('test it')) {
        return 'try it';
    }

    return null;
}

/**
 * DEPRECATED: Suggestion extraction has been replaced by tail viewport
 * The tail viewport shows Claude's native suggestions directly
 */
function extractAndShowSuggestions(content) {
    // No-op - tail viewport handles suggestion display now
}

/**
 * Parse terminal capture for Claude's state
 * NOTE: This is now a no-op since updateTailViewport() handles:
 * - Terminal tail display (shows Claude's questions/suggestions natively)
 * - Working indicator updates
 * The old question/suggestion extraction is no longer needed.
 */
async function parseTerminalState() {
    // No longer needed - updateTailViewport() handles everything
    // Kept for backward compatibility with loadTerminalSuggestions alias
}

/**
 * DEPRECATED: Question banner has been replaced by tail viewport
 * The tail viewport shows Claude's native question UI directly
 */
function updateQuestionBanner(content, isWaitingForInput) {
    // No-op - tail viewport handles question display now
}

/**
 * DEPRECATED: Suggestion UI has been replaced by tail viewport
 * The tail viewport shows Claude's native suggestions directly
 */
function updateSuggestion(content) {
    // No-op - tail viewport handles suggestion display now
}

// Alias for backward compatibility
const loadTerminalSuggestions = parseTerminalState;

/**
 * Escape HTML entities
 */
function escapeHtml(text) {
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

/**
 * Start auto-refresh for log view
 */
function startLogAutoRefresh() {
    stopLogAutoRefresh();  // Clear any existing timer
    logAutoRefreshTimer = setInterval(() => {
        if (currentView === 'log') {
            refreshLogContent();
        }
    }, LOG_AUTO_REFRESH_INTERVAL);
}

/**
 * Stop auto-refresh for log view
 */
function stopLogAutoRefresh() {
    if (logAutoRefreshTimer) {
        clearInterval(logAutoRefreshTimer);
        logAutoRefreshTimer = null;
    }
}

// Track last log modified time to avoid unnecessary re-renders
let lastLogModified = 0;
let lastDetectedTurn = null;  // Track last detected turn type for snapshot capture

/**
 * Refresh log content without resetting logLoaded flag
 * (for auto-refresh to get new content)
 */
// Store pending content when user is scrolling
let pendingLogContent = null;

async function refreshLogContent() {
    if (!logContent) return;

    try {
        const response = await fetch(`/api/log?token=${token}`);
        if (!response.ok) return;

        const data = await response.json();
        if (!data.exists || !data.content) return;

        // Only re-render if content actually changed
        if (data.modified && data.modified === lastLogModified) {
            return;  // No change, skip re-render
        }
        lastLogModified = data.modified || 0;

        // Detect turn type for auto-snapshot (before rendering)
        const turnType = detectTurnType(data.content);
        if (turnType && turnType !== lastDetectedTurn) {
            lastDetectedTurn = turnType;
            // Capture snapshot with detected label
            captureSnapshot(turnType);
        }

        // If user is NOT at bottom, don't re-render (would cause scroll jump)
        // Just store the content and show indicator
        if (!userAtBottom) {
            pendingLogContent = data.content;
            showNewContentIndicator();
            return;
        }

        // User is at bottom - safe to re-render
        renderLogEntries(data.content);
        pendingLogContent = null;

        // Load suggestions from terminal capture (not JSONL log)
        loadTerminalSuggestions();
    } catch (error) {
        // Silently fail on auto-refresh
        console.debug('Log auto-refresh failed:', error);
    }
}

/**
 * Detect turn type from log content for auto-snapshot labeling
 * Returns: 'tool_call', 'claude_done', 'error', or null (no significant change)
 */
function detectTurnType(content) {
    if (!content) return null;

    // Get last few lines to detect recent activity
    const lines = content.split('\n').filter(l => l.trim());
    const recentLines = lines.slice(-15).join('\n');

    // Error patterns (highest priority)
    if (/error|Error|ERROR|failed|Failed|FAILED|exception|Exception/.test(recentLines)) {
        // Check if it's actually an error in the output, not just the word
        if (/✗|error:|Error:|failed to|Failed to|exception:/i.test(recentLines)) {
            return 'error';
        }
    }

    // Tool call patterns
    if (/^• (?:Read|Write|Edit|Bash|Grep|Glob|Task|WebFetch|WebSearch)/m.test(recentLines)) {
        return 'tool_call';
    }

    // Claude done patterns (asking user, showing results)
    if (/^❯\s*$|^\[1-9\]|\(y\/n\)|\?$/m.test(recentLines)) {
        return 'claude_done';
    }

    return null;
}

/**
 * Setup log input field for sending commands
 */
function setupLogInput() {
    if (!logInput || !logSend) return;

    // Send on Enter key (multiple event types for mobile compatibility)
    const handleEnter = (e) => {
        if ((e.key === 'Enter' || e.keyCode === 13) && !e.shiftKey) {
            e.preventDefault();
            sendLogCommand();
        }
    };
    logInput.addEventListener('keydown', handleEnter);
    logInput.addEventListener('keypress', handleEnter);

    // Send button - smart mode: send when idle, queue when busy
    logSend.addEventListener('click', () => {
        if (terminalBusy) {
            queueLogCommand();
        } else {
            sendLogCommand();
        }
    });

    // Focus mode: when input is tapped, refresh the active prompt
    logInput.addEventListener('focus', () => {
        refreshActivePrompt();
    });
}

/**
 * Setup quick response buttons (1, 2, 3, yes, no)
 * These send directly to the terminal for fast mobile interaction
 */
function setupQuickResponses() {
    if (!quickResponses) return;

    quickResponses.querySelectorAll('.quick-response-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const value = btn.dataset.value;
            if (value && socket && socket.readyState === WebSocket.OPEN) {
                sendInput(value + '\r');
            }
        });
    });
}

/**
 * Send command from log input to terminal
 * Atomic send: command + carriage return as single write
 */
function sendLogCommand() {
    if (isPreviewMode()) return;  // No input in preview mode
    if (!socket || socket.readyState !== WebSocket.OPEN) return;

    const command = logInput ? logInput.value.trim() : '';

    // If empty, just send Enter (like control bar) for confirming prompts
    if (!command) {
        sendInput('\r');
        // Mark busy after sending non-trivial commands
        setTerminalBusy(true);
        captureSnapshot('user_send');  // Capture state before command
        return;
    }

    // Atomic send: command + carriage return
    sendInput(command + '\r');

    // Mark terminal as busy after sending
    setTerminalBusy(true);
    captureSnapshot('user_send');  // Capture state before command

    // Clear input
    logInput.value = '';
    logInput.dataset.autoSuggestion = 'false';

    // Add to command history
    addToHistory(command);

    // Force refresh log after a short delay
    setTimeout(() => {
        logLoaded = false;
        lastLogModified = 0;  // Force re-fetch
        loadLogContent();
    }, 500);
}

/**
 * Queue command from input box instead of sending directly
 */
function queueLogCommand() {
    const command = logInput ? logInput.value.trim() : '';

    // Enqueue the command (even empty for Enter)
    enqueueCommand(command).then(success => {
        if (success) {
            // Clear input on success
            logInput.value = '';
            logInput.dataset.autoSuggestion = 'false';
            // Add to command history
            if (command) {
                addToHistory(command);
            }
        }
    });
}

/**
 * Setup hybrid view with draggable resize handle (hold-to-drag)
 * NOTE: This is no longer used with the new log/terminal tab architecture
 */
function setupHybridView() {
    // Hybrid view has been replaced with separate log and terminal tabs
    return;

    const HOLD_DELAY = 150;  // ms to hold before drag activates

    let isDragging = false;
    let isHolding = false;
    let holdTimer = null;
    let startY = 0;
    let startLogHeight = 0;

    // Get the hybrid view's available height
    function getAvailableHeight() {
        return hybridView.clientHeight - resizeHandle.offsetHeight;
    }

    // Update section heights based on log height percentage
    function setSectionHeights(logHeightPx) {
        const availableHeight = getAvailableHeight();
        const minHeight = 60;  // Minimum height for either section

        // Clamp log height
        logHeightPx = Math.max(minHeight, Math.min(availableHeight - minHeight, logHeightPx));

        // Apply heights using flex-basis
        const logPercent = (logHeightPx / availableHeight) * 100;
        logSection.style.flex = `0 0 ${logPercent}%`;
        terminalSection.style.flex = `1 1 ${100 - logPercent}%`;
    }

    // Activate drag mode after hold delay
    function activateDrag() {
        isDragging = true;
        isHolding = false;

        // Prevent text selection during drag
        document.body.style.userSelect = 'none';
        document.body.style.webkitUserSelect = 'none';

        // Add active state to handle
        resizeHandle.classList.add('active');

        // Scroll log to bottom when drag starts
        if (logContent) {
            logContent.scrollTop = logContent.scrollHeight;
        }
    }

    // Handle touch/mouse down - start hold timer
    function onPointerDown(e) {
        // Clear any existing timer
        if (holdTimer) clearTimeout(holdTimer);

        isHolding = true;
        startY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
        startLogHeight = logSection.offsetHeight;

        // Start hold timer - drag activates after delay
        holdTimer = setTimeout(() => {
            if (isHolding) {
                activateDrag();
            }
        }, HOLD_DELAY);
    }

    // Handle drag move
    function onPointerMove(e) {
        // If still in hold phase and moved too much, cancel
        if (isHolding && !isDragging) {
            const clientY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
            const deltaY = Math.abs(clientY - startY);
            // If moved more than 10px before hold completes, cancel (probably scrolling)
            if (deltaY > 10) {
                cancelHold();
            }
            return;
        }

        if (!isDragging) return;

        e.preventDefault();
        const clientY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
        const deltaY = clientY - startY;
        const newLogHeight = startLogHeight + deltaY;

        setSectionHeights(newLogHeight);
    }

    // Cancel hold without activating drag
    function cancelHold() {
        if (holdTimer) {
            clearTimeout(holdTimer);
            holdTimer = null;
        }
        isHolding = false;
    }

    // Handle drag end
    function onPointerUp() {
        cancelHold();

        if (!isDragging) return;

        isDragging = false;
        document.body.style.userSelect = '';
        document.body.style.webkitUserSelect = '';
        resizeHandle.classList.remove('active');

        // Enable force scroll flag - this makes the write handler scroll after EACH write
        // This is the key: scrollToBottom() during active writes is ignored,
        // but scrolling AFTER each write completes works
        forceScrollToBottom = true;
        setTimeout(() => { forceScrollToBottom = false; }, 1000);

        // Resize terminal after drag completes
        setTimeout(() => {
            if (fitAddon) fitAddon.fit();
            sendResize();

            // Scroll log to bottom
            if (logContent) {
                logContent.scrollTop = logContent.scrollHeight;
            }
        }, 50);
    }

    // Touch events for mobile
    resizeHandle.addEventListener('touchstart', onPointerDown, { passive: true });
    document.addEventListener('touchmove', onPointerMove, { passive: false });
    document.addEventListener('touchend', onPointerUp);
    document.addEventListener('touchcancel', onPointerUp);

    // Mouse events for desktop
    resizeHandle.addEventListener('mousedown', onPointerDown);
    document.addEventListener('mousemove', onPointerMove);
    document.addEventListener('mouseup', onPointerUp);

    // Header refresh button - refreshes log AND syncs input box
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            // Refresh log content
            logLoaded = false;
            lastLogModified = 0;  // Force re-fetch
            loadLogContent();
            // Sync terminal prompt to input box
            syncPromptToInput();
        });
    }

    // Terminal view refresh button - re-fetches terminal content
    if (terminalRefresh) {
        terminalRefresh.addEventListener('click', async () => {
            terminalRefresh.textContent = 'Refreshing...';
            terminalRefresh.disabled = true;
            try {
                const response = await fetch(`/api/refresh?token=${token}`);
                const data = await response.json();
                if (data.content && term) {
                    // Clear and write fresh content
                    term.clear();
                    term.write(data.content);
                }
            } catch (e) {
                console.error('Terminal refresh failed:', e);
            } finally {
                terminalRefresh.textContent = 'Refresh';
                terminalRefresh.disabled = false;
            }
        });
    }
}


// ================== Queue Functions ==================

// Queue persistence constants
const QUEUE_STORAGE_PREFIX = 'mto_queue_';
const QUEUE_SENDING_TIMEOUT_MS = 30000;  // 30s - stale "sending" becomes "queued"

/**
 * Generate a unique ID for queue items (client-side)
 */
function makeQueueId() {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
        return crypto.randomUUID();
    }
    // Fallback for older browsers
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
}

/**
 * Get localStorage key for queue (scoped to session)
 */
function getQueueStorageKey(session) {
    return QUEUE_STORAGE_PREFIX + (session || 'default');
}

/**
 * Save queue to localStorage
 */
function saveQueueToStorage() {
    if (!currentSession) return;
    try {
        const key = getQueueStorageKey(currentSession);
        const data = {
            items: queueItems,
            savedAt: Date.now()
        };
        localStorage.setItem(key, JSON.stringify(data));
    } catch (e) {
        console.warn('Failed to save queue to storage:', e);
    }
}

/**
 * Load queue from localStorage
 * Converts stale "sending" items back to "queued"
 */
function loadQueueFromStorage() {
    if (!currentSession) return [];
    try {
        const key = getQueueStorageKey(currentSession);
        const raw = localStorage.getItem(key);
        if (!raw) return [];

        const data = JSON.parse(raw);
        const items = data.items || [];
        const now = Date.now();

        // Convert stale "sending" items back to "queued"
        for (const item of items) {
            if (item.status === 'sending') {
                const age = now - (item.lastAttemptAt || item.createdAt || 0);
                if (age > QUEUE_SENDING_TIMEOUT_MS) {
                    item.status = 'queued';
                    item.attempts = (item.attempts || 0);
                }
            }
        }

        // Filter out sent/failed items (they don't need to persist)
        return items.filter(i => i.status === 'queued' || i.status === 'sending');
    } catch (e) {
        console.warn('Failed to load queue from storage:', e);
        return [];
    }
}

/**
 * Reconcile local queue with server state
 * - Server status wins for items on both sides
 * - Local items not on server get re-enqueued (idempotent)
 * - Server items not local get added
 */
async function reconcileQueue() {
    if (!currentSession) return;

    // Load local state first
    const localItems = loadQueueFromStorage();

    // Fetch server state
    let serverItems = [];
    try {
        const resp = await fetch(`/api/queue/list?session=${encodeURIComponent(currentSession)}&token=${token}`);
        if (resp.ok) {
            const data = await resp.json();
            serverItems = data.items || [];
            queuePaused = data.paused || false;
            updatePauseButton();
        }
    } catch (e) {
        console.warn('Failed to fetch server queue for reconciliation:', e);
    }

    // Build ID maps
    const serverMap = new Map(serverItems.map(i => [i.id, i]));
    const localMap = new Map(localItems.map(i => [i.id, i]));

    // Merge: start with server items (authoritative for status)
    const merged = [...serverItems];

    // Add local items not on server (re-enqueue them)
    const toEnqueue = [];
    for (const local of localItems) {
        if (!serverMap.has(local.id)) {
            // Local item missing from server - need to re-enqueue
            toEnqueue.push(local);
        }
    }

    // Re-enqueue missing items (idempotent - server will dedupe by ID)
    for (const item of toEnqueue) {
        try {
            const params = new URLSearchParams({
                session: currentSession,
                text: item.text,
                policy: item.policy || 'auto',
                id: item.id,  // Pass our ID for idempotency
                token: token
            });
            const resp = await fetch(`/api/queue/enqueue?${params}`, { method: 'POST' });
            if (resp.ok) {
                const data = await resp.json();
                if (data.is_new) {
                    merged.push(data.item);
                }
            }
        } catch (e) {
            console.warn('Failed to re-enqueue item:', item.id, e);
            // Keep in local state anyway
            merged.push(item);
        }
    }

    // Update global state
    queueItems = merged;
    saveQueueToStorage();
    renderQueueList();

    console.log(`Queue reconciled: ${serverItems.length} server, ${localItems.length} local, ${toEnqueue.length} re-enqueued`);
}

/**
 * Open unified drawer with Queue tab selected
 */
function openDrawerWithQueueTab() {
    const drawer = document.getElementById('previewDrawer');
    const backdrop = document.getElementById('drawerBackdrop');
    if (drawer) {
        drawer.classList.remove('hidden');
        if (backdrop) backdrop.classList.remove('hidden');
        drawerOpen = true;
        switchRollbackTab('queue');
        refreshQueueList();
    }
}

/**
 * Render queue items in the drawer
 */
function renderQueueList() {
    if (!queueList) return;

    if (queueItems.length === 0) {
        queueList.innerHTML = '<div class="queue-empty">Queue is empty</div>';
        queueCount.textContent = '0';
        updateQueueBadge(0);
        return;
    }

    queueList.innerHTML = queueItems.map(item => {
        const displayText = item.text.length > 40 ? item.text.slice(0, 40) + '...' : item.text;
        const escapedText = displayText.replace(/</g, '&lt;').replace(/>/g, '&gt;');
        return `
            <div class="queue-item" data-id="${item.id}">
                <span class="queue-item-status ${item.status}"></span>
                <div class="queue-item-content">
                    <div class="queue-item-text">${escapedText || '(Enter)'}</div>
                    <div class="queue-item-meta">
                        <span class="queue-item-policy ${item.policy}">${item.policy}</span>
                    </div>
                </div>
                <button class="queue-item-remove" data-id="${item.id}">&times;</button>
            </div>
        `;
    }).join('');

    queueCount.textContent = queueItems.length.toString();
    updateQueueBadge(queueItems.length);

    // Add remove handlers
    queueList.querySelectorAll('.queue-item-remove').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const id = btn.dataset.id;
            removeQueueItem(id);
        });
    });
}

/**
 * Update queue badge visibility and count (both view bar and tab)
 */
function updateQueueBadge(count) {
    // Update view bar badge
    if (queueBadge) {
        if (count > 0) {
            queueBadge.textContent = count.toString();
            queueBadge.classList.remove('hidden');
        } else {
            queueBadge.classList.add('hidden');
        }
    }
    // Update tab badge
    if (queueTabBadge) {
        if (count > 0) {
            queueTabBadge.textContent = count.toString();
            queueTabBadge.classList.remove('hidden');
        } else {
            queueTabBadge.classList.add('hidden');
        }
    }
}

/**
 * Refresh queue list from server
 */
async function refreshQueueList() {
    if (!currentSession) return;

    try {
        const resp = await fetch(`/api/queue/list?session=${encodeURIComponent(currentSession)}&token=${token}`);
        if (resp.ok) {
            const data = await resp.json();
            queueItems = data.items || [];
            queuePaused = data.paused || false;
            updatePauseButton();
            renderQueueList();
        }
    } catch (e) {
        console.error('Failed to refresh queue:', e);
    }
}

/**
 * Enqueue a command
 * Generates client-side ID for idempotency and persists to localStorage
 */
async function enqueueCommand(text, policy = 'auto') {
    if (!currentSession) return false;

    // Generate client-side ID for idempotency
    const itemId = makeQueueId();

    // Create local item immediately (optimistic)
    const localItem = {
        id: itemId,
        text: text,
        policy: policy,
        status: 'queued',
        createdAt: Date.now(),
        attempts: 0
    };

    // Check for duplicate (shouldn't happen, but be safe)
    if (queueItems.some(i => i.id === itemId)) {
        console.warn('Duplicate queue item ID:', itemId);
        return false;
    }

    // Add to local state and persist
    queueItems.push(localItem);
    saveQueueToStorage();
    renderQueueList();

    // Send to server (idempotent)
    try {
        const params = new URLSearchParams({
            session: currentSession,
            text: text,
            policy: policy,
            id: itemId,  // Client-generated ID for idempotency
            token: token
        });
        const resp = await fetch(`/api/queue/enqueue?${params}`, {
            method: 'POST'
        });

        if (resp.ok) {
            const data = await resp.json();
            // Update local item with server response (policy may have been auto-classified)
            const idx = queueItems.findIndex(i => i.id === itemId);
            if (idx >= 0) {
                queueItems[idx] = { ...queueItems[idx], ...data.item };
                saveQueueToStorage();
                renderQueueList();
            }
            return true;
        }
    } catch (e) {
        console.error('Failed to enqueue to server:', e);
        // Item is still in local storage, will be re-synced on reconnect
    }
    return true;  // Return true since item is queued locally
}

/**
 * Remove item from queue
 * Removes from local storage immediately, then syncs with server
 */
async function removeQueueItem(itemId) {
    if (!currentSession) return;

    // Remove from local state immediately
    queueItems = queueItems.filter(item => item.id !== itemId);
    saveQueueToStorage();
    renderQueueList();

    // Sync with server
    try {
        const params = new URLSearchParams({
            session: currentSession,
            item_id: itemId,
            token: token
        });
        await fetch(`/api/queue/remove?${params}`, {
            method: 'POST'
        });
    } catch (e) {
        console.error('Failed to remove queue item from server:', e);
        // Item already removed locally, server will sync on next reconcile
    }
}

/**
 * Toggle pause state
 */
async function toggleQueuePause() {
    if (!currentSession) return;

    const endpoint = queuePaused ? '/api/queue/resume' : '/api/queue/pause';
    const params = new URLSearchParams({
        session: currentSession,
        token: token
    });

    try {
        const resp = await fetch(`${endpoint}?${params}`, {
            method: 'POST'
        });

        if (resp.ok) {
            queuePaused = !queuePaused;
            updatePauseButton();
        }
    } catch (e) {
        console.error('Failed to toggle pause:', e);
    }
}

/**
 * Update pause button text and style
 */
function updatePauseButton() {
    if (!queuePauseBtn) return;
    queuePauseBtn.textContent = queuePaused ? 'Resume' : 'Pause';
    queuePauseBtn.classList.toggle('paused', queuePaused);
}

/**
 * Send next unsafe item manually
 */
async function sendNextUnsafe() {
    if (!currentSession) return;

    try {
        const params = new URLSearchParams({
            session: currentSession,
            token: token
        });
        const resp = await fetch(`/api/queue/send-next?${params}`, {
            method: 'POST'
        });

        if (resp.ok) {
            refreshQueueList();
        }
    } catch (e) {
        console.error('Failed to send next:', e);
    }
}

/**
 * Flush all queue items
 */
async function flushQueue() {
    if (!currentSession) return;

    if (!confirm('Clear all queued commands?')) return;

    try {
        const params = new URLSearchParams({
            session: currentSession,
            confirm: 'true',
            token: token
        });
        const resp = await fetch(`/api/queue/flush?${params}`, {
            method: 'POST'
        });

        if (resp.ok) {
            queueItems = [];
            renderQueueList();
        }
    } catch (e) {
        console.error('Failed to flush queue:', e);
    }
}

/**
 * Handle queue WebSocket messages
 * Persists changes to localStorage
 */
function handleQueueMessage(msg) {
    switch (msg.type) {
        case 'queue_update':
            if (msg.action === 'add') {
                // Idempotent: don't add if already exists
                if (!queueItems.some(i => i.id === msg.item.id)) {
                    queueItems.push(msg.item);
                }
            } else if (msg.action === 'update') {
                const idx = queueItems.findIndex(i => i.id === msg.item.id);
                if (idx >= 0) queueItems[idx] = msg.item;
            } else if (msg.action === 'remove') {
                queueItems = queueItems.filter(i => i.id !== msg.item.id);
            }
            saveQueueToStorage();
            renderQueueList();
            break;

        case 'queue_sent':
            // Item was sent, remove from local state
            queueItems = queueItems.filter(i => i.id !== msg.id);
            saveQueueToStorage();
            renderQueueList();
            break;

        case 'queue_state':
            queuePaused = msg.paused;
            updatePauseButton();
            updateQueueBadge(msg.count);
            break;
    }
}

/**
 * Setup queue event listeners
 */
function setupQueue() {
    if (queuePauseBtn) {
        queuePauseBtn.addEventListener('click', toggleQueuePause);
    }

    if (queueSendNext) {
        queueSendNext.addEventListener('click', sendNextUnsafe);
    }

    if (queueFlush) {
        queueFlush.addEventListener('click', flushQueue);
    }

    // Initial load
    refreshQueueList();
}


// ============================================================================
// PREVIEW MODE FUNCTIONS
// ============================================================================

/**
 * Capture a snapshot (server-side)
 */
async function captureSnapshot(label = 'manual') {
    if (previewMode) return;  // Don't capture while previewing

    try {
        const resp = await fetch(`/api/rollback/preview/capture?label=${label}&token=${token}`, {
            method: 'POST'
        });
        const data = await resp.json();
        console.log('Snapshot capture result:', data);
        return data;
    } catch (e) {
        console.warn('Snapshot capture failed:', e);
        return null;
    }
}

/**
 * Load list of available snapshots
 */
async function loadSnapshotList() {
    try {
        console.log('Loading snapshot list...');
        const resp = await fetch(`/api/rollback/previews?token=${token}`);
        const data = await resp.json();
        console.log('Snapshots response:', data);
        previewSnapshots = data.snapshots || [];
        console.log('Snapshot count:', previewSnapshots.length);
        renderPreviewList();
    } catch (e) {
        console.error('Failed to load snapshots:', e);
    }
}

/**
 * Enter preview mode with a specific snapshot
 */
async function enterPreviewMode(snapId) {
    try {
        // Fetch full snapshot
        const resp = await fetch(`/api/rollback/preview/${snapId}?token=${token}`);
        if (!resp.ok) throw new Error('Snapshot not found');

        previewSnapshot = await resp.json();
        previewMode = snapId;

        // Notify server
        await fetch(`/api/rollback/preview/select?snap_id=${snapId}&token=${token}`, {
            method: 'POST'
        });

        // Render preview state
        renderPreviewLog();
        renderPreviewTerminal();

        // Show banner, disable inputs
        showPreviewBanner();
        disableInputsForPreview();

        // Close drawer
        closePreviewDrawer();

    } catch (e) {
        console.error('Failed to enter preview mode:', e);
        previewMode = null;
        previewSnapshot = null;
    }
}

/**
 * Exit preview mode, return to live
 */
async function exitPreviewMode() {
    previewMode = null;
    previewSnapshot = null;

    // Notify server
    await fetch(`/api/rollback/preview/select?token=${token}`, { method: 'POST' });

    // Hide banner, re-enable inputs
    hidePreviewBanner();
    enableInputsAfterPreview();

    // Refresh live content
    logLoaded = false;
    loadLogContent();
    refreshActivePrompt();
}

/**
 * Render log from snapshot data
 */
function renderPreviewLog() {
    if (!previewSnapshot || !logContent) return;

    // Parse and render log entries from snapshot
    const content = previewSnapshot.log_entries;
    renderLogEntries(content);
}

/**
 * Render terminal from snapshot
 */
function renderPreviewTerminal() {
    if (!previewSnapshot) return;
    const activePromptContent = document.getElementById('activePromptContent');
    if (activePromptContent) {
        activePromptContent.textContent = previewSnapshot.terminal_text || '';
    }
}

/**
 * Show preview mode banner
 */
function showPreviewBanner() {
    const banner = document.getElementById('previewBanner');
    const timestamp = document.getElementById('previewTimestamp');
    if (banner) {
        banner.classList.remove('hidden');
        if (timestamp && previewSnapshot) {
            const date = new Date(previewSnapshot.timestamp);
            timestamp.textContent = date.toLocaleTimeString();
        }
    }
}

/**
 * Hide preview mode banner
 */
function hidePreviewBanner() {
    const banner = document.getElementById('previewBanner');
    if (banner) banner.classList.add('hidden');
}

/**
 * Disable all input controls in preview mode
 */
function disableInputsForPreview() {
    if (logInput) logInput.disabled = true;
    if (logSend) logSend.disabled = true;
    document.querySelectorAll('.quick-btn').forEach(btn => btn.disabled = true);
    document.querySelectorAll('.view-btn').forEach(btn => {
        if (btn.id !== 'previewDrawerBtn') btn.disabled = true;
    });
}

/**
 * Re-enable all input controls
 */
function enableInputsAfterPreview() {
    if (logInput) logInput.disabled = false;
    if (logSend) logSend.disabled = false;
    document.querySelectorAll('.quick-btn').forEach(btn => btn.disabled = false);
    document.querySelectorAll('.view-btn').forEach(btn => btn.disabled = false);
}

/**
 * Check if in preview mode
 */
function isPreviewMode() {
    return previewMode !== null;
}

/**
 * Open unified drawer (defaults to queue tab)
 */
function openDrawer() {
    const drawer = document.getElementById('previewDrawer');
    const backdrop = document.getElementById('drawerBackdrop');
    if (drawer) {
        drawer.classList.remove('hidden');
        if (backdrop) backdrop.classList.remove('hidden');
        drawerOpen = true;
        // Default to queue tab, refresh list
        switchRollbackTab('queue');
    }
}

/**
 * Close drawer
 */
function closePreviewDrawer() {
    const drawer = document.getElementById('previewDrawer');
    const backdrop = document.getElementById('drawerBackdrop');
    if (drawer) drawer.classList.add('hidden');
    if (backdrop) backdrop.classList.add('hidden');
    drawerOpen = false;
}

/**
 * Toggle pin state for a snapshot
 */
async function toggleSnapshotPin(snapId, pinned) {
    try {
        const resp = await fetch(`/api/rollback/preview/${snapId}/pin?pinned=${pinned}&token=${token}`, {
            method: 'POST'
        });
        if (resp.ok) {
            // Update local state and re-render
            const snap = previewSnapshots.find(s => s.id === snapId);
            if (snap) snap.pinned = pinned;
            renderPreviewList();
        }
    } catch (e) {
        console.error('Failed to toggle pin:', e);
    }
}

/**
 * Export snapshot as JSON file
 */
async function exportSnapshot(snapId) {
    try {
        const url = `/api/rollback/preview/${snapId}/export?token=${token}`;
        // Trigger download by opening in new window or using anchor
        const a = document.createElement('a');
        a.href = url;
        a.download = `${snapId}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    } catch (e) {
        console.error('Failed to export snapshot:', e);
    }
}

/**
 * Render the preview list in the drawer with filtering
 */
function renderPreviewList() {
    const list = document.getElementById('previewList');
    if (!list) return;

    // Apply filter
    const filteredSnapshots = previewFilter === 'all'
        ? previewSnapshots
        : previewSnapshots.filter(snap => snap.label === previewFilter);

    if (filteredSnapshots.length === 0) {
        const msg = previewFilter === 'all'
            ? 'No snapshots yet'
            : `No ${previewFilter} snapshots`;
        list.innerHTML = `<div class="preview-empty">${msg}</div>`;
        return;
    }

    list.innerHTML = filteredSnapshots.map(snap => {
        const date = new Date(snap.timestamp);
        const time = date.toLocaleTimeString();
        const isActive = previewMode === snap.id;
        const isPinned = snap.pinned;
        // Friendly label display
        const labelDisplay = getLabelDisplay(snap.label);
        return `
            <div class="preview-list-item ${isActive ? 'active' : ''} ${isPinned ? 'pinned' : ''}" data-snap-id="${snap.id}">
                <button class="preview-pin-btn ${isPinned ? 'pinned' : ''}" title="${isPinned ? 'Unpin' : 'Pin'}">
                    ${isPinned ? '&#x1F4CC;' : '&#x1F4CD;'}
                </button>
                <span class="preview-time">${time}</span>
                <span class="preview-label-badge ${snap.label}">${labelDisplay}</span>
                <button class="preview-export-btn" title="Export JSON">&#x2B07;</button>
                <button class="preview-load-btn">${isActive ? 'Current' : 'Load'}</button>
            </div>
        `;
    }).join('');
}

/**
 * Get friendly display name for snapshot label
 */
function getLabelDisplay(label) {
    const displays = {
        'user_send': 'User',
        'tool_call': 'Tool',
        'claude_done': 'Done',
        'error': 'Error',
        'periodic': 'Auto',
        'manual': 'Manual'
    };
    return displays[label] || label;
}

/**
 * Set preview filter and re-render
 */
function setPreviewFilter(filter) {
    previewFilter = filter;

    // Update filter button states
    document.querySelectorAll('.preview-filter-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.filter === filter);
    });

    renderPreviewList();
}

/**
 * Setup preview event handlers
 */
function setupPreviewHandlers() {
    // Back to live button
    document.getElementById('previewBackToLive')?.addEventListener('click', exitPreviewMode);

    // Drawer close
    document.getElementById('previewDrawerClose')?.addEventListener('click', closePreviewDrawer);

    // Backdrop tap to close drawer
    document.getElementById('drawerBackdrop')?.addEventListener('click', closePreviewDrawer);

    // Drawer open (from view bar)
    drawersBtn?.addEventListener('click', openDrawer);

    // Snapshot button in drawer
    const snapBtn = document.getElementById('snapshotBtn');
    snapBtn?.addEventListener('click', async () => {
        // Visual feedback
        const origText = snapBtn.textContent;
        snapBtn.textContent = 'Saving...';
        snapBtn.disabled = true;

        await captureSnapshot('manual');

        // Refresh list after a short delay
        setTimeout(() => {
            loadSnapshotList();
            snapBtn.textContent = origText;
            snapBtn.disabled = false;
        }, 300);
    });

    // List item clicks (event delegation)
    document.getElementById('previewList')?.addEventListener('click', async (e) => {
        const item = e.target.closest('.preview-list-item');
        if (!item) return;
        const snapId = item.dataset.snapId;
        if (!snapId) return;

        // Handle pin button
        const pinBtn = e.target.closest('.preview-pin-btn');
        if (pinBtn) {
            const isPinned = pinBtn.classList.contains('pinned');
            await toggleSnapshotPin(snapId, !isPinned);
            return;
        }

        // Handle export button
        const exportBtn = e.target.closest('.preview-export-btn');
        if (exportBtn) {
            await exportSnapshot(snapId);
            return;
        }

        // Handle load button
        const loadBtn = e.target.closest('.preview-load-btn');
        if (loadBtn && snapId !== previewMode) {
            enterPreviewMode(snapId);
        }
    });

    // Periodic snapshot capture (every 30s)
    setInterval(() => {
        if (!isPreviewMode()) {
            captureSnapshot('periodic');
        }
    }, 30000);

    // Preview filter buttons
    document.querySelectorAll('.preview-filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const filter = btn.dataset.filter;
            if (filter) setPreviewFilter(filter);
        });
    });

    // Setup tab switching
    document.querySelectorAll('.rollback-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            const tabName = tab.dataset.tab;
            switchRollbackTab(tabName);
        });
    });

    // Git tab handlers
    document.getElementById('gitRefreshBtn')?.addEventListener('click', loadGitCommits);
    document.getElementById('gitBackBtn')?.addEventListener('click', showGitCommitList);
    document.getElementById('gitDryRunBtn')?.addEventListener('click', dryRunRevert);
    document.getElementById('gitRevertBtn')?.addEventListener('click', executeRevert);

    // Git commit list click handler
    document.getElementById('gitCommitList')?.addEventListener('click', (e) => {
        const item = e.target.closest('.git-commit-item');
        if (item) {
            const hash = item.dataset.hash;
            if (hash) showGitCommitDetail(hash);
        }
    });

    // Process tab handlers
    document.getElementById('processRefreshBtn')?.addEventListener('click', loadProcessStatus);
    document.getElementById('processTerminateBtn')?.addEventListener('click', () => terminateProcess(false));
    document.getElementById('processKillBtn')?.addEventListener('click', () => terminateProcess(true));
    document.getElementById('processRespawnBtn')?.addEventListener('click', respawnProcess);
}

// ============================================================================
// GIT TAB FUNCTIONS
// ============================================================================

let gitCommits = [];
let selectedCommitHash = null;
let lastRevertCommit = null;  // The SHA of the revert commit (for undo = revert-the-revert)
let gitStatus = null;  // Current git status (branch, dirty, ahead/behind)
let dryRunValidatedHash = null;  // Commit hash that passed dry-run (safer revert UX)

/**
 * Load git status (branch, dirty, ahead/behind)
 */
async function loadGitStatus() {
    const banner = document.getElementById('gitStatusBanner');
    const statusText = document.getElementById('gitStatusText');

    if (!banner || !statusText) return;

    try {
        const resp = await fetch(`/api/rollback/git/status?token=${token}`);
        gitStatus = await resp.json();

        if (!gitStatus.has_repo) {
            banner.className = 'git-status-banner no-repo';
            statusText.innerHTML = 'No git repository found';
            return;
        }

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
            if (gitStatus.ahead > 0) parts.push(`↑${gitStatus.ahead}`);
            if (gitStatus.behind > 0) parts.push(`↓${gitStatus.behind}`);
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

        // Disable revert button if dirty
        updateRevertButtonState();

    } catch (e) {
        console.error('Failed to load git status:', e);
        banner.className = 'git-status-banner';
        statusText.textContent = 'Error loading status';
    }
}

/**
 * Update revert button enabled state based on git status and dry-run validation
 */
function updateRevertButtonState() {
    const revertBtn = document.getElementById('gitRevertBtn');
    const dryRunBtn = document.getElementById('gitDryRunBtn');

    if (revertBtn) {
        // Revert requires: clean working dir + dry-run passed for this commit
        const isDirty = gitStatus?.is_dirty;
        const hasDryRun = dryRunValidatedHash === selectedCommitHash;

        if (isDirty) {
            revertBtn.disabled = true;
            revertBtn.title = 'Commit or stash changes first';
        } else if (!hasDryRun) {
            revertBtn.disabled = true;
            revertBtn.title = 'Run dry-run first to preview changes';
        } else {
            revertBtn.disabled = false;
            revertBtn.title = '';
        }
    }

    // Dry-run only requires clean working dir
    if (dryRunBtn && gitStatus) {
        dryRunBtn.disabled = gitStatus.is_dirty;
        if (gitStatus.is_dirty) {
            dryRunBtn.title = 'Commit or stash changes first';
        } else {
            dryRunBtn.title = '';
        }
    }
}

/**
 * Switch between drawer tabs (Queue / Runner / Preview / Git / Process)
 */
function switchRollbackTab(tabName) {
    // Update tab buttons
    document.querySelectorAll('.rollback-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.tab === tabName);
    });

    // Update tab content
    const queueContent = document.getElementById('queueTabContent');
    const runnerContent = document.getElementById('runnerTabContent');
    const devContent = document.getElementById('devTabContent');
    const previewContent = document.getElementById('previewTabContent');
    const gitContent = document.getElementById('gitTabContent');
    const processContent = document.getElementById('processTabContent');

    // Hide all tabs
    queueContent?.classList.add('hidden');
    queueContent?.classList.remove('active');
    runnerContent?.classList.add('hidden');
    runnerContent?.classList.remove('active');
    devContent?.classList.add('hidden');
    devContent?.classList.remove('active');
    previewContent?.classList.add('hidden');
    previewContent?.classList.remove('active');
    gitContent?.classList.add('hidden');
    gitContent?.classList.remove('active');
    processContent?.classList.add('hidden');
    processContent?.classList.remove('active');

    // Show selected tab
    if (tabName === 'queue') {
        queueContent?.classList.remove('hidden');
        queueContent?.classList.add('active');
        refreshQueueList();
    } else if (tabName === 'runner') {
        runnerContent?.classList.remove('hidden');
        runnerContent?.classList.add('active');
        loadRunnerCommands();
    } else if (tabName === 'dev') {
        devContent?.classList.remove('hidden');
        devContent?.classList.add('active');
        loadDevPreviewConfig();
    } else if (tabName === 'preview') {
        previewContent?.classList.remove('hidden');
        previewContent?.classList.add('active');
    } else if (tabName === 'git') {
        gitContent?.classList.remove('hidden');
        gitContent?.classList.add('active');
        // Load status and commits when switching to git tab
        loadGitStatus();
        loadGitCommits();
    } else if (tabName === 'process') {
        processContent?.classList.remove('hidden');
        processContent?.classList.add('active');
        loadProcessStatus();
    }
}

/**
 * Load git commits list
 */
async function loadGitCommits() {
    const list = document.getElementById('gitCommitList');
    if (!list) return;

    list.innerHTML = '<div class="git-empty">Loading...</div>';

    try {
        const resp = await fetch(`/api/rollback/git/commits?token=${token}`);
        if (!resp.ok) throw new Error('Failed to load commits');

        const data = await resp.json();
        gitCommits = data.commits || [];
        renderGitCommitList();
    } catch (e) {
        console.error('Failed to load git commits:', e);
        list.innerHTML = '<div class="git-empty">Failed to load commits</div>';
    }
}

/**
 * Render git commits list
 */
function renderGitCommitList() {
    const list = document.getElementById('gitCommitList');
    if (!list) return;

    if (gitCommits.length === 0) {
        list.innerHTML = '<div class="git-empty">No commits found</div>';
        return;
    }

    list.innerHTML = gitCommits.map(commit => `
        <div class="git-commit-item" data-hash="${commit.hash}">
            <span class="git-commit-hash">${commit.hash.substring(0, 7)}</span>
            <span class="git-commit-subject">${escapeHtml(commit.subject)}</span>
            <span class="git-commit-meta">${escapeHtml(commit.author)} &middot; ${commit.date}</span>
        </div>
    `).join('');
}

/**
 * Show git commit list (hide detail)
 */
function showGitCommitList() {
    const list = document.getElementById('gitCommitList');
    const detail = document.getElementById('gitCommitDetail');
    const dryRunResult = document.getElementById('gitDryRunResult');

    list?.classList.remove('hidden');
    detail?.classList.add('hidden');
    dryRunResult?.classList.add('hidden');
    selectedCommitHash = null;
    dryRunValidatedHash = null;  // Reset dry-run validation
}

/**
 * Show git commit detail
 */
async function showGitCommitDetail(hash) {
    const list = document.getElementById('gitCommitList');
    const detail = document.getElementById('gitCommitDetail');
    const content = document.getElementById('gitDetailContent');
    const hashSpan = document.getElementById('gitDetailHash');
    const dryRunResult = document.getElementById('gitDryRunResult');

    if (!detail || !content) return;

    selectedCommitHash = hash;
    dryRunValidatedHash = null;  // Reset dry-run validation for new commit
    list?.classList.add('hidden');
    detail.classList.remove('hidden');
    dryRunResult?.classList.add('hidden');

    // Update button states (revert disabled until dry-run passes)
    updateRevertButtonState();

    if (hashSpan) hashSpan.textContent = hash.substring(0, 7);
    content.innerHTML = '<div class="git-empty">Loading...</div>';

    try {
        const resp = await fetch(`/api/rollback/git/commit/${hash}?token=${token}`);
        if (!resp.ok) throw new Error('Failed to load commit');

        const data = await resp.json();

        content.innerHTML = `
            <div class="git-detail-subject">${escapeHtml(data.subject)}</div>
            ${data.body ? `<div class="git-detail-body">${escapeHtml(data.body)}</div>` : ''}
            <div class="git-detail-meta">
                <strong>Author:</strong> ${escapeHtml(data.author)}<br>
                <strong>Date:</strong> ${escapeHtml(data.date)}
            </div>
            ${data.stat ? `<div class="git-detail-stat">${escapeHtml(data.stat)}</div>` : ''}
        `;
    } catch (e) {
        console.error('Failed to load commit detail:', e);
        content.innerHTML = '<div class="git-empty">Failed to load commit details</div>';
    }
}

/**
 * Dry run revert for selected commit
 */
async function dryRunRevert() {
    if (!selectedCommitHash) return;

    const dryRunBtn = document.getElementById('gitDryRunBtn');
    const dryRunResult = document.getElementById('gitDryRunResult');

    if (!dryRunBtn || !dryRunResult) return;

    dryRunBtn.disabled = true;
    dryRunBtn.textContent = 'Checking...';
    dryRunResult.classList.remove('hidden', 'success', 'error');
    dryRunResult.innerHTML = '<pre>Running dry-run...</pre>';

    try {
        const resp = await fetch(`/api/rollback/git/revert/dry-run?commit_hash=${selectedCommitHash}&token=${token}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (data.success) {
            dryRunResult.classList.add('success');
            dryRunResult.innerHTML = `<pre>${escapeHtml(data.message)}\n\n${escapeHtml(data.changes || 'No changes')}</pre>`;
            // Mark this commit as validated - enables Revert button
            dryRunValidatedHash = selectedCommitHash;
        } else {
            dryRunResult.classList.add('error');
            dryRunResult.innerHTML = `<pre>Error: ${escapeHtml(data.error || 'Unknown error')}\n${escapeHtml(data.details || '')}</pre>`;
            dryRunValidatedHash = null;  // Clear validation on failure
        }
    } catch (e) {
        console.error('Dry run failed:', e);
        dryRunResult.classList.add('error');
        dryRunResult.innerHTML = `<pre>Error: ${e.message}</pre>`;
        dryRunValidatedHash = null;  // Clear validation on error
    } finally {
        dryRunBtn.disabled = false;
        dryRunBtn.textContent = 'Dry Run';
        updateRevertButtonState();  // Update Revert button state
    }
}

/**
 * Execute revert for selected commit
 */
async function executeRevert() {
    if (!selectedCommitHash) return;

    if (!confirm(`Are you sure you want to revert commit ${selectedCommitHash.substring(0, 7)}?\n\nThis will create a new commit that undoes the changes.`)) {
        return;
    }

    const revertBtn = document.getElementById('gitRevertBtn');
    const dryRunResult = document.getElementById('gitDryRunResult');

    if (!revertBtn || !dryRunResult) return;

    revertBtn.disabled = true;
    revertBtn.textContent = 'Reverting...';
    dryRunResult.classList.remove('hidden', 'success', 'error');
    dryRunResult.innerHTML = '<pre>Executing revert...</pre>';

    try {
        const resp = await fetch(`/api/rollback/git/revert/execute?commit_hash=${selectedCommitHash}&token=${token}&${getTargetParams()}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (data.success) {
            lastRevertCommit = data.new_commit;  // Store revert commit SHA for undo
            dryRunValidatedHash = null;  // Clear validation after successful revert
            dryRunResult.classList.add('success');
            dryRunResult.innerHTML = `<pre>Revert successful!\nNew commit: ${data.new_commit.substring(0, 7)}\n\nTo undo, click "Undo Revert" (creates another revert commit).</pre>
                <button id="gitUndoBtn" class="git-action-btn secondary" style="margin-top: 8px;">Undo Revert</button>`;
            showToast('Revert successful', 'success');

            // Add undo handler
            document.getElementById('gitUndoBtn')?.addEventListener('click', undoRevert);

            // Refresh commits list
            loadGitCommits();
        } else {
            dryRunResult.classList.add('error');
            dryRunResult.innerHTML = `<pre>Error: ${escapeHtml(data.error || 'Unknown error')}</pre>`;
        }
    } catch (e) {
        console.error('Revert failed:', e);
        dryRunResult.classList.add('error');
        dryRunResult.innerHTML = `<pre>Error: ${e.message}</pre>`;
    } finally {
        revertBtn.textContent = 'Revert';
        updateRevertButtonState();  // Update button state (will disable since dry-run cleared)
    }
}

/**
 * Undo the last revert (by reverting the revert commit - non-destructive)
 */
async function undoRevert() {
    if (!lastRevertCommit) return;

    if (!confirm('Are you sure you want to undo the revert?\n\nThis will create a new commit that undoes the revert (revert-the-revert).')) {
        return;
    }

    const dryRunResult = document.getElementById('gitDryRunResult');
    if (!dryRunResult) return;

    dryRunResult.innerHTML = '<pre>Undoing revert...</pre>';

    try {
        const resp = await fetch(`/api/rollback/git/revert/undo?revert_commit=${lastRevertCommit}&token=${token}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (data.success) {
            dryRunResult.classList.remove('error');
            dryRunResult.classList.add('success');
            dryRunResult.innerHTML = `<pre>Undo successful!\nCreated commit: ${data.new_commit.substring(0, 7)}</pre>`;
            lastRevertCommit = null;
            showToast('Undo successful', 'success');

            // Refresh commits list and status
            loadGitStatus();
            loadGitCommits();
        } else {
            dryRunResult.classList.add('error');
            dryRunResult.innerHTML = `<pre>Error: ${escapeHtml(data.error || 'Unknown error')}\n${escapeHtml(data.details || '')}</pre>`;
        }
    } catch (e) {
        console.error('Undo failed:', e);
        dryRunResult.classList.add('error');
        dryRunResult.innerHTML = `<pre>Error: ${e.message}</pre>`;
    }
}

// ============================================================================
// PROCESS MANAGEMENT
// ============================================================================

let processStatus = null;

/**
 * Load process status
 */
async function loadProcessStatus() {
    const banner = document.getElementById('processStatusBanner');
    const statusText = document.getElementById('processStatusText');

    if (!banner || !statusText) return;

    try {
        const resp = await fetch(`/api/process/status?token=${token}`);
        processStatus = await resp.json();

        let html = '';
        if (processStatus.is_running) {
            html = `<span class="process-status-running">Running</span> PID: ${processStatus.pid}`;
            if (processStatus.session) {
                html += ` | Session: ${escapeHtml(processStatus.session)}`;
            }
            banner.className = 'process-status-banner running';
        } else if (processStatus.pid) {
            html = `<span class="process-status-dead">Dead</span> (was PID: ${processStatus.pid})`;
            banner.className = 'process-status-banner dead';
        } else {
            html = '<span class="process-status-none">No process</span>';
            banner.className = 'process-status-banner none';
        }

        statusText.innerHTML = html;
        updateProcessButtons();

    } catch (e) {
        console.error('Failed to load process status:', e);
        banner.className = 'process-status-banner';
        statusText.textContent = 'Error loading status';
    }
}

/**
 * Update process button states based on status
 */
function updateProcessButtons() {
    const terminateBtn = document.getElementById('processTerminateBtn');
    const killBtn = document.getElementById('processKillBtn');
    const respawnBtn = document.getElementById('processRespawnBtn');

    const isRunning = processStatus?.is_running;

    if (terminateBtn) {
        terminateBtn.disabled = !isRunning;
        terminateBtn.title = isRunning ? '' : 'No process running';
    }
    if (killBtn) {
        killBtn.disabled = !isRunning;
        killBtn.title = isRunning ? '' : 'No process running';
    }
    if (respawnBtn) {
        respawnBtn.disabled = false;  // Always available
    }
}

/**
 * Terminate process with SIGTERM
 */
async function terminateProcess(force = false) {
    const resultDiv = document.getElementById('processResult');
    if (!resultDiv) return;

    const action = force ? 'force kill' : 'terminate';
    if (!confirm(`Are you sure you want to ${action} the process?\n\nThis will end the current PTY session.`)) {
        return;
    }

    resultDiv.classList.remove('hidden', 'success', 'error');
    resultDiv.innerHTML = `<pre>${force ? 'Force killing' : 'Terminating'}...</pre>`;

    try {
        const resp = await fetch(`/api/process/terminate?token=${token}&force=${force}&${getTargetParams()}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (data.success) {
            resultDiv.classList.add('success');
            resultDiv.innerHTML = `<pre>Process terminated (${data.method})\nPID: ${data.pid}</pre>`;
            showToast('Process terminated', 'success');
        } else {
            resultDiv.classList.add('error');
            resultDiv.innerHTML = `<pre>Error: ${escapeHtml(data.error)}</pre>`;
        }
    } catch (e) {
        console.error('Terminate failed:', e);
        resultDiv.classList.add('error');
        resultDiv.innerHTML = `<pre>Error: ${e.message}</pre>`;
    }

    // Refresh status
    await loadProcessStatus();
}

/**
 * Respawn the PTY process
 */
async function respawnProcess() {
    const resultDiv = document.getElementById('processResult');
    if (!resultDiv) return;

    if (!confirm('Are you sure you want to respawn the process?\n\nThis will terminate the current session (if any) and create a new one.')) {
        return;
    }

    resultDiv.classList.remove('hidden', 'success', 'error');
    resultDiv.innerHTML = '<pre>Respawning...</pre>';

    try {
        const resp = await fetch(`/api/process/respawn?token=${token}&${getTargetParams()}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (data.success) {
            resultDiv.classList.add('success');
            let msg = `Process respawned!\nNew PID: ${data.new_pid}\nSession: ${data.session}`;
            if (data.old_pid) {
                msg = `Old PID: ${data.old_pid}\n` + msg;
            }
            resultDiv.innerHTML = `<pre>${escapeHtml(msg)}</pre>`;
            showToast('Process respawned', 'success');

            // Trigger reconnect to pick up new process
            setTimeout(() => {
                if (socket) {
                    socket.close();
                }
            }, 500);
        } else {
            resultDiv.classList.add('error');
            resultDiv.innerHTML = `<pre>Error: ${escapeHtml(data.error)}</pre>`;
        }
    } catch (e) {
        console.error('Respawn failed:', e);
        resultDiv.classList.add('error');
        resultDiv.innerHTML = `<pre>Error: ${e.message}</pre>`;
    }

    // Refresh status
    await loadProcessStatus();
}

// ============================================================================
// END PROCESS MANAGEMENT
// ============================================================================

// ============================================================================
// RUNNER (QUICK COMMANDS)
// ============================================================================

let runnerCommands = null;

/**
 * Load available runner commands
 */
async function loadRunnerCommands() {
    const container = document.getElementById('runnerCommands');
    if (!container) return;

    // Use cached commands if available
    if (runnerCommands) {
        renderRunnerCommands();
        return;
    }

    container.innerHTML = '<div class="runner-loading">Loading commands...</div>';

    try {
        const resp = await fetch(`/api/runner/commands?token=${token}`);
        const data = await resp.json();
        runnerCommands = data.commands;
        renderRunnerCommands();
    } catch (e) {
        console.error('Failed to load runner commands:', e);
        container.innerHTML = '<div class="runner-error">Failed to load commands</div>';
    }
}

/**
 * Render runner command buttons
 */
function renderRunnerCommands() {
    const container = document.getElementById('runnerCommands');
    if (!container || !runnerCommands) return;

    container.innerHTML = Object.entries(runnerCommands).map(([id, cmd]) => {
        return `
            <button class="runner-cmd-btn" data-cmd-id="${id}" title="${escapeHtml(cmd.description)}">
                <span class="runner-cmd-icon">${cmd.icon}</span>
                <span class="runner-cmd-label">${escapeHtml(cmd.label)}</span>
            </button>
        `;
    }).join('');

    // Add click handlers
    container.querySelectorAll('.runner-cmd-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const cmdId = btn.dataset.cmdId;
            if (cmdId) executeRunnerCommand(cmdId);
        });
    });
}

/**
 * Execute a runner command
 */
async function executeRunnerCommand(commandId) {
    try {
        const resp = await fetch(`/api/runner/execute?command_id=${commandId}&token=${token}&${getTargetParams()}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (data.success) {
            showToast(`Running: ${data.label}`, 'success');
            // Close drawer and switch to terminal view to see output
            closePreviewDrawer();
            if (currentView !== 'terminal') {
                switchToTerminalView();
            }
        } else {
            showToast(`Error: ${data.error}`, 'error');
        }
    } catch (e) {
        console.error('Runner execute failed:', e);
        showToast(`Error: ${e.message}`, 'error');
    }
}

/**
 * Execute custom runner command
 */
async function executeCustomCommand() {
    const input = document.getElementById('runnerCustomInput');
    if (!input) return;

    const command = input.value.trim();
    if (!command) return;

    try {
        const resp = await fetch(`/api/runner/custom?command=${encodeURIComponent(command)}&token=${token}&${getTargetParams()}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (data.success) {
            showToast('Command sent', 'success');
            input.value = '';
            // Close drawer and switch to terminal view
            closePreviewDrawer();
            if (currentView !== 'terminal') {
                switchToTerminalView();
            }
        } else {
            showToast(`Error: ${data.error}`, 'error');
        }
    } catch (e) {
        console.error('Custom command failed:', e);
        showToast(`Error: ${e.message}`, 'error');
    }
}

/**
 * Setup runner event handlers
 */
function setupRunnerHandlers() {
    // Custom command button
    document.getElementById('runnerCustomBtn')?.addEventListener('click', executeCustomCommand);

    // Custom command input Enter key
    document.getElementById('runnerCustomInput')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            executeCustomCommand();
        }
    });
}

// ============================================================================
// END RUNNER
// ============================================================================

// ============================================================================
// DEV PREVIEW
// ============================================================================

let devPreviewConfig = null;
let devPreviewStatus = {};  // { serviceId: { status, latency } }
let activeDevService = null;
let devStatusTimer = null;
const DEV_STATUS_POLL_INTERVAL = 5000;

/**
 * Load preview config for current repo
 */
async function loadDevPreviewConfig() {
    try {
        const resp = await fetch(`/api/preview/config?token=${token}`);
        const data = await resp.json();
        devPreviewConfig = data.exists ? data : null;
        renderDevServices();
        if (devPreviewConfig && devPreviewConfig.services?.length) {
            startDevStatusPolling();
        } else {
            stopDevStatusPolling();
        }
    } catch (e) {
        console.error('Failed to load preview config:', e);
        devPreviewConfig = null;
        renderDevServices();
    }
}

/**
 * Render service tabs with status dots
 */
function renderDevServices() {
    const container = document.getElementById('devServiceTabs');
    const banner = document.getElementById('devStatusBanner');
    const statusText = document.getElementById('devStatusText');

    if (!devPreviewConfig || !devPreviewConfig.services?.length) {
        if (statusText) statusText.textContent = 'No services configured';
        if (container) container.innerHTML = '<div class="dev-empty">Add preview.config.json to enable</div>';
        updateDevControls(false);
        return;
    }

    if (statusText) {
        const runningCount = Object.values(devPreviewStatus).filter(s => s.status === 'running').length;
        statusText.textContent = `${runningCount}/${devPreviewConfig.services.length} running`;
    }

    if (container) {
        container.innerHTML = devPreviewConfig.services.map(svc => {
            const status = devPreviewStatus[svc.id]?.status || 'unknown';
            const isActive = activeDevService === svc.id;
            return `
                <button class="dev-service-tab ${isActive ? 'active' : ''}" data-service-id="${svc.id}">
                    <span class="dev-status-dot ${status}"></span>
                    <span class="dev-service-label">${escapeHtml(svc.label)}</span>
                    <span class="dev-service-port">:${svc.port}</span>
                </button>
            `;
        }).join('');

        // Click handlers
        container.querySelectorAll('.dev-service-tab').forEach(btn => {
            btn.addEventListener('click', () => selectDevService(btn.dataset.serviceId));
        });
    }

    updateDevControls(!!activeDevService);
}

/**
 * Update control button states
 */
function updateDevControls(enabled) {
    const startBtn = document.getElementById('devStartBtn');
    const restartBtn = document.getElementById('devRestartBtn');
    const stopBtn = document.getElementById('devStopBtn');
    const openBtn = document.getElementById('devOpenBtn');
    const copyBtn = document.getElementById('devCopyBtn');

    [startBtn, restartBtn, stopBtn, openBtn, copyBtn].forEach(btn => {
        if (btn) btn.disabled = !enabled;
    });
}

/**
 * Select a service and load its preview
 */
function selectDevService(serviceId) {
    activeDevService = serviceId;
    const svc = devPreviewConfig?.services?.find(s => s.id === serviceId);
    if (!svc) return;

    renderDevServices();  // Update active state

    const frame = document.getElementById('devPreviewFrame');
    const placeholder = document.getElementById('devPreviewPlaceholder');
    const status = devPreviewStatus[serviceId]?.status;

    if (status === 'running') {
        const url = buildDevPreviewUrl(svc);
        if (frame) {
            frame.src = url;
            frame.classList.remove('hidden');
        }
        if (placeholder) placeholder.classList.add('hidden');
    } else {
        if (frame) {
            frame.src = 'about:blank';
            frame.classList.add('hidden');
        }
        if (placeholder) {
            placeholder.classList.remove('hidden');
            placeholder.textContent = status === 'stopped'
                ? `${svc.label} is not running`
                : `${svc.label} status: ${status || 'unknown'}`;
        }
    }
}

/**
 * Build preview URL for service (using Tailscale config or localhost fallback)
 */
function buildDevPreviewUrl(service) {
    if (devPreviewConfig?.tailscaleServe?.urlPattern) {
        return devPreviewConfig.tailscaleServe.urlPattern
            .replace('{hostname}', devPreviewConfig.tailscaleServe.hostname || '')
            .replace('{port}', service.port);
    }
    // Fallback to localhost (works if on same network)
    const path = service.path || '/';
    return `http://localhost:${service.port}${path}`;
}

/**
 * Poll service status periodically
 */
function startDevStatusPolling() {
    if (devStatusTimer) clearInterval(devStatusTimer);
    refreshDevStatus();
    devStatusTimer = setInterval(refreshDevStatus, DEV_STATUS_POLL_INTERVAL);
}

function stopDevStatusPolling() {
    if (devStatusTimer) {
        clearInterval(devStatusTimer);
        devStatusTimer = null;
    }
}

async function refreshDevStatus() {
    try {
        const resp = await fetch(`/api/preview/status?token=${token}`);
        const data = await resp.json();
        devPreviewStatus = {};
        data.services?.forEach(s => {
            devPreviewStatus[s.id] = { status: s.status, latency: s.latency };
        });
        renderDevServices();

        // If active service just became running, reload iframe
        if (activeDevService) {
            const status = devPreviewStatus[activeDevService]?.status;
            const frame = document.getElementById('devPreviewFrame');
            if (status === 'running' && frame && frame.classList.contains('hidden')) {
                selectDevService(activeDevService);
            }
        }
    } catch (e) {
        console.error('Dev status check failed:', e);
    }
}

/**
 * Start the active preview service
 */
async function startDevService() {
    if (!activeDevService) {
        showToast('Select a service first', 'error');
        return;
    }
    const svc = devPreviewConfig?.services?.find(s => s.id === activeDevService);
    if (!svc?.startCommand) {
        showToast('No start command configured', 'error');
        return;
    }

    try {
        const resp = await fetch(`/api/preview/start?service_id=${activeDevService}&token=${token}&${getTargetParams()}`, {
            method: 'POST'
        });
        const data = await resp.json();
        if (data.success) {
            showToast(`Starting ${svc.label}...`, 'success');
            closePreviewDrawer();
            switchToTerminalView();
        } else {
            showToast(`Error: ${data.error || data.message}`, 'error');
        }
    } catch (e) {
        showToast(`Error: ${e.message}`, 'error');
    }
}

/**
 * Stop the active preview service (sends Ctrl+C)
 */
async function stopDevService() {
    if (!activeDevService) {
        showToast('Select a service first', 'error');
        return;
    }

    try {
        const resp = await fetch(`/api/preview/stop?service_id=${activeDevService}&token=${token}&${getTargetParams()}`, {
            method: 'POST'
        });
        const data = await resp.json();
        if (data.success) {
            showToast('Sent stop signal', 'success');
            setTimeout(refreshDevStatus, 1000);
        } else {
            showToast(`Error: ${data.error || data.message}`, 'error');
        }
    } catch (e) {
        showToast(`Error: ${e.message}`, 'error');
    }
}

/**
 * Restart the active preview service
 */
async function restartDevService() {
    await stopDevService();
    setTimeout(startDevService, 1500);
}

/**
 * Open preview in new tab
 */
function openDevPreview() {
    const svc = devPreviewConfig?.services?.find(s => s.id === activeDevService);
    if (svc) {
        window.open(buildDevPreviewUrl(svc), '_blank');
    }
}

/**
 * Copy preview URL to clipboard
 */
function copyDevPreviewUrl() {
    const svc = devPreviewConfig?.services?.find(s => s.id === activeDevService);
    if (svc) {
        const url = buildDevPreviewUrl(svc);
        navigator.clipboard.writeText(url).then(() => {
            showToast('URL copied', 'success');
        }).catch(() => {
            showToast('Copy failed', 'error');
        });
    }
}

/**
 * Setup Dev Preview event handlers
 */
function setupDevPreview() {
    document.getElementById('devStartBtn')?.addEventListener('click', startDevService);
    document.getElementById('devStopBtn')?.addEventListener('click', stopDevService);
    document.getElementById('devRestartBtn')?.addEventListener('click', restartDevService);
    document.getElementById('devOpenBtn')?.addEventListener('click', openDevPreview);
    document.getElementById('devCopyBtn')?.addEventListener('click', copyDevPreviewUrl);
}

// ============================================================================
// END DEV PREVIEW
// ============================================================================

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Show a toast notification
 * @param {string} message - The message to display
 * @param {string} type - 'success', 'error', or 'info'
 * @param {number} duration - How long to show (ms), default 3000
 */
function showToast(message, type = 'success', duration = 3000) {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    // Auto-remove after duration
    setTimeout(() => {
        toast.style.animation = 'toastFadeOut 0.3s ease-out forwards';
        setTimeout(() => toast.remove(), 300);
    }, duration);
}


// Initialize
document.addEventListener('DOMContentLoaded', async () => {
    initDOMElements();

    // Initialize terminal (but it starts hidden in terminal tab)
    initTerminal();

    // Setup quick response buttons
    setupQuickResponses();

    // Hide control bars initially (view mode)
    controlBarsContainer.classList.add('hidden');

    setupEventListeners();
    setupTerminalFocus();
    setupViewportHandler();
    setupClipboard();
    setupRepoDropdown();
    setupTargetSelector();
    setupFileSearch();
    setupJumpToBottom();
    setupCopyButton();
    setupCommandHistory();
    setupComposeMode();
    setupChallenge();
    setupViewToggle();
    setupSwipeNavigation();
    setupTranscriptSearch();
    startActivityUpdates();
    setupQueue();
    setupCollapseHandler();
    setupSuperCollapseHandler();
    setupScrollTracking();
    setupPlanPreviewHandler();
    setupPlanButton();
    setupPreviewHandlers();
    setupRunnerHandlers();
    setupDevPreview();

    // Scroll input bar to the right so Enter button is visible
    if (inputBar) {
        inputBar.scrollLeft = inputBar.scrollWidth;
    }

    // Load current session first, then config
    await loadCurrentSession();
    await loadConfig();

    // Load local queue and reconcile with server
    await reconcileQueue();

    connect();

    // Start with log view as primary
    switchToLogView();
});
