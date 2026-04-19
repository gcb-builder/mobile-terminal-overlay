/**
 * Mobile Terminal Overlay - Terminal Client
 *
 * Connects xterm.js to the WebSocket backend for tmux relay.
 */

import { abortableSleep, findSafeBoundary, formatFileSize, cleanTerminalOutput,
         stripAnsi, escapeRegExp, classifyLogEntry, yieldToMain, escapeHtml,
         shellSplit, formatTimeAgo } from './src/utils.js';
import { deriveUIState, deriveSystemSummary } from './src/ui-state.js';
import ctx from './src/context.js';
import { initMcp, loadMcp, loadPluginsTab } from './src/features/mcp.js';
import { initEnv, loadEnv } from './src/features/env.js';
import { initCollapse, scheduleCollapse, scheduleSuperCollapse, applyCollapseSync } from './src/features/collapse.js';
import { initQueue, renderQueueList, handleQueueMessage, enqueueCommand,
         reconcileQueue, reloadQueueForTarget, refreshQueueList,
         getQueueItems, isQueuePaused, saveQueueToStorage,
         popNextQueueItem, requeueItem } from './src/features/queue.js';
import { initBacklog, handleBacklogMessage, handleCandidateMessage,
         refreshBacklogList, reloadBacklogForProject, addBacklogItem,
         updateBacklogStatus } from './src/features/backlog.js';
import { initPermissions, loadPermissions } from './src/features/permissions.js';
import { initMarkdown, scheduleMarkdownParse, schedulePlanPreviews } from './src/features/markdown.js';
import { initDocs } from './src/features/docs.js';
import { initToolOutput } from './src/features/tool-output.js';
import { initHistory, loadHistory, loadGitStatus } from './src/features/history.js';
import { initTeam, activateTeamView, startTeamCardRefresh, stopTeamCardRefresh,
         refreshTeamCards, renderTeamCards, updateTerminalAgentSelector,
         sendTeamInput, selectNextAgent, selectPrevAgent, approveSelectedAgent,
         denySelectedAgent, openSelectedAgentTerminal, focusSearchInput,
         getLastSystemSummary, scrollToFirstAttention, populateDispatchPlans,
         setupTeamFilters, applyDensity, getTeamDensity,
         showLaunchTeamModal } from './src/features/team.js';
import { initPalette, openPalette, closePalette } from './src/features/palette.js';
import { initActivity, loadActivity, stopActivity } from './src/features/activity.js';

// Init order (runtime-sensitive):
// 1. DOM refs available (DOMContentLoaded)
// 2. ctx fields populated (token, config, session, etc.)
// 3. Feature init functions bind listeners (initMcp, initEnv, etc.)
// 4. Core terminal/socket init (connect, setupCommandHistory)
// 5. Initial load of active tab/view

// VERSION DIAGNOSTIC - if you see this in console, browser has v247 code
console.log('=== TERMINAL.JS v286 ===');
console.log('Mode epoch system active: stale writes will be cancelled');
console.log('SSE fallback transport available');

// Global error boundary for debugging
window.addEventListener('error', (e) => {
    console.error('Global error:', e.error || e.message);
});
window.addEventListener('unhandledrejection', (e) => {
    console.error('Unhandled rejection:', e.reason);
});

// Get ctx.token from URL (may be null if --no-auth)
const urlParams = new URLSearchParams(window.location.search);
ctx.token = urlParams.get('token') || '';
const paramFontSize = urlParams.get('font_size');
const paramPhysicalKb = urlParams.get('physical_kb');
// Physical keyboard flag: URL param is immediate, server ctx.config updates after load
let isPhysicalKb = paramPhysicalKb === '1';

// Persistent client ID for request tracking (helps debug duplicate requests)
ctx.clientId = sessionStorage.getItem('mto_client_id') || crypto.randomUUID();
sessionStorage.setItem('mto_client_id', ctx.clientId);
console.log('Client ID:', ctx.clientId.slice(0, 8));

// Standard headers for all API requests
// Token sent via header (preferred) in addition to query string (backward compat)
function apiHeaders(extra = {}) {
    const h = { 'X-Client-ID': ctx.clientId, ...extra };
    if (ctx.token) h['X-MTO-Token'] = ctx.token;
    return h;
}

// Helper for fetch with auth + client ID headers
function apiFetch(url, options = {}) {
    const headers = apiHeaders(options.headers);
    return fetch(url, { ...options, headers });
}
ctx.apiFetch = apiFetch;

// Fetch with timeout using AbortController
// Prevents indefinite hangs on slow/unresponsive endpoints
async function fetchWithTimeout(url, options = {}, timeoutMs = 10000) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    try {
        const response = await fetch(url, {
            ...options,
            signal: controller.signal,
            headers: apiHeaders(options.headers)
        });
        return response;
    } finally {
        clearTimeout(timeoutId);
    }
}

// Singleflight polling infrastructure
// AbortController-based async loops that can be cancelled on view switch
// abortableSleep — moved to src/utils.js

// State
// ctx.terminal initialized by context.js (null)
// ctx.socket initialized by context.js (null)
let interactiveMode = false;   // Terminal keyboard passthrough (off by default)
let interactiveIdleTimer = null;  // Auto-disable after inactivity
// ctx.config initialized by context.js (null)
// ctx.currentSession initialized by context.js (null)

// Output mode: 'tail' (default) or 'full'
// In tail mode: server sends rate-limited text snapshots (no xterm rendering)
// In full mode: server sends raw PTY bytes (xterm renders everything)
// ctx.outputMode initialized by context.js ('tail')

// Mode epoch: incremented on every mode change to invalidate in-flight operations
// This prevents stale data from being written after mode switches
// ctx.modeEpoch initialized by context.js (0)

// Buffered xterm writes with hard backlog cap and chunk splitting
let writeQueue = [];  // Array of {data: Uint8Array, epoch: number}
let queuedBytes = 0;
let draining = false;
let isResyncing = false;
let lastResyncTime = 0;
const RESYNC_COOLDOWN = 5000;  // Don't resync more than once per 5 seconds

// Backlog limits (tuned for mobile)
const MAX_QUEUE_BYTES = 200000;  // 200KB max backlog
const MAX_QUEUE_ITEMS = 500;     // 500 chunks max
const MAX_PER_FRAME_MS = 8;      // 8ms max per frame
const CHUNK_SIZE = 8192;         // 8KB max per ctx.terminal.write() call (larger to avoid ANSI splits)

// Encoder for converting strings to bytes (used once, reused)
const _textEncoder = new TextEncoder();

// findSafeBoundary — moved to src/utils.js

/**
 * Enqueue binary data for ctx.terminal rendering.
 * BYTES ONLY - all data must be Uint8Array.
 * Tagged with epoch to allow cancellation on mode change.
 */
function enqueueSplit(data, epoch) {
    if (isResyncing) return;
    if (epoch !== ctx.modeEpoch) return;  // Stale data from old mode

    // Only accept Uint8Array - bytes all the way
    if (!(data instanceof Uint8Array)) {
        console.warn('[QUEUE] Expected Uint8Array, got:', typeof data);
        return;
    }

    // Split into chunks at safe boundaries
    let offset = 0;
    while (offset < data.length) {
        if (epoch !== ctx.modeEpoch) return;  // Mode changed during split

        const remaining = data.length - offset;
        const targetEnd = Math.min(offset + CHUNK_SIZE, data.length);

        // Find safe boundary (don't split ANSI sequences or UTF-8)
        const safeEnd = findSafeBoundary(data, targetEnd);
        const actualEnd = safeEnd > offset ? safeEnd : targetEnd;  // Fallback if no safe boundary

        const slice = data.subarray(offset, actualEnd);
        queuedWriteInternal(slice, epoch);
        offset = actualEnd;
    }
}

/**
 * Internal: add chunk to queue with epoch tag
 */
function queuedWriteInternal(data, epoch) {
    if (epoch !== ctx.modeEpoch) return;  // Stale

    const size = data.byteLength || 0;
    writeQueue.push({ data, epoch });
    queuedBytes += size;

    // Check for overflow - clear queue instead of resync loop
    if (queuedBytes > MAX_QUEUE_BYTES || writeQueue.length > MAX_QUEUE_ITEMS) {
        console.warn(`[QUEUE] Overflow (${queuedBytes} bytes, ${writeQueue.length} items) - clearing`);
        writeQueue = [];
        queuedBytes = 0;
        draining = false;
        return;
    }

    if (!draining) {
        draining = true;
        requestAnimationFrame(drainWriteQueue);
    }
}

/**
 * Public API: queue data for ctx.terminal write.
 * Gates all writes behind ctx.outputMode === 'full'.
 * Converts strings to bytes.
 */
function queuedWrite(data) {
    // CRITICAL: Only write to ctx.terminal in full mode
    if (ctx.outputMode !== 'full') {
        return;
    }

    const epoch = ctx.modeEpoch;

    if (data instanceof Uint8Array) {
        enqueueSplit(data, epoch);
    } else if (data instanceof ArrayBuffer) {
        enqueueSplit(new Uint8Array(data), epoch);
    } else if (typeof data === 'string') {
        // Convert string to bytes - no string splitting
        enqueueSplit(_textEncoder.encode(data), epoch);
    } else {
        console.warn('[QUEUE] Unknown data type:', typeof data);
    }
}

/**
 * Drain write queue to ctx.terminal.
 * Checks epoch before each write to abort if mode changed.
 */
function drainWriteQueue() {
    const drainEpoch = ctx.modeEpoch;

    // Abort if resyncing or not in full mode
    if (isResyncing || ctx.outputMode !== 'full') {
        writeQueue = [];
        queuedBytes = 0;
        draining = false;
        return;
    }

    const frameStart = performance.now();
    let chunksThisFrame = 0;
    const MAX_CHUNKS_PER_FRAME = 4;
    const MAX_PER_CHUNK_MS = 4;

    while (writeQueue.length && chunksThisFrame < MAX_CHUNKS_PER_FRAME) {
        // Check frame budget
        if (performance.now() - frameStart >= MAX_PER_FRAME_MS) {
            break;
        }

        // Check if mode changed - abort immediately
        if (ctx.modeEpoch !== drainEpoch || ctx.outputMode !== 'full') {
            writeQueue = [];
            queuedBytes = 0;
            draining = false;
            console.log('[QUEUE] Mode changed during drain, aborting');
            return;
        }

        const item = writeQueue.shift();
        const size = item.data.byteLength || 0;
        queuedBytes -= size;

        // Skip stale items (from old epoch)
        if (item.epoch !== drainEpoch) {
            continue;
        }

        chunksThisFrame++;
        // Log first write at each epoch for debugging
        if (chunksThisFrame === 1 && writeQueue.length === 0) {
            console.log(`[TERMINAL] v245 Writing ${size} bytes (epoch=${drainEpoch}, first chunk)`);
        }
        ctx.terminal.write(item.data);
    }

    // Continue draining if more items and still in correct mode
    if (writeQueue.length && ctx.outputMode === 'full' && ctx.modeEpoch === drainEpoch) {
        requestAnimationFrame(drainWriteQueue);
    } else {
        draining = false;
        queuedBytes = 0;  // Reset counter
    }
}

function triggerTerminalResync() {
    if (isResyncing) return;
    if (ctx.outputMode !== 'full') return;  // Only resync in full mode

    // Enforce cooldown to prevent resync loops
    const now = Date.now();
    if (now - lastResyncTime < RESYNC_COOLDOWN) {
        writeQueue = [];
        queuedBytes = 0;
        console.warn('Resync cooldown active, dropping data');
        return;
    }

    isResyncing = true;
    lastResyncTime = now;
    ctx.modeEpoch++;  // Invalidate any in-flight data

    // Clear queue
    writeQueue = [];
    queuedBytes = 0;
    draining = false;

    showToast('Resyncing terminal...', 1000);

    if (ctx.terminal) {
        ctx.terminal.reset();
    }

    const resyncEpoch = ctx.modeEpoch;
    fetchTerminalSnapshot().then(snapshot => {
        // Check epoch hasn't changed during fetch
        if (ctx.modeEpoch !== resyncEpoch || ctx.outputMode !== 'full') {
            console.log('Resync cancelled - mode changed');
            isResyncing = false;
            return;
        }
        if (snapshot && ctx.terminal) {
            enqueueSplitDirect(snapshot, resyncEpoch);
        }
        setTimeout(() => {
            isResyncing = false;
            console.log('Terminal resync complete');
        }, 500);
    }).catch(err => {
        console.error('Resync failed:', err);
        isResyncing = false;
    });
}

// Direct enqueue for resync snapshot (with epoch)
function enqueueSplitDirect(data, epoch) {
    if (isResyncing) return;
    if (epoch !== ctx.modeEpoch) return;

    // Convert to bytes if string
    let bytes;
    if (typeof data === 'string') {
        bytes = _textEncoder.encode(data);
    } else if (data instanceof Uint8Array) {
        bytes = data;
    } else {
        return;
    }

    // Chunk and enqueue with epoch tag (through overflow-protected path)
    let offset = 0;
    while (offset < bytes.length) {
        if (epoch !== ctx.modeEpoch) return;  // Abort if mode changed
        const targetEnd = Math.min(offset + CHUNK_SIZE, bytes.length);
        const safeEnd = findSafeBoundary(bytes, targetEnd);
        const actualEnd = safeEnd > offset ? safeEnd : targetEnd;
        const slice = bytes.subarray(offset, actualEnd);
        queuedWriteInternal(slice, epoch);
        offset = actualEnd;
    }
}

async function fetchTerminalSnapshot() {
    try {
        const params = new URLSearchParams({ token: ctx.token });
        if (ctx.activeTarget) params.set('target', ctx.activeTarget);
        const resp = await apiFetch(`/api/terminal/snapshot?${params}`);
        if (!resp.ok) throw new Error(`Snapshot failed: ${resp.status}`);
        const data = await resp.json();
        return data.content || '';
    } catch (err) {
        console.error('fetchTerminalSnapshot error:', err);
        return '';
    }
}

// Reconnection with exponential backoff
let reconnectDelay = 300;
const MAX_RECONNECT_DELAY = 10000;  // Cap at 10s (was 30s) - mobile needs fast recovery
const INITIAL_RECONNECT_DELAY = 300;  // Faster initial reconnect (was 500ms)
const MIN_CONNECTION_INTERVAL = 300;  // Minimum ms between connection attempts
const RECONNECT_OVERLAY_GRACE_MS = 2500;  // Don't show overlay for brief disconnects
let intentionalClose = false;  // Track intentional closes to skip auto-reconnect
let repoSwitchInProgress = false;  // Suppress 4003 auto-reconnect during switchRepo()
let isConnecting = false;  // Prevent concurrent connection attempts
let reconnectInProgress = false;  // Global gate — prevents parallel reconnects across all triggers
let reconnectTimer = null;  // Track pending reconnect
let reconnectOverlayTimer = null;  // Delayed overlay (grace period)
let lastConnectionAttempt = 0;  // Timestamp of last connection attempt
let reconnectAttempts = 0;  // Track consecutive failed reconnects
const SHOW_HARD_REFRESH_AFTER = 3;  // Show hard refresh button after N failures
const AUTO_RELOAD_AFTER = 4;         // Auto-reload page after N failures (fixes stale proxy connections)
let hasConnectedOnce = false;  // Track if we've ever connected (to detect reconnects)
let reconcileInFlight = false;  // Prevent overlapping reconciliations

// Server restart on failed reconnect
const RESTART_TIMEOUT = 2500;   // Try restart if no connection within 2.5s
const RESTART_COOLDOWN = 60000; // Client-side 60s cooldown between restart attempts
let lastRestartAttempt = 0;     // Timestamp of last restart request
let restartPending = false;     // Track if restart is in progress

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

// Transport selection: WS preferred, SSE fallback for HTTP/2 proxies
const _bp = window.__BASE_PATH || '';
const _transportKey = `mto_transport_${location.origin}${_bp}`;
const _urlTransport = new URLSearchParams(location.search).get('transport');
let _preferSSE = _urlTransport === 'sse' || (!_urlTransport && sessionStorage.getItem(_transportKey) === 'sse');
// ?transport=ws explicitly clears stored SSE preference so it doesn't reassert next load
if (_urlTransport === 'ws') sessionStorage.removeItem(_transportKey);

// SSE input batcher: coalesces rapid keystrokes into single POST requests
const _sseEncoder = new TextEncoder();
const SSE_BATCH_MS = 50;
let _sseBatchBuf = [];
let _sseBatchTimer = null;

function _sseFlushInput() {
    _sseBatchTimer = null;
    if (_sseBatchBuf.length === 0) return;
    // Merge all buffered Uint8Arrays into one
    const total = _sseBatchBuf.reduce((n, b) => n + b.length, 0);
    const merged = new Uint8Array(total);
    let offset = 0;
    for (const b of _sseBatchBuf) { merged.set(b, offset); offset += b.length; }
    _sseBatchBuf = [];
    apiFetch(`/api/terminal/input`, { method: 'POST', body: merged });
}

/** Queue raw input bytes for batched SSE POST. Immediate flush for special keys. */
function sseSendInputBatched(data, immediate = false) {
    const bytes = data instanceof Uint8Array ? data : _sseEncoder.encode(data);
    _sseBatchBuf.push(bytes);
    if (immediate || bytes.length === 1 && bytes[0] < 0x20) {
        // Control chars (Ctrl-C, Enter, arrows) — flush now
        if (_sseBatchTimer) { clearTimeout(_sseBatchTimer); _sseBatchTimer = null; }
        _sseFlushInput();
    } else if (!_sseBatchTimer) {
        _sseBatchTimer = setTimeout(_sseFlushInput, SSE_BATCH_MS);
    }
}

// Activity-based keepalive - detect stale connections
const IDLE_THRESHOLD = 20000;  // If no data for 20s, send a ping to verify connection
let idleCheckTimer = null;

// Local command history (persisted to localStorage)
const MAX_HISTORY_SIZE = 100;
let commandHistory = JSON.parse(localStorage.getItem('terminalHistory') || '[]');
let historyIndex = -1;
let historySavedInput = '';
let currentInput = '';

// DOM elements (initialized in DOMContentLoaded)
let terminalContainer, controlBarsContainer;
let collapseToggle, controlBar, roleBar, inputBar, viewBar;
let statusOverlay, statusText, repoBtn, repoLabel, repoDropdown;
// Target selector variables removed (elements no longer in DOM)
let agentCrashBanner, agentRespawnBtn, agentCrashDismissBtn;
// searchBtn removed - search is now in docs modal
let composeBtn, composeModal;
let composeInput, composeClose, composeClear, composePaste, composeInsert, composeRun;
let composeAttach, composeFileInput, composeThinkMode, composeAttachments;
let selectCopyBtn, drawersBtn, challengeBtn;
let challengeModal, challengeClose, challengeResult, challengeStatus, challengeRun;
let logView, logInput, logSend, logContent, refreshBtn;
let terminalView;

// Attachments state for compose modal
let pendingAttachments = [];
// Promises for currently-in-flight uploads from compose AND desktop input.
// sendComposedText / queueComposedText / sendLogCommand await these before
// reading pendingAttachments, otherwise a fast-tapping user can hit Send
// before an upload returns and the file is silently dropped.
let inflightUploads = [];

// Draft persistence for compose modal
let composeDraft = sessionStorage.getItem('composeDraft') || '';
let draftAttachments = JSON.parse(sessionStorage.getItem('composeDraftAttachments') || '[]');

// Last activity timestamp tracking
let lastActivityTime = 0;
let lastActivityElement = null;
let activityUpdateTimer = null;

// Force scroll to bottom flag (used during resize)
let forceScrollToBottom = false;

// Queue — moved to src/features/queue.js

// Unified drawer state
let drawerOpen = false;


// Terminal busy state - when busy, input box shows Q instead of Enter
let terminalBusy = false;

// Tool collapse — moved to src/features/collapse.js
const scheduleIdle = window.requestIdleCallback || ((cb) => setTimeout(cb, 100));

// Scroll tracking for log view - only auto-scroll if user is at bottom
let userAtBottom = true;
let scrollLockUntil = 0;  // Timestamp: ignore scroll events until this time
let newContentIndicator = null;

// Preview mode state
let previewMode = null;          // null = live, string = snapshot_id
let previewSnapshot = null;      // Full snapshot data when in preview
let previewSnapshots = [];       // Cached list of snapshots
let previewFilter = 'all';       // Current filter: all, user_send, tool_call, agent_done, error

// Target selector state (for multi-pane sessions)
// ctx.targets initialized by context.js ([])
// ctx.activeTarget initialized by context.js (null)
let expectedRepoPath = null;     // Expected repo path from ctx.config
let targetLocked = true;         // Lock mode (true = locked, false = follow active pane)

// Pending prompt state (for questions/confirmations)
let pendingPrompt = null;        // { id, kind, text, choices, answered, sentChoice }
let dismissedPrompts = new Set(); // Prompt IDs user dismissed without answering
let promptBanner = null;         // DOM reference for sticky banner
let _permAutoApprovedAt = 0;     // Timestamp of last auto-approval (suppresses banner flash)
let _lastLogContentHash = '';    // Hash of last log content (for change detection)
let _logSettledAt = 0;           // When log content stopped changing

// Agent health polling state (singleflight async loop)
let agentHealthController = null;  // AbortController for singleflight loop
let lastAgentHealth = null;        // Last health check result
let agentStartedAt = null;         // Timestamp when agent was detected running
const HEALTH_POLL_INTERVAL = 5000;  // 5 seconds between health checks
let agentCrashDebounceTimer = null;  // Debounce timer for crash detection
let dismissedCrashPanes = new Set();  // Panes where user dismissed crash banner
let agentName = 'Agent';             // Display name from /config (e.g. "Claude", "Codex CLI")

// Status strip state
let lastPhase = null;               // Last phase result from /api/status/phase
let phaseIdleShowHistoryTimer = null; // Timer for showing "Open History" after idle transition

// Team state
// ctx.teamState initialized by context.js (null)

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
    agentCrashBanner = document.getElementById('agentCrashBanner');
    agentRespawnBtn = document.getElementById('agentRespawnBtn');
    agentCrashDismissBtn = document.getElementById('agentCrashDismissBtn');
    // searchBtn/searchModal removed - search is now in docs modal
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
    lastActivityElement = document.getElementById('lastActivity');
    logView = document.getElementById('logView');
    logInput = document.getElementById('logInput');
    logSend = document.getElementById('logSend');
    logContent = document.getElementById('logContent');
    refreshBtn = document.getElementById('refreshBtn');
    terminalView = document.getElementById('terminalView');
    terminalBlock = document.getElementById('terminalBlock');
    activePromptContent = document.getElementById('activePromptContent');
    // Queue elements — initialized via initQueue() in DOMContentLoaded
    // New window modal elements
    newWindowModal = document.getElementById('newWindowModal');
    newWindowClose = document.getElementById('newWindowClose');
    newWindowRepo = document.getElementById('newWindowRepo');
    newWindowName = document.getElementById('newWindowName');
    newWindowAutoStart = document.getElementById('newWindowAutoStart');
    newWindowCancel = document.getElementById('newWindowCancel');
    newWindowCreate = document.getElementById('newWindowCreate');
    // Prompt banner element
    promptBanner = document.getElementById('promptBanner');
}

// Additional DOM elements
let terminalBlock, activePromptContent;
let newWindowModal, newWindowClose, newWindowRepo, newWindowName;
let newWindowAutoStart, newWindowCancel, newWindowCreate;

// Available repos for new window creation
let availableRepos = [];
let availableWorkspaceDirs = [];

/**
 * Initialize the ctx.terminal
 * Uses fit addon to auto-size based on container width
 */
let fitAddon = null;
let searchAddon = null;

// Detect mobile for performance tuning
const isMobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent) || window.innerWidth < 768;

function initTerminal() {
    ctx.terminal = new Terminal({
        cursorBlink: false,
        cursorStyle: 'bar',
        cursorInactiveStyle: 'none',
        fontSize: Number(paramFontSize) || 11,
        fontFamily: 'Menlo, Monaco, "Courier New", monospace',
        scrollback: isMobile ? 2000 : 10000,  // Smaller buffer on mobile for faster rendering
        smoothScrollDuration: 0,  // Disable smooth scroll - causes delays on mobile
        overviewRulerWidth: 0,
        theme: {
            background: '#1e1e1e',
            foreground: '#d4d4d4',
            cursor: '#1e1e1e',  // Same as background = invisible
            cursorAccent: '#1e1e1e',
            selection: 'rgba(55, 148, 255, 0.3)',
            black: '#1e1e1e',
            red: '#f14c4c',
            green: '#89d185',
            yellow: '#cca700',
            blue: '#569cd6',
            magenta: '#c586c0',
            cyan: '#4ec9b0',
            white: '#d4d4d4',
            brightBlack: '#808080',
            brightRed: '#f14c4c',
            brightGreen: '#89d185',
            brightYellow: '#e5c07b',
            brightBlue: '#3794ff',
            brightMagenta: '#d2a8ff',
            brightCyan: '#4ec9b0',
            brightWhite: '#ffffff',
        },
        allowProposedApi: true,
    });

    // Fit addon to auto-size ctx.terminal to container
    fitAddon = new FitAddon.FitAddon();
    ctx.terminal.loadAddon(fitAddon);

    // Web links addon for clickable URLs
    const webLinksAddon = new WebLinksAddon.WebLinksAddon();
    ctx.terminal.loadAddon(webLinksAddon);

    // Search addon for in-terminal text search
    if (typeof SearchAddon !== 'undefined') {
        searchAddon = new SearchAddon.SearchAddon();
        ctx.terminal.loadAddon(searchAddon);
    }

    ctx.terminal.open(terminalContainer);

    // Fit to container after opening
    fitAddon.fit();

    // ResizeObserver: re-fit terminal whenever container dimensions actually change.
    // This catches layout changes that single rAF misses (e.g., view switching from
    // display:none, keyboard appearing, orientation change).
    if (typeof ResizeObserver !== 'undefined') {
        let prevW = 0, prevH = 0, resizeTimer = 0;
        const ro = new ResizeObserver((entries) => {
            const entry = entries[0];
            if (!entry) return;
            const { width, height } = entry.contentRect;
            // Only re-fit if dimensions actually changed and are non-zero
            if (width > 0 && height > 0 && (width !== prevW || height !== prevH)) {
                prevW = width;
                prevH = height;
                // Debounce to avoid spamming during drag resizes
                clearTimeout(resizeTimer);
                resizeTimer = setTimeout(() => {
                    if (fitAddon) fitAddon.fit();
                    sendResize();
                }, 50);
            }
        });
        ro.observe(terminalContainer);
    }

    // Handle ctx.terminal input (only when unlocked)
    // Send as binary for faster processing (bypasses JSON parsing on server)
    const encoder = new TextEncoder();

    // Simple composition handling - no incremental sending to avoid doubles
    let isComposing = false;

    ctx.terminal.textarea.addEventListener('compositionstart', () => {
        isComposing = true;
    });

    ctx.terminal.textarea.addEventListener('compositionend', () => {
        isComposing = false;
    });

    // Reset composition state on blur (prevents stuck state after focus changes)
    ctx.terminal.textarea.addEventListener('blur', () => {
        isComposing = false;
    });

    // Also reset on focus to ensure clean state
    ctx.terminal.textarea.addEventListener('focus', () => {
        isComposing = false;
    });

    ctx.terminal.onData((data) => {
        // On desktop, terminal is view-only — all input goes through the input bar
        // Only allow direct terminal input in interactive mode (vim/top/fzf)
        if (ctx.uiMode === 'desktop-multipane' && !interactiveMode) return;
        if (!isPreviewMode() && ctx.socket && ctx.socket.readyState === WebSocket.OPEN) {
            // Skip during active composition - wait for compositionend then onData fires
            if (isComposing) {
                return;
            }
            if (ctx._transportType === 'sse') {
                sseSendInputBatched(data);
            } else {
                ctx.socket.send(encoder.encode(data));
            }
            // Reset interactive idle timer on each keystroke (if active)
            if (interactiveMode) resetInteractiveIdleTimer();
        }
    });

    // Send fixed size once after short delay (no dynamic resizing)
    setTimeout(() => {
        sendResize();
    }, 100);
}

/**
 * Active Prompt - shows current screen state from tmux
 * Uses singleflight async loop pattern - only one request in flight at a time
 */
const ACTIVE_PROMPT_LINES = 25;  // Lines to capture from current screen
const ACTIVE_PROMPT_INTERVAL = 1000;  // Wait 1s between requests
let activePromptController = null;  // AbortController for singleflight loop

/**
 * Quick busy-state re-check after sending a command.
 * Polls at 300ms intervals (up to 3 tries) to clear terminalBusy fast,
 * so the send button doesn't stay stuck as "Q" for a full poll cycle.
 */
function scheduleEarlyBusyCheck(attempt = 0) {
    if (!terminalBusy || attempt >= 3) return;
    setTimeout(async () => {
        if (!terminalBusy) return;  // Already cleared by normal poll
        try {
            const resp = await apiFetch(`/api/terminal/capture?lines=${ACTIVE_PROMPT_LINES}`);
            if (!resp.ok) return;
            const data = await resp.json();
            if (!data.content) return;
            const content = stripAnsi(data.content);
            if (extractPromptContent(content) !== null) {
                setTerminalBusy(false);
                return;
            }
        } catch (e) { /* ignore */ }
        scheduleEarlyBusyCheck(attempt + 1);
    }, 300);
}

async function refreshActivePrompt(signal) {
    if (!activePromptContent) return;

    // Skip if page not visible (save resources)
    if (document.visibilityState !== 'visible') return;

    // Don't refresh if user has text selected (would lose selection)
    const selection = window.getSelection();
    if (selection && selection.toString().length > 0) {
        return;
    }

    try {
        // Use capture endpoint with small line count (current screen, not scrollback)
        const response = await apiFetch(`/api/terminal/capture?lines=${ACTIVE_PROMPT_LINES}`, { signal });
        if (!response.ok) return;

        const data = await response.json();
        if (!data.content) return;

        // Strip ANSI codes
        let content = stripAnsi(data.content);

        // Extract context usage before cleaning strips it
        extractContextUsage(content);

        // Detect permission prompts from RAW terminal content (before cleaning
        // strips box-drawing chars that contain the tool name)
        extractPermissionPrompt(content);

        // Clean up clutter for display
        content = cleanTerminalOutput(content);

        // Update content (no auto-scroll - let user control scroll position)
        activePromptContent.textContent = content;

        // Suggestion auto-fill disabled — was injecting stale content into input

        // Check if prompt is visible - if so, ctx.terminal is ready
        const extracted = extractPromptContent(content);
        if (extracted !== null) {
            setTerminalBusy(false);
        }

    } catch (error) {
        if (error.name === 'AbortError') throw error;  // Re-throw abort
        console.debug('Active prompt refresh failed:', error);
    }
}

async function startActivePrompt() {
    stopActivePrompt();
    activePromptController = new AbortController();
    const signal = activePromptController.signal;

    // Singleflight async loop - only one request at a time
    while (!signal.aborted) {
        try {
            await refreshActivePrompt(signal);
            await abortableSleep(ACTIVE_PROMPT_INTERVAL, signal);
        } catch (error) {
            if (error.name === 'AbortError') break;
            console.debug('Active prompt loop error:', error);
            // Wait before retry on error
            try { await abortableSleep(2000, signal); } catch { break; }
        }
    }
}

function stopActivePrompt() {
    if (activePromptController) {
        activePromptController.abort();
        activePromptController = null;
    }
}

/**
 * Extract context usage from Claude Code's status bar.
 * Looks for patterns like "XX% context left" or "context left until auto-compact".
 */
let lastContextPct = -1;
let contextAlertSent = false;

function extractContextUsage(content) {
    const pill = document.getElementById('contextPill');
    if (!pill) return;

    // Claude Code shows: "XX% context left until auto-compact" or similar
    // Also: "Context left until auto-compact: XX%"
    const patterns = [
        /(\d+)%\s*context\s*left/i,
        /context\s*left[^:]*?:\s*(\d+)%/i,
        /(\d+)%\s*remaining/i,
    ];

    let remaining = -1;
    for (const pat of patterns) {
        const m = content.match(pat);
        if (m) {
            remaining = parseInt(m[1], 10);
            break;
        }
    }

    if (remaining < 0 || remaining > 100) {
        // No context indicator found — hide pill if agent not running
        if (lastContextPct < 0) pill.classList.add('hidden');
        return;
    }

    // Avoid unnecessary DOM updates
    if (remaining === lastContextPct) return;
    lastContextPct = remaining;

    const used = 100 - remaining;
    pill.textContent = `ctx ${remaining}%`;
    pill.classList.remove('hidden', 'ctx-ok', 'ctx-warn', 'ctx-critical');

    if (remaining > 30) {
        pill.classList.add('ctx-ok');
    } else if (remaining > 15) {
        pill.classList.add('ctx-warn');
    } else {
        pill.classList.add('ctx-critical');
    }

    // Toast warning at 15% remaining (once per session)
    if (remaining <= 15 && !contextAlertSent) {
        contextAlertSent = true;
        showToast(`Context low: ${remaining}% remaining`, 'warning', 5000);
    }
}

/**
 * Update context pill from backend /api/status/phase data.
 * Preferred over pane-scraping when backend provides context_pct.
 */
function updateContextFromBackend(data) {
    if (data.context_pct == null) return;
    const pill = document.getElementById('contextPill');
    if (!pill) return;

    const remaining = Math.max(0, Math.min(100, Math.round(100 - data.context_pct)));
    if (remaining === lastContextPct) return;
    lastContextPct = remaining;

    pill.textContent = `ctx ${remaining}%`;
    pill.classList.remove('hidden', 'ctx-ok', 'ctx-warn', 'ctx-critical');

    if (remaining > 30) {
        pill.classList.add('ctx-ok');
    } else if (remaining > 15) {
        pill.classList.add('ctx-warn');
    } else {
        pill.classList.add('ctx-critical');
    }

    if (remaining <= 15 && !contextAlertSent) {
        contextAlertSent = true;
        showToast(`Context low: ${remaining}% remaining`, 'warning', 5000);
    }
}

/**
 * Extract suggestion from ctx.terminal output and pre-fill input box
 */
let lastSuggestion = '';
let recentSentCommands = new Set();  // Track recent commands to avoid re-suggesting

function extractAndSuggestCommand(content) {
    if (!logInput) return;
    if (terminalBusy) return;  // Don't pre-fill while ctx.terminal is processing

    // Don't overwrite if user is typing
    if (document.activeElement === logInput && logInput.value.length > 0) {
        return;
    }

    // Skip Claude's session rating prompt — not actionable
    if (ctx.agentType === 'claude' && /How is Claude doing/i.test(content)) return;

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

        // Numbered options in an interactive choice dialog (? prefix)
        // Only suggest when there's a question prompt, not plain numbered lists
        if (/^\?\s/.test(trimmed) || /^\[?\d\]?\)\s/.test(trimmed)) {
            // Look for numbered options in surrounding lines
            const hasQuestion = lines.some(l => /^\?\s/.test(l.trim()));
            if (hasQuestion && /^\[?[1-3]\]?\)?\.?\s+/.test(trimmed)) {
                suggestion = '1';
                break;
            }
        }

        // Yes/No prompts
        if (/\(y\/n\)/i.test(trimmed) || /\[yes\/no\]/i.test(trimmed)) {
            suggestion = 'y';
            break;
        }
    }

    // Never re-suggest a recently sent command
    if (suggestion && recentSentCommands.has(suggestion)) return;

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
 * Extract editable content from ctx.terminal prompt line.
 * Supports multiple prompt patterns: Claude Code, bash, zsh, python, node.
 * @param {string} content - Terminal capture (ANSI stripped)
 * @returns {string|null} - Content after prompt marker, or null if no prompt found
 */
function extractPromptContent(content) {
    const lines = content.split('\n');

    // Prompt patterns in priority order
    // NOTE: No generic "> " pattern — too many false positives from git log,
    // quoted text, commit messages, etc.
    const patterns = [
        /^❯\s*(.*)$/,                                                  // Claude Code: ❯ cmd
        /^(?:\([^)]+\)\s*)?[\w.-]+@[\w.-]+[:\s][^$#]*[$#]\s*(.*)$/,   // bash: user@host:~$
        /^[$#]\s*(.*)$/,                                               // Simple: $ or #
        /^>>>\s*(.*)$/,                                                // Python REPL
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
 * Sync ctx.terminal prompt content to input box
 */
async function syncPromptToInput() {
    if (!logInput) return;

    try {
        const response = await apiFetch(`/api/terminal/capture?lines=5`);
        if (!response.ok) return;

        const data = await response.json();
        const content = stripAnsi(data.content || '');
        const extracted = extractPromptContent(content);

        if (extracted !== null) {
            // Don't re-fill with the same text we just sent
            if (extracted && (extracted === lastSuggestion || recentSentCommands.has(extracted))) {
                setTerminalBusy(false);
                return;
            }
            logInput.value = extracted;
            logInput.dataset.autoSuggestion = 'false';
            logInput.focus();
            logInput.setSelectionRange(logInput.value.length, logInput.value.length);
            // Prompt detected - ctx.terminal is ready
            setTerminalBusy(false);
        }
    } catch (e) {
        console.debug('Sync failed:', e);
    }
}

/**
 * Set ctx.terminal busy state and update send button accordingly.
 *
 * Note: this used to schedule a client-side queue drain after a 3-second
 * idle. That logic has been removed — the server's CommandQueue is now
 * the single auto-drainer, which avoids the double-send race where both
 * sides popped the same item. The client just observes via WS messages
 * (queue_sent updates the local item to 'sent'). Manual "Run" still
 * works via sendNextUnsafe().
 */
function setTerminalBusy(busy) {
    terminalBusy = busy;
    updateSendButton();
}

/**
 * Pop the next queued command and send it to the terminal.
 * If the terminal is busy (race condition), re-queue the item.
 */
function sendNextUnsafe() {
    const item = popNextQueueItem();
    if (!item) return;
    if (terminalBusy) {
        requeueItem(item);
        showToast('Terminal busy \u2014 command re-queued', 'info', 1500);
        return;
    }

    // Remove from server-side queue so _process_loop doesn't send it again
    dequeueFromServer(item.id);

    sendTextAtomic(item.text, true);
    setTerminalBusy(true);
    // No scheduleEarlyBusyCheck() here — the early busy-clear is too
    // aggressive for queued commands and causes rapid-fire drain.
    // Normal poll cycle will detect the prompt and clear busy state.
    captureSnapshot('queue_send');
    recentSentCommands.add(item.text);
    if (recentSentCommands.size > 20) {
        recentSentCommands.delete(recentSentCommands.values().next().value);
    }
    if (item.text) addToHistory(item.text);
}

/**
 * Fire-and-forget: tell server to remove a queue item.
 * Prevents server-side _process_loop from sending it again.
 */
function dequeueFromServer(itemId) {
    const params = new URLSearchParams({
        session: ctx.currentSession,
        item_id: itemId,
        token: ctx.token,
    });
    if (ctx.activeTarget) params.set('pane_id', ctx.activeTarget);
    apiFetch(`/api/queue/remove?${params}`, { method: 'POST' }).catch(() => {});
}

/**
 * Update send button appearance based on ctx.terminal busy state
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

    // Mirror ready state on shortcut-bar Q button
    const qBtn = document.querySelector('.btn-queue[data-key="queue"]');
    if (qBtn) qBtn.classList.toggle('agent-ready', !terminalBusy);
}

/**
 * Send key to ctx.terminal and sync result to input box
 * @param {string} key - ANSI key code to send
 * @param {number} delay - ms to wait before capture (default 100)
 */
async function sendKeyWithSync(key, delay = 100) {
    if (!ctx.socket || ctx.socket.readyState !== WebSocket.OPEN) return;

    sendInput(key);
    await new Promise(r => setTimeout(r, delay));
    await syncPromptToInput();
}

// ── Visibility-aware poller registry ────────────────────────────────
//
// One central scheduler for setInterval-based pollers. Each registered
// poller declares its name + callback + interval; the registry runs them
// only when the document is visible and pauses them all wholesale when
// the tab hides. This replaces N per-poller `if (visibilityState ===
// 'hidden') return;` checks scattered across the file with one
// visibilitychange listener that suspends/resumes everything.
//
// Why centralized: scattered visibility checks waste timer ticks (the
// browser still wakes the page to evaluate them) and miss nested
// setTimeouts that schedule new work after the visibility check has
// already passed. Pulling pollers into a registry also makes "stop
// everything on disconnect" a one-liner if we ever need it.
//
// Async-loop pollers (startAgentHealthPolling, startLogAutoRefresh) are
// NOT migrated — they already self-manage via AbortController and check
// visibility inside their own loops, which is correct for that pattern.

const _pollers = new Map();  // name -> { fn, intervalMs, timer }
let _pollersSuspended = (typeof document !== 'undefined') &&
    document.visibilityState === 'hidden';

function _runPoller(name, fn) {
    try { fn(); }
    catch (e) { console.error(`[poller:${name}]`, e); }
}

function registerPoller(name, fn, intervalMs) {
    // Replace an existing poller with the same name (idempotent re-register).
    unregisterPoller(name);
    const entry = { fn, intervalMs, timer: null };
    _pollers.set(name, entry);
    if (!_pollersSuspended) {
        entry.timer = setInterval(() => _runPoller(name, fn), intervalMs);
    }
}

function unregisterPoller(name) {
    const entry = _pollers.get(name);
    if (entry && entry.timer) clearInterval(entry.timer);
    _pollers.delete(name);
}

if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', () => {
        const hidden = document.visibilityState === 'hidden';
        if (hidden && !_pollersSuspended) {
            // Suspend: clear all intervals. State is preserved so we can
            // resume without losing each poller's identity.
            for (const entry of _pollers.values()) {
                if (entry.timer) {
                    clearInterval(entry.timer);
                    entry.timer = null;
                }
            }
            _pollersSuspended = true;
        } else if (!hidden && _pollersSuspended) {
            // Resume: restart all intervals.
            for (const [name, entry] of _pollers) {
                entry.timer = setInterval(
                    () => _runPoller(name, entry.fn),
                    entry.intervalMs,
                );
            }
            _pollersSuspended = false;
        }
    });
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
 * Send a Stop / interrupt to the agent.
 *
 * Sends Ctrl+C (interrupt) followed by Esc (clear-input) ~100ms later.
 * The Esc is critical: in Claude Code, Ctrl+C interrupts the agent but
 * *preserves* the previously-submitted prompt in the input buffer for
 * editing. If the user then types new text and submits, the new text
 * gets appended to the preserved text and Claude receives
 * "<prev message> + <new message>" — the user-visible "my previous
 * message keeps getting re-sent" symptom. Esc clears the buffer in
 * Claude Code and is a harmless no-op in bash/zsh; in vim it exits
 * insert mode (which is what you want after a Stop anyway).
 *
 * Use this in preference to ``sendKeyDebounced('\\x03', true)`` for
 * any user-facing "stop the agent" action.
 */
function sendStopInterrupt() {
    sendKeyDebounced('\x03', true);
    setTimeout(() => sendKeyDebounced('\x1b', true), 100);
}

/**
 * Start heartbeat ping/pong for connection health monitoring.
 *
 * Routed through registerPoller, so visibility handling is centralized
 * (browser hidden → all pollers paused). The pong-timeout setTimeout
 * still has its own handle so we can cancel it from stopHeartbeat or
 * before scheduling a new one.
 */
function startHeartbeat() {
    stopHeartbeat();
    lastPongTime = Date.now();

    registerPoller('heartbeat', () => {
        if (ctx.socket && ctx.socket.readyState === WebSocket.OPEN) {
            ctx.socket.send(JSON.stringify({ type: 'ping' }));

            // Cancel any pending pong-timeout from a previous tick before
            // scheduling the new one — otherwise rapid ticks could stack.
            if (heartbeatTimeoutTimer) clearTimeout(heartbeatTimeoutTimer);
            heartbeatTimeoutTimer = setTimeout(() => {
                heartbeatTimeoutTimer = null;
                console.log('Heartbeat timeout - no pong received, reconnecting');
                if (ctx.socket) ctx.socket.close();
            }, HEARTBEAT_TIMEOUT);
        }
    }, HEARTBEAT_INTERVAL);
}

/**
 * Stop heartbeat timers.
 *
 * Note: idle-check is now independent from heartbeat (was bundled here
 * historically because they shared a timer pool). Each has its own
 * stop function — stopIdleCheck handles its own state.
 */
function stopHeartbeat() {
    unregisterPoller('heartbeat');
    if (heartbeatTimer) {
        // Backward-compat: legacy callers may have set this directly. Clear it.
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
    }
    if (heartbeatTimeoutTimer) {
        clearTimeout(heartbeatTimeoutTimer);
        heartbeatTimeoutTimer = null;
    }
}

// Tracks the nested stale-check setTimeout from the idle-check poller.
// Previously this was an anonymous setTimeout — no handle, couldn't be
// cancelled. If the page hid right after scheduling it (or stopIdleCheck
// fired), the timer would still run and could close a healthy socket.
let idleStaleCheckTimer = null;

/**
 * Start idle connection check — detects stale connections faster.
 * If no data received for IDLE_THRESHOLD, send a ping to verify connection.
 *
 * Routed through registerPoller: visibility handling is centralized.
 * The nested stale-check timer now has a handle so it can be cancelled.
 */
function startIdleCheck() {
    stopIdleCheck();
    lastDataReceived = Date.now();

    registerPoller('idle-check', () => {
        if (!ctx.socket || ctx.socket.readyState !== WebSocket.OPEN) return;

        const idle = Date.now() - lastDataReceived;
        if (idle <= IDLE_THRESHOLD) return;

        // No data for a while — ping to verify the connection is alive.
        console.log(`Connection idle for ${idle}ms, sending keepalive ping`);
        ctx.socket.send(JSON.stringify({ type: 'ping' }));

        // Schedule a follow-up stale-check; clear any pending one first
        // so rapid ticks don't stack handles. The handle is module-level
        // so stopIdleCheck can cancel it cleanly.
        if (idleStaleCheckTimer) clearTimeout(idleStaleCheckTimer);
        idleStaleCheckTimer = setTimeout(() => {
            idleStaleCheckTimer = null;
            const stillIdle = Date.now() - lastDataReceived;
            if (stillIdle > IDLE_THRESHOLD + HEARTBEAT_TIMEOUT) {
                console.log('Connection appears stale, forcing reconnect');
                if (ctx.socket) ctx.socket.close();
            }
        }, HEARTBEAT_TIMEOUT);
    }, 5000);
}

function stopIdleCheck() {
    unregisterPoller('idle-check');
    if (idleCheckTimer) {
        // Backward-compat for any legacy direct assignment.
        clearInterval(idleCheckTimer);
        idleCheckTimer = null;
    }
    if (idleStaleCheckTimer) {
        clearTimeout(idleStaleCheckTimer);
        idleStaleCheckTimer = null;
    }
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
        // Don't enforce foreground assumptions while hidden — browser/OS may
        // clamp timers and delay packets, making socket state unreliable
        if (document.visibilityState === 'hidden') return;

        // Check if we're in a stuck state:
        // - Not connecting
        // - Socket is null or not OPEN
        // - No reconnect timer scheduled
        // - Overlay is hidden (user thinks they're connected)
        const isStuck = (
            !isConnecting &&
            (!ctx.socket || ctx.socket.readyState !== WebSocket.OPEN) &&
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
            if (ctx.socket && ctx.socket.readyState !== WebSocket.CLOSED) {
                try { ctx.socket.close(); } catch (e) {}
            }
            ctx.socket = null;
            reconnectDelay = INITIAL_RECONNECT_DELAY;
            if (_preferSSE) connectSSE(); else connect();
        }
    }, 10000);  // Check every 10s
}

/**
 * Update last activity timestamp when ctx.terminal receives data
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
    // Routed through registerPoller for centralized visibility handling.
    // Previously plain setInterval with no visibility check — wasted ticks
    // when the tab was hidden (browsers throttle but don't suspend).
    registerPoller('activity-updates', updateActivityDisplay, 5000);
}

/**
 * Update connection status indicator in header
 */
function updateConnectionIndicator(status) {
    const indicator = document.getElementById('connectionIndicator');
    if (!indicator) return;

    indicator.className = 'connection-indicator ' + status;
    indicator.title = status === 'connected' ? 'Connected' : 'Disconnected';

    // Update reconnect badge in system status strip
    const reconnectBadge = document.getElementById('reconnectBadge');
    if (reconnectBadge) {
        if (status === 'connected') {
            reconnectBadge.classList.add('hidden');
        } else {
            reconnectBadge.classList.remove('hidden');
        }
    }

    // Update connection banner
    updateConnectionBanner(status);
}

/**
 * Show/hide connection state banner.
 * States: 'connected', 'disconnected', 'reconnecting'
 */
function updateConnectionBanner(status) {
    const banner = document.getElementById('connectionBanner');
    if (!banner) return;

    const icon = document.getElementById('connectionBannerIcon');
    const text = document.getElementById('connectionBannerText');
    const action = document.getElementById('connectionBannerAction');

    if (status === 'connected') {
        banner.classList.add('hidden');
        banner.className = 'connection-banner hidden';
        return;
    }

    banner.classList.remove('hidden');

    if (status === 'reconnecting') {
        banner.className = 'connection-banner reconnecting';
        if (icon) icon.textContent = '';  // CSS spinner via ::after
        if (text) text.textContent = 'Reconnecting...';
        if (action) action.classList.add('hidden');
    } else {
        // disconnected
        banner.className = 'connection-banner disconnected';
        if (icon) icon.textContent = '\u26A0';
        if (text) text.textContent = 'Connection lost';
        if (action) {
            action.classList.remove('hidden');
            action.textContent = 'Reconnect';
            action.onclick = () => {
                if (typeof manualReconnect === 'function') manualReconnect();
                updateConnectionBanner('reconnecting');
            };
        }
    }
}

/**
 * Manual reconnect triggered by user
 */
function manualReconnect() {
    reconnectInProgress = false;  // User override — force through gate
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    reconnectDelay = INITIAL_RECONNECT_DELAY;
    if (_preferSSE) connectSSE(); else connect();
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
 * Handle a parsed JSON message from either WS or SSE transport.
 * Returns true if the message was handled (caller should not process further).
 */
function handleJsonMessage(msg) {
    // v2 typed message envelope — route to handler
    if (msg.v === 2) {
        handleTypedMessage(msg);
        return true;
    }

    // Server hello handshake - confirms connection is fully established
    if (msg.type === 'hello') {
        console.log('Received hello:', msg);
        helloReceived = true;
        if (helloTimer) {
            clearTimeout(helloTimer);
            helloTimer = null;
        }
        // Hide overlay immediately on hello - connection is established
        if (statusOverlay && !statusOverlay.classList.contains('hidden')) {
            statusOverlay.classList.add('hidden');
        }
        return true;
    }

    // Tail mode updates - lightweight text for Log view
    if (msg.type === 'tail') {
        return true;
    }

    if (msg.type === 'pong') {
        handlePong();
        return true;
    }

    // Server closing connection (SSE transport sends this before stream ends)
    if (msg.type === 'close') {
        if (msg.code === 4002) {
            console.log('Connection replaced by another client (SSE close)');
            intentionalClose = true;  // Prevent auto-reconnect
            statusText.textContent = 'Replaced by another connection. Tap to reconnect.';
            statusOverlay.classList.remove('hidden');
            if (reconnectBtn) reconnectBtn.classList.remove('hidden');
        }
        return true;
    }

    // Server-initiated ping - respond with pong to keep connection alive
    if (msg.type === 'server_ping') {
        if (ctx.socket && ctx.socket.readyState === WebSocket.OPEN) {
            ctx.socket.send(JSON.stringify({ type: 'pong' }));
        }
        return true;
    }

    // Handle queue messages
    if (msg.type === 'queue_update' || msg.type === 'queue_sent' || msg.type === 'queue_state') {
        handleQueueMessage(msg);
        // Auto-complete linked backlog item when queue item is sent
        if (msg.type === 'queue_sent' && msg.backlog_id) {
            updateBacklogStatus(msg.backlog_id, 'done');
        }
        return true;
    }

    // Handle backlog messages
    if (msg.type === 'backlog_update') {
        handleBacklogMessage(msg);
        return true;
    }

    return false;
}

/**
 * SSE transport: POST-based message routing for socket.send() calls
 */
function ssePostMessage(data) {
    if (typeof data === 'string') {
        try {
            const msg = JSON.parse(data);
            if (msg.type === 'resize') {
                apiFetch(`/api/terminal/resize?cols=${msg.cols}&rows=${msg.rows}`, { method: 'POST' });
            } else if (msg.type === 'ping') {
                apiFetch(`/api/terminal/ping`, { method: 'POST' })
                    .then(r => r.json())
                    .then(() => handleJsonMessage({ type: 'pong' }))
                    .catch(() => {});
            } else if (msg.type === 'pong') {
                // Response to server_ping — no POST endpoint needed, server tracks via stream
            } else if (msg.type === 'set_mode') {
                apiFetch(`/api/terminal/mode?mode=${msg.mode}`, { method: 'POST' });
            } else if (msg.type === 'input') {
                sseSendInputBatched(msg.data);
            } else if (msg.type === 'text') {
                apiFetch(`/api/terminal/text`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text: msg.text, enter: msg.enter || false })
                });
            } else {
                // Generic JSON — send as text input
                apiFetch(`/api/terminal/text`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: data
                });
            }
        } catch(e) {
            // Not JSON — raw text input
            sseSendInputBatched(data);
        }
    } else if (data instanceof ArrayBuffer || data instanceof Uint8Array) {
        sseSendInputBatched(data);
    }
}

/**
 * Handle SSE stream close — cleanup and schedule reconnect
 */
function handleSSEClose() {
    reconnectInProgress = false;  // Allow next reconnect attempt
    if (ctx._sseHeartbeat) { clearInterval(ctx._sseHeartbeat); ctx._sseHeartbeat = null; }
    if (ctx._sseReader) {
        try { ctx._sseReader.cancel(); } catch(e) {}
        ctx._sseReader = null;
    }
    // Flush any pending input batch
    if (_sseBatchTimer) { clearTimeout(_sseBatchTimer); _sseBatchTimer = null; }
    _sseBatchBuf = [];
    ctx._transportType = null;
    if (ctx.socket) ctx.socket.readyState = WebSocket.CLOSED;
    updateConnectionIndicator('disconnected');

    // Reconnect after delay — connectionBanner handles the visual feedback,
    // no need for statusOverlay (which duplicates the reconnecting indicator)
    if (!intentionalClose) {

        reconnectTimer = setTimeout(() => {
            if (_preferSSE) connectSSE();
            else connect();
        }, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
    }
    intentionalClose = false;
}

/**
 * Connect via SSE transport (fallback for HTTP/2 proxies that break WebSocket)
 */
function connectSSE() {
    // Global reconnect gate
    if (reconnectInProgress) {
        console.log('[connectSSE] Reconnect already in progress, skipping');
        return;
    }
    reconnectInProgress = true;

    // Close existing WS or SSE
    if (ctx.socket && ctx.socket.readyState !== WebSocket.CLOSED) {
        intentionalClose = true;
        ctx.socket.close();
        ctx.socket = null;
    }
    if (ctx._sseReader) {
        try { ctx._sseReader.cancel(); } catch(e) {}
        ctx._sseReader = null;
    }

    isConnecting = true;
    // Don't show overlay on reconnect — grace period handles it
    if (!hasConnectedOnce) {
        statusText.textContent = 'Connecting (SSE)...';
        statusOverlay.classList.remove('hidden');
    }

    // SSE stream goes through apiFetch (NOT EventSource), so the
    // X-MTO-Token header is set normally. URL token not required here.
    // If we ever switch back to EventSource, restore ?token=… because
    // EventSource can't set custom headers.
    const streamUrl = `/api/terminal/stream`;

    apiFetch(streamUrl).then(response => {
        if (!response.ok) throw new Error(`SSE stream ${response.status}`);
        if (!response.body) throw new Error('No response body for SSE');

        isConnecting = false;
        reconnectInProgress = false;
        console.log('[SSE] Connected');

        // Mark SSE as transport type
        ctx._transportType = 'sse';

        // Create a socket-like interface so existing code works unchanged
        ctx.socket = {
            readyState: WebSocket.OPEN,
            send: function(data) { ssePostMessage(data); },
            close: function() {
                this.readyState = WebSocket.CLOSED;
                if (ctx._sseReader) {
                    try { ctx._sseReader.cancel(); } catch(e) {}
                }
            }
        };

        reconnectDelay = INITIAL_RECONNECT_DELAY;
        reconnectAttempts = 0;
        helloReceived = false;
        hasConnectedOnce = true;

        // Cancel overlay timer
        if (reconnectOverlayTimer) {
            clearTimeout(reconnectOverlayTimer);
            reconnectOverlayTimer = null;
        }

        updateConnectionIndicator('connected');
        startIdleCheck();

        // SSE heartbeat via POST /ping
        if (ctx._sseHeartbeat) clearInterval(ctx._sseHeartbeat);
        ctx._sseHeartbeat = setInterval(() => {
            if (ctx._transportType !== 'sse') return;
            apiFetch(`/api/terminal/ping`, { method: 'POST' })
                .catch(() => {
                    console.warn('[SSE] Ping failed, reconnecting');
                    if (ctx._sseReader) {
                        try { ctx._sseReader.cancel(); } catch(e) {}
                    }
                });
        }, 15000);

        // Send initial resize
        sendResize();

        // Send initial mode
        apiFetch(`/api/terminal/mode?mode=${ctx.outputMode}`, { method: 'POST' });

        // Parse SSE stream
        const reader = response.body.getReader();
        ctx._sseReader = reader;
        const decoder = new TextDecoder();
        let buffer = '';

        function processSSE() {
            reader.read().then(({ done, value }) => {
                if (done) {
                    console.log('[SSE] Stream ended');
                    handleSSEClose();
                    return;
                }

                // Track data for idle detection
                lastDataReceived = Date.now();

                buffer += decoder.decode(value, { stream: true });
                const events = buffer.split('\n\n');
                buffer = events.pop(); // Keep incomplete event in buffer

                for (const eventStr of events) {
                    if (!eventStr.trim()) continue;

                    let eventType = 'message';
                    let data = '';

                    for (const line of eventStr.split('\n')) {
                        if (line.startsWith('event: ')) eventType = line.slice(7);
                        else if (line.startsWith('data: ')) data += (data ? '\n' : '') + line.slice(6);
                        else if (line.startsWith(':')) continue; // comment/keepalive
                    }

                    if (!data) continue;

                    if (eventType === 'message') {
                        // JSON message — same as WS onmessage for JSON
                        try {
                            const msg = JSON.parse(data);
                            if (!handleJsonMessage(msg)) {
                                // Unhandled JSON — treat as terminal text in full mode
                                if (ctx.outputMode === 'full' && ctx.terminal) {
                                    queuedWrite(data);
                                }
                            }
                        } catch(e) {
                            // Not valid JSON
                            if (ctx.outputMode === 'full' && ctx.terminal) {
                                queuedWrite(data);
                            }
                        }
                    } else if (eventType === 'text') {
                        // Terminal text (escape sequences)
                        if (ctx.outputMode !== 'full') continue;
                        const text = data.replace(/\\n/g, '\n').replace(/\\r/g, '\r');
                        if (ctx.terminal) queuedWrite(text);
                        updateLastActivity();
                        if (statusOverlay && !statusOverlay.classList.contains('hidden')) {
                            statusOverlay.classList.add('hidden');
                        }
                    } else if (eventType === 'binary') {
                        // Base64-encoded PTY bytes
                        if (ctx.outputMode !== 'full') continue;
                        try {
                            const bytes = atob(data);
                            const arr = new Uint8Array(bytes.length);
                            for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
                            if (ctx.terminal) queuedWrite(arr);
                            updateLastActivity();
                            if (statusOverlay && !statusOverlay.classList.contains('hidden')) {
                                statusOverlay.classList.add('hidden');
                            }
                        } catch(e) {}
                    }
                }

                processSSE();
            }).catch(err => {
                if (err.name !== 'AbortError') {
                    console.warn('[SSE] Read error:', err);
                    handleSSEClose();
                }
            });
        }

        processSSE();

    }).catch(err => {
        console.error('[SSE] Connect failed:', err);
        isConnecting = false;
        reconnectInProgress = false;
        handleSSEClose();
    });
}

/**
 * Connect to WebSocket
 */
async function connect() {
    // Global reconnect gate — prevents parallel reconnects from competing triggers
    if (reconnectInProgress) {
        console.log('[connect] Reconnect already in progress, skipping');
        return;
    }
    reconnectInProgress = true;

    // Prevent concurrent connection attempts
    if (isConnecting) {
        console.log('Connection already in progress, skipping');
        reconnectInProgress = false;
        return;
    }

    // Enforce minimum interval between connection attempts
    const now = Date.now();
    const elapsed = now - lastConnectionAttempt;
    if (elapsed < MIN_CONNECTION_INTERVAL) {
        console.log(`Throttling connection, waiting ${MIN_CONNECTION_INTERVAL - elapsed}ms`);
        reconnectInProgress = false;  // Clear so throttle timer's call can proceed
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

    // Close existing ctx.socket if any (any state except CLOSED)
    if (ctx.socket && ctx.socket.readyState !== WebSocket.CLOSED) {
        intentionalClose = true;
        ctx.socket.close();
        ctx.socket = null;
    }

    isConnecting = true;

    // Pre-flight: verify HTTP path to backend works before WS upgrade.
    // If this fails, the proxy/tunnel is down — no point attempting WS.
    try {
        const preCheck = await apiFetch('/api/ws-debug');
        if (preCheck.ok) {
            const dbg = await preCheck.json();
            console.log(`[WS pre-flight] server state:`, dbg);
        } else {
            console.warn(`[WS pre-flight] HTTP ${preCheck.status} — proxy may be down`);
        }
    } catch (e) {
        console.warn(`[WS pre-flight] fetch failed:`, e.message);
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Token MUST be in the URL: new WebSocket() can't set custom
    // headers, so X-MTO-Token isn't sent on the upgrade request. PR2's
    // bulk strip removed this and broke auth-enabled deployments.
    const wsUrl = `${protocol}//${window.location.host}${window.__BASE_PATH || ''}/ws/terminal?token=${ctx.token}&_t=${Date.now()}`;

    // Only show overlay on first connect — reconnects use grace period
    if (!hasConnectedOnce) {
        statusText.textContent = 'Connecting...';
        statusOverlay.classList.remove('hidden');
    }

    // Hide reconnect button while connecting
    const reconnectBtn = document.getElementById('reconnectBtn');
    if (reconnectBtn) reconnectBtn.classList.add('hidden');

    ctx.socket = new WebSocket(wsUrl);

    // WS upgrade timeout: if onopen doesn't fire within 5s, the upgrade
    // is hanging (common with Tailscale Serve HTTP/2→WS translation).
    // Fall back to SSE transport.
    let wsUpgradeTimer = setTimeout(() => {
        if (ctx.socket && ctx.socket.readyState !== WebSocket.OPEN) {
            console.warn('[WS] Timeout after 5s, falling back to SSE');
            intentionalClose = true;
            ctx.socket.close();
            reconnectInProgress = false;  // Clear gate before SSE fallback
            _preferSSE = true;
            sessionStorage.setItem(_transportKey, 'sse');
            connectSSE();
        }
    }, 5000);

    ctx.socket.onopen = () => {
        clearTimeout(wsUpgradeTimer);
        console.log(`[v245] WebSocket connected (mode=${ctx.outputMode}, epoch=${ctx.modeEpoch})`);
        isConnecting = false;
        reconnectInProgress = false;

        // Cancel overlay timer (but don't hide overlay yet - wait for ctx.terminal data)
        if (reconnectOverlayTimer) {
            clearTimeout(reconnectOverlayTimer);
            reconnectOverlayTimer = null;
        }
        // Update status to show connection established, waiting for data
        if (statusText && !statusOverlay.classList.contains('hidden')) {
            statusText.textContent = 'Connected, loading...';
        }

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
                if (ctx.socket) ctx.socket.close();
            }
        }, HELLO_TIMEOUT);

        // Fit ctx.terminal to container (don't clear buffer - server will replay history)
        if (ctx.terminal && fitAddon) {
            fitAddon.fit();
        }

        sendResize();

        // Sync output mode with server (in case setOutputMode was called before ctx.socket opened)
        if (ctx.socket && ctx.socket.readyState === WebSocket.OPEN) {
            ctx.socket.send(JSON.stringify({ type: 'set_mode', mode: ctx.outputMode }));
        }

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
                    reloadBacklogForProject('');
                    await refreshLogContent();
                } catch (e) {
                    console.warn('Post-reconnect sync failed:', e);
                } finally {
                    reconcileInFlight = false;
                }
            })();
        }
    };

    let _firstDataReceived = false;
    ctx.socket.onmessage = (event) => {
        // Track all incoming data for idle detection
        lastDataReceived = Date.now();

        if (event.data instanceof Blob) {
            // Binary PTY data - only process in full mode
            if (ctx.outputMode !== 'full') {
                console.debug('Ignoring binary data in tail mode');
                return;
            }
            // Capture epoch BEFORE async operation to detect mode changes
            const captureEpoch = ctx.modeEpoch;
            const blobSize = event.data.size;
            console.log(`[WS] Binary: ${blobSize} bytes (epoch=${captureEpoch})`);

            event.data.arrayBuffer().then((buffer) => {
                // Check if mode changed during async blob read
                if (ctx.modeEpoch !== captureEpoch || ctx.outputMode !== 'full') {
                    console.debug(`[WS] Discarding stale binary (epoch ${captureEpoch} vs ${ctx.modeEpoch})`);
                    return;
                }
                queuedWrite(new Uint8Array(buffer));
                // Force ctx.terminal refresh on first data
                if (!_firstDataReceived) {
                    _firstDataReceived = true;
                    setTimeout(() => {
                        if (fitAddon) fitAddon.fit();
                        ctx.terminal.refresh(0, ctx.terminal.rows - 1);
                    }, 50);
                }
                updateLastActivity();
                if (statusOverlay && !statusOverlay.classList.contains('hidden')) {
                    statusOverlay.classList.add('hidden');
                }
            });
        } else {
            // Check for JSON messages (pong, queue updates, server ping, hello, tail, etc.)
            if (event.data.startsWith('{')) {
                try {
                    const msg = JSON.parse(event.data);
                    if (handleJsonMessage(msg)) return;
                } catch (e) {
                    // Not JSON, treat as ctx.terminal data
                }
            }
            // Text ctx.terminal data - only process in full mode (same as binary)
            if (ctx.outputMode !== 'full') {
                console.debug('Ignoring text data in tail mode');
                return;
            }
            queuedWrite(event.data);
            updateLastActivity();
            // Hide loading overlay when ctx.terminal data arrives
            if (statusOverlay && !statusOverlay.classList.contains('hidden')) {
                statusOverlay.classList.add('hidden');
            }
        }
    };

    ctx.socket.onclose = (event) => {
        clearTimeout(wsUpgradeTimer);
        console.log('WebSocket closed:', event.code, event.reason);
        isConnecting = false;
        reconnectInProgress = false;
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
            if (repoSwitchInProgress) {
                // switchRepo() manages its own reconnect — don't double-schedule
                console.log('4003 during repo switch, suppressing auto-reconnect');
                return;
            }
            // Target switch (not repo switch) - reconnect after rate limit window
            console.log('Target switch: server closed connection, reconnecting...');
            statusText.textContent = 'Switching target...';
            statusOverlay.classList.remove('hidden');
            // Wait 600ms to clear server's 500ms rate limit window
            reconnectDelay = INITIAL_RECONNECT_DELAY;
            reconnectTimer = setTimeout(() => {
                if (_preferSSE) connectSSE(); else connect();
            }, 600);
            return;
        }

        // Rate limited (4004) - wait longer before retry
        if (event.code === 4004) {
            console.log('Rate limited by server, waiting before retry');
            reconnectDelay = Math.max(reconnectDelay, 2000);
        }

        // PTY died (4500) - ctx.terminal process died, will be recreated on reconnect
        if (event.code === 4500) {
            console.warn('PTY died - terminal process ended');
            statusText.textContent = 'Terminal process ended. Reconnecting...';
            statusOverlay.classList.remove('hidden');
            // Reconnect immediately - server will recreate PTY
            reconnectDelay = INITIAL_RECONNECT_DELAY;
        }

        // Abnormal close (1006) — likely HTTP/2 proxy broke WS upgrade; switch to SSE
        if (event.code === 1006 && !_preferSSE) {
            console.warn('[WS] Abnormal close (1006), switching to SSE transport');
            _preferSSE = true;
            sessionStorage.setItem(_transportKey, 'sse');
        }

        // Track reconnect attempts
        reconnectAttempts++;

        // Auto-reload after repeated failures to force a fresh connection.
        if (reconnectAttempts >= AUTO_RELOAD_AFTER) {
            const lastReload = parseInt(sessionStorage.getItem('_mto_reload_ts') || '0', 10);
            if (Date.now() - lastReload > 30000) {
                console.log(`Auto-reload after ${reconnectAttempts} failed reconnects`);
                sessionStorage.setItem('_mto_reload_ts', String(Date.now()));
                location.reload();
                return;
            }
        }

        // Clear any existing overlay timer before scheduling new one
        if (reconnectOverlayTimer) {
            clearTimeout(reconnectOverlayTimer);
            reconnectOverlayTimer = null;
        }

        // Grace period: delay showing overlay only after repeated failures.
        // connectionBanner handles the normal reconnecting indicator.
        reconnectOverlayTimer = setTimeout(() => {
            if (!ctx.socket || ctx.socket.readyState !== WebSocket.OPEN) {
                // Only show heavy overlay after multiple failures (hard refresh option)
                if (reconnectAttempts >= SHOW_HARD_REFRESH_AFTER) {
                    statusText.textContent = `Reconnecting... (attempt ${reconnectAttempts})`;
                    statusOverlay.classList.remove('hidden');
                    if (reconnectBtn) reconnectBtn.classList.remove('hidden');
                    const hardRefreshBtn = document.getElementById('hardRefreshBtn');
                    if (hardRefreshBtn) hardRefreshBtn.classList.remove('hidden');
                }
            }
            reconnectOverlayTimer = null;
        }, RECONNECT_OVERLAY_GRACE_MS);

        // Reconnect with exponential backoff (starts immediately, overlay is delayed)
        reconnectTimer = setTimeout(() => {
            updateConnectionBanner('reconnecting');
            if (_preferSSE) connectSSE(); else connect();
        }, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
    };

    ctx.socket.onerror = (error) => {
        clearTimeout(wsUpgradeTimer);
        console.error('WebSocket error:', error);
        isConnecting = false;
        statusText.textContent = `Connection error (readyState=${ctx.socket?.readyState})`;
    };
}

/**
 * Send ctx.terminal dimensions to server
 */
function sendResize() {
    if (ctx.terminal && fitAddon) {
        fitAddon.fit();
    }
    if (ctx.socket && ctx.socket.readyState === WebSocket.OPEN && ctx.terminal) {
        ctx.socket.send(JSON.stringify({
            type: 'resize',
            cols: ctx.terminal.cols,
            rows: ctx.terminal.rows,
        }));
    }
}

/**
 * Set output mode: 'tail' or 'full'
 * In tail mode, server sends lightweight text snapshots
 * In full mode, server sends raw PTY bytes for xterm rendering
 */
function setOutputMode(mode) {
    if (mode !== 'tail' && mode !== 'full') return;
    if (mode === ctx.outputMode) return;

    const oldMode = ctx.outputMode;
    ctx.outputMode = mode;
    ctx.modeEpoch++;  // Invalidate ALL in-flight operations from previous mode

    console.log(`[MODE] v245 Switching ${oldMode} -> ${mode} (epoch=${ctx.modeEpoch})`);

    // Clear queue on ANY mode change - prevents stale data from leaking
    writeQueue = [];
    queuedBytes = 0;
    draining = false;

    // No ctx.terminal.reset() — SIGWINCH redraw from server will overwrite
    // the screen with fresh content. Keeping old content visible avoids
    // a blank flash during the round-trip.

    if (ctx.socket && ctx.socket.readyState === WebSocket.OPEN) {
        ctx.socket.send(JSON.stringify({
            type: 'set_mode',
            mode: mode
        }));
    } else {
        console.warn(`[MODE] Cannot send set_mode - ctx.socket not open (state: ${ctx.socket?.readyState})`);
    }
}

/**
 * Send input to ctx.terminal (binary format, same as main ctx.terminal)
 */
const inputEncoder = new TextEncoder();

function sendInput(data) {
    if (isPreviewMode()) return;  // No input in preview mode
    if (ctx.socket && ctx.socket.readyState === WebSocket.OPEN) {
        if (ctx._transportType === 'sse') {
            sseSendInputBatched(data);
        } else {
            ctx.socket.send(inputEncoder.encode(data));
        }
    }
}

/**
 * Send text atomically via tmux send-keys (not PTY byte write).
 * Use this for all composed text input (input bar, prompt buttons, quick responses).
 * This avoids interleaving with PTY output stream.
 * @param {string} text - Text to send (empty string OK if just sending Enter)
 * @param {boolean} enter - Whether to send Enter after text (default true)
 */
function sendTextAtomic(text, enter = true) {
    if (isPreviewMode()) return;
    if (ctx.socket && ctx.socket.readyState === WebSocket.OPEN) {
        ctx.socket.send(JSON.stringify({ type: 'text', text: text, enter: enter }));
    }
}
// Expose for feature modules (backlog send-now)
ctx.sendTextAtomic = sendTextAtomic;

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

    // Also collapse/expand the action bar and dispatch bar
    const actionBar = document.getElementById('actionBar');
    if (actionBar) {
        actionBar.classList.toggle('collapsed', isCollapsed);
    }
    const dispatchBar = document.getElementById('teamDispatchBar');
    if (dispatchBar && !dispatchBar.classList.contains('hidden')) {
        dispatchBar.classList.toggle('collapsed', isCollapsed);
    }

    // When expanding in log or ctx.terminal view, also remove 'hidden' to ensure visibility
    if (!isCollapsed && (ctx.currentView === 'log' || ctx.currentView === 'terminal')) {
        controlBarsContainer.classList.remove('hidden');
    }

    // Don't resize - keeps ctx.terminal stable, prevents tmux reflow/corruption
}

/**
 * Toggle interactive mode (ctx.terminal keyboard passthrough)
 */
function toggleInteractiveMode() {
    interactiveMode = !interactiveMode;
    updateInteractiveBadge();

    if (interactiveMode) {
        // Focus ctx.terminal for keyboard input
        if (ctx.terminal) ctx.terminal.focus();
        resetInteractiveIdleTimer();
        showToast('Interactive mode ON — raw keyboard enabled', 'info', 2000);
    } else {
        clearInteractiveIdleTimer();
        showToast('Interactive mode OFF', 'info', 1500);
    }
}

/**
 * Update interactive mode badge visibility
 */
function updateInteractiveBadge() {
    const badge = document.getElementById('interactiveBadge');
    const toggleBtn = document.getElementById('interactiveToggle');
    if (badge) {
        badge.classList.toggle('hidden', !interactiveMode);
    }
    if (toggleBtn) {
        toggleBtn.classList.toggle('active', interactiveMode);
        toggleBtn.textContent = interactiveMode ? 'Interactive ON' : 'Interactive';
    }
}

/**
 * Reset interactive idle timer (auto-disable after 5 min)
 */
function resetInteractiveIdleTimer() {
    clearInteractiveIdleTimer();
    interactiveIdleTimer = setTimeout(() => {
        if (interactiveMode) {
            interactiveMode = false;
            updateInteractiveBadge();
            showToast('Interactive mode auto-disabled (idle)', 'info', 2000);
        }
    }, 5 * 60 * 1000);  // 5 minutes
}

/**
 * Clear interactive idle timer
 */
function clearInteractiveIdleTimer() {
    if (interactiveIdleTimer) {
        clearTimeout(interactiveIdleTimer);
        interactiveIdleTimer = null;
    }
}

/**
 * Setup ctx.terminal focus handling
 */
function setupTerminalFocus() {
    // Disable mobile IME composition - send characters directly without preview
    ctx.terminal.textarea.setAttribute('autocomplete', 'off');
    ctx.terminal.textarea.setAttribute('autocorrect', 'off');
    ctx.terminal.textarea.setAttribute('autocapitalize', 'off');
    ctx.terminal.textarea.setAttribute('spellcheck', 'false');
    // Only set inputmode='text' for soft keyboard devices
    // Physical keyboard devices (Titan 2) skip this to avoid soft keyboard popup
    if (!isPhysicalKb) {
        ctx.terminal.textarea.setAttribute('inputmode', 'text');
    }

    // Tap ctx.terminal to focus and show keyboard (mobile only — desktop uses input bar)
    terminalContainer.addEventListener('click', () => {
        if (ctx.uiMode !== 'desktop-multipane') {
            ctx.terminal.focus();
        }
    });
}

/**
 * Load configuration (with 5s timeout)
 */
async function loadConfig() {
    try {
        const response = await fetchWithTimeout(`/config`, {}, 5000);
        if (!response.ok) {
            console.error('Failed to load config');
            return;
        }
        ctx.config = await response.json();
        // Set agent type and display name from server config
        ctx.agentType = ctx.config.agent_type || 'claude';
        if (ctx.config.agent_name) {
            agentName = ctx.config.agent_name;
        }
        if (!paramFontSize && ctx.config.font_size && ctx.terminal) {
            ctx.terminal.options.fontSize = ctx.config.font_size;
            fitAddon.fit();
        }
        // Apply server-detected physical keyboard (Tailscale device detection)
        if (ctx.config.physical_kb && !isPhysicalKb) {
            isPhysicalKb = true;
            if (ctx.terminal?.textarea) {
                ctx.terminal.textarea.removeAttribute('inputmode');
            }
        }
        await populateUI();  // await to ensure ctx.targets are loaded before log view
    } catch (error) {
        if (error.name === 'AbortError') {
            console.warn('loadConfig timed out');
        } else {
            console.error('Error loading config:', error);
        }
    }
}

/**
 * Load current session from server (with 3s timeout)
 */
async function loadCurrentSession() {
    try {
        const response = await fetchWithTimeout(`/current-session`, {}, 3000);
        if (response.ok) {
            const data = await response.json();
            ctx.currentSession = data.session;
        }
    } catch (error) {
        if (error.name === 'AbortError') {
            console.warn('loadCurrentSession timed out');
        } else {
            console.error('Error loading current session:', error);
        }
    }
}

/**
 * Load and display .claude/CONTEXT.md in the context banner.
 * Fire-and-forget — must never block startup or WebSocket.
 */
let contextBannerRequestId = 0;

async function loadContextBanner() {
    const requestId = ++contextBannerRequestId;
    const banner = document.getElementById('contextBanner');
    const preview = document.getElementById('contextBannerPreview');
    const body = document.getElementById('contextBannerBody');
    if (!banner || !preview || !body) return;

    // Check per-repo dismiss flag
    const dismissKey = `mto_context_dismissed_${ctx.currentSession || ''}`;
    if (sessionStorage.getItem(dismissKey)) {
        banner.classList.add('hidden');
        return;
    }

    try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 3000);
        const response = await apiFetch(`/api/docs/context`, {
            signal: controller.signal,
        });
        clearTimeout(timeout);

        // Stale response — a newer request is in flight
        if (requestId !== contextBannerRequestId) return;

        if (!response.ok) { banner.classList.add('hidden'); return; }
        const data = await response.json();
        if (!data.exists || !data.content?.trim()) {
            banner.classList.add('hidden');
            return;
        }

        // Extract preview: first non-empty, non-heading line
        const lines = data.content.split('\n');
        const previewLine = lines.find(l => l.trim() && !l.startsWith('#')) || lines[0] || '';
        preview.textContent = previewLine.trim();

        // Body: cap at 4000 chars
        let bodyText = data.content.slice(0, 4000);
        if (data.content.length > 4000) bodyText += '\n... [truncated]';
        body.textContent = bodyText;
        body.classList.add('hidden');  // Start collapsed

        banner.classList.remove('hidden');
    } catch (e) {
        if (requestId === contextBannerRequestId) {
            banner.classList.add('hidden');
        }
    }
}

/**
 * Populate UI from ctx.config
 */
async function populateUI() {
    if (!ctx.config) return;

    // Populate role buttons - send directly to ctx.terminal
    if (ctx.config.role_prefixes && ctx.config.role_prefixes.length > 0) {
        roleBar.innerHTML = '';
        ctx.config.role_prefixes.forEach((role) => {
            const btn = document.createElement('button');
            btn.className = 'role-btn';
            btn.textContent = role.label;
            btn.addEventListener('click', () => {
                // Ensure ctx.terminal is focused/active before sending input
                if (ctx.terminal) ctx.terminal.focus();
                sendInput(role.insert);
            });
            roleBar.appendChild(btn);
        });
    }

    // Populate repo dropdown and recent repos quick-switcher
    populateRepoDropdown();
    populateRecentRepos();

    // Load target selector (for multi-pane sessions)
    // IMPORTANT: await to ensure ctx.activeTarget is set before log view loads
    await loadTargets();

    // Start Claude health polling if document is visible
    if (document.visibilityState === 'visible') {
        startAgentHealthPolling();
    }
}

/**
 * Update navigation label to show "repo • pane" format
 */
function updateNavLabel() {
    // Get current target info
    let currentTarget = null;
    let paneInfo = '';

    if (ctx.activeTarget && ctx.targets.length > 0) {
        currentTarget = ctx.targets.find(t => t.id === ctx.activeTarget);
        if (currentTarget) {
            paneInfo = currentTarget.window_name || ctx.activeTarget;
        } else {
            paneInfo = ctx.activeTarget;
        }
    } else if (ctx.targets.length === 1) {
        currentTarget = ctx.targets[0];
        paneInfo = currentTarget.window_name || currentTarget.id;
    }

    // Get repo label - match based on target's cwd, not just session
    let repoName = ctx.config?.session_name || 'Terminal';
    if (ctx.config && ctx.config.repos && currentTarget) {
        // Find repo whose path matches the target's cwd
        const matchingRepo = ctx.config.repos.find(r =>
            currentTarget.cwd && currentTarget.cwd.startsWith(r.path)
        );
        if (matchingRepo) {
            repoName = matchingRepo.label;
        } else {
            // No matching repo - use the directory name from cwd
            repoName = currentTarget.project || currentTarget.cwd?.split('/').pop() || repoName;
        }
    } else if (ctx.config && ctx.config.repos) {
        // Fallback: use first repo matching session
        const currentRepo = ctx.config.repos.find(r => r.session === ctx.currentSession);
        if (currentRepo) {
            repoName = currentRepo.label;
        }
    }

    // Combine: "repo • pane" or just "repo" if single pane with matching name
    if (paneInfo && (ctx.targets.length > 1 || paneInfo !== repoName)) {
        repoLabel.textContent = `${repoName} • ${paneInfo}`;
    } else {
        repoLabel.textContent = repoName;
    }
}

/**
 * Kill a non-active pane via the server API.
 */
async function killPane(targetId) {
    try {
        const resp = await apiFetch(`/api/pane/kill`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_id: targetId })
        });
        const data = await resp.json();
        if (resp.ok && data.success) {
            showToast(`Pane ${targetId} killed`, 'info', 2000);
            await loadTargets();
            populateRepoDropdown();
        } else {
            showToast(data.error || 'Failed to kill pane', 'error', 3000);
        }
    } catch (err) {
        showToast('Error killing pane', 'error', 3000);
        console.error('killPane error:', err);
    }
}

/**
 * Populate unified navigation dropdown
 * Sections: Current Session panes, Actions, Other Sessions
 */
function populateRepoDropdown() {
    const hasRepos = ctx.config && ((ctx.config.repos && ctx.config.repos.length > 0) || (ctx.config.workspace_dirs && ctx.config.workspace_dirs.length > 0));
    const hasMultiplePanes = ctx.targets.length > 1;
    const hasNewWindow = hasRepos || hasMultiplePanes;  // Always allow new window if there are panes
    const hasContent = hasNewWindow;

    // Update nav label
    updateNavLabel();

    // Hide arrow if nothing to show
    if (!hasContent) {
        repoBtn.querySelector('.repo-arrow').style.display = 'none';
        return;
    }

    repoBtn.querySelector('.repo-arrow').style.display = '';
    repoDropdown.innerHTML = '';

    // Build set of team target IDs so we can skip them in "Current Session"
    // Always build this set (even if team is in another repo) to hide team panes from session list
    let teamTargetIds = new Set();
    if (ctx.teamState && ctx.teamState.has_team && ctx.teamState.team) {
        const allMembers = [ctx.teamState.team.leader, ...(ctx.teamState.team.agents || [])].filter(Boolean);
        teamTargetIds = new Set(allMembers.map(a => a.target_id));
    }

    // Team agents are shown in the team screen only — not in the pane switcher.
    // (teamTargetIds built above still filters them from "Current Session")

    // Section 1: Current Session panes (skip team panes)
    if (ctx.targets.length > 0) {
        const nonTeamTargets = ctx.targets.filter(t => !teamTargetIds.has(t.id));

        if (nonTeamTargets.length > 0) {
            const header = document.createElement('div');
            header.className = 'nav-section-header';
            header.textContent = 'Current Session';
            repoDropdown.appendChild(header);

            nonTeamTargets.forEach((target) => {
                const opt = document.createElement('button');
                const isActive = target.id === ctx.activeTarget;
                opt.className = 'nav-pane-option' + (isActive ? ' active' : '');

                const shortPath = target.cwd.replace(/^\/home\/[^/]+/, '~');
                const windowName = target.window_name || '';

                // Check for layout name mismatch hint
                const dirName = target.cwd.split('/').filter(Boolean).pop() || '';
                const normDir = dirName.toLowerCase().replace(/[^a-z0-9]/g, '');
                const normWindow = windowName.toLowerCase().replace(/-[a-f0-9]{4,}$/, '').replace(/[^a-z0-9]/g, '');
                const nameMatches = normDir && normWindow && (normWindow.includes(normDir) || normDir.includes(normWindow));
                const hintBadge = (!nameMatches && windowName && dirName) ? '<span class="target-name-hint" title="Window name differs from directory">?</span>' : '';

                const checkMark = isActive ? '<span class="nav-check">✓</span>' : '';
                const killBtn = isActive ? '' : '<span class="nav-kill-btn" aria-label="Kill pane"></span>';

                opt.innerHTML = `
                    <div class="nav-pane-content">
                        ${checkMark}<span class="nav-project">${escapeHtml(target.project)}</span>
                        <span class="nav-pane-info">${escapeHtml(windowName)}${hintBadge} • ${escapeHtml(target.pane_id)}</span>
                        <span class="nav-path">${escapeHtml(shortPath)}</span>
                    </div>
                    ${killBtn}
                `;
                opt.addEventListener('click', () => selectTarget(target.id));

                // Attach kill handler with two-tap confirmation
                const killEl = opt.querySelector('.nav-kill-btn');
                if (killEl) {
                    let confirmTimer = null;
                    killEl.addEventListener('click', (e) => {
                        e.stopPropagation();
                        if (killEl.classList.contains('confirm')) {
                            // Second tap — actually kill
                            clearTimeout(confirmTimer);
                            killPane(target.id);
                        } else {
                            // First tap — enter confirm state
                            killEl.classList.add('confirm');
                            // Revert after 3s if no second tap
                            confirmTimer = setTimeout(() => {
                                killEl.classList.remove('confirm');
                            }, 3000);
                        }
                    });
                }

                repoDropdown.appendChild(opt);
            });
        }
    }

    // Section 2: Actions (+ New Window)
    if (hasNewWindow) {
        if (ctx.targets.length > 0) {
            const divider = document.createElement('div');
            divider.className = 'nav-section-divider';
            repoDropdown.appendChild(divider);
        }

        const newWindowOpt = document.createElement('button');
        newWindowOpt.className = 'nav-action-option';
        newWindowOpt.textContent = '+ New Window in Repo...';
        newWindowOpt.addEventListener('click', () => {
            repoDropdown.classList.add('hidden');
            showNewWindowModal();
        });
        repoDropdown.appendChild(newWindowOpt);
    }

    // Section 3: Other Sessions
    if (hasRepos) {
        // Get sessions that are not current
        const otherRepos = ctx.config.repos.filter(r => r.session !== ctx.currentSession);

        // Also add default session if not in repos and not current
        const defaultInRepos = ctx.config.repos.some(r => r.session === ctx.config.session_name);
        const otherSessions = [...otherRepos];
        if (!defaultInRepos && ctx.config.session_name !== ctx.currentSession) {
            otherSessions.unshift({ label: ctx.config.session_name, path: 'Default', session: ctx.config.session_name });
        }

        if (otherSessions.length > 0) {
            const divider = document.createElement('div');
            divider.className = 'nav-section-divider';
            repoDropdown.appendChild(divider);

            const header = document.createElement('div');
            header.className = 'nav-section-header';
            header.textContent = 'Other Sessions';
            repoDropdown.appendChild(header);

            otherSessions.forEach((repo) => {
                const opt = document.createElement('button');
                opt.className = 'nav-session-option';
                opt.innerHTML = `
                    <div>
                        <span class="nav-session-label">${escapeHtml(repo.label)}</span>
                        <span class="nav-session-path">${escapeHtml(repo.path)}</span>
                    </div>
                    <span class="reconnect-pill">Switch</span>
                `;
                opt.addEventListener('click', () => switchRepo(repo.session));
                repoDropdown.appendChild(opt);
            });
        }
    }

    // Section: Agent actions (Claude only, when agent is idle)
    if (ctx.agentType === 'claude') {
        const actionsDivider = document.createElement('div');
        actionsDivider.className = 'nav-section-divider';
        repoDropdown.appendChild(actionsDivider);

        const actionsHeader = document.createElement('div');
        actionsHeader.className = 'nav-section-header';
        actionsHeader.textContent = 'Agent';
        repoDropdown.appendChild(actionsHeader);

        // Continue (most recent session) — one tap
        const continueBtn = document.createElement('button');
        continueBtn.className = 'nav-action-option';
        continueBtn.textContent = 'Continue Last Session';
        continueBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const paneId = ctx.activeTarget;
            continueBtn.textContent = 'Starting...';
            try {
                await apiFetch('/api/agent/start?pane_id=' + encodeURIComponent(paneId) + '&token=' + ctx.token, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ startup_command: 'claude --continue' }),
                });
                repoDropdown.classList.add('hidden');
                ctx.showToast('Continuing last session', 'success');
            } catch (err) {
                ctx.showToast(err.message || 'Failed', 'warning');
                continueBtn.textContent = 'Continue Last Session';
            }
        });
        repoDropdown.appendChild(continueBtn);

        // Resume Session — expandable picker
        const resumeBtn = document.createElement('button');
        resumeBtn.className = 'nav-action-option';
        resumeBtn.textContent = 'Resume Session...';
        let resumeExpanded = false;
        let resumeContainer = null;
        resumeBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            if (resumeExpanded && resumeContainer) {
                resumeContainer.remove();
                resumeContainer = null;
                resumeExpanded = false;
                resumeBtn.textContent = 'Resume Session...';
                return;
            }
            resumeBtn.textContent = 'Loading...';
            try {
                const resp = await apiFetch('/api/log/sessions');
                const data = await resp.json();
                const sessions = (data.sessions || []).slice(0, 8);
                if (sessions.length === 0) {
                    resumeBtn.textContent = 'No sessions found';
                    setTimeout(() => { resumeBtn.textContent = 'Resume Session...'; }, 2000);
                    return;
                }
                resumeBtn.textContent = 'Resume Session...';
                resumeExpanded = true;
                resumeContainer = document.createElement('div');
                resumeContainer.className = 'nav-resume-list';
                sessions.forEach(s => {
                    const sBtn = document.createElement('button');
                    sBtn.className = 'nav-resume-item';
                    const shortId = s.id.substring(0, 8);
                    const preview = (s.preview || '').substring(0, 50);
                    const label = document.createElement('span');
                    label.className = 'nav-resume-label';
                    label.textContent = shortId + (s.is_current ? ' (current)' : '');
                    const desc = document.createElement('span');
                    desc.className = 'nav-resume-preview';
                    desc.textContent = preview;
                    sBtn.appendChild(label);
                    sBtn.appendChild(desc);
                    sBtn.addEventListener('click', async (ev) => {
                        ev.stopPropagation();
                        sBtn.textContent = 'Starting...';
                        const paneId = ctx.activeTarget;
                        try {
                            await apiFetch('/api/agent/start?pane_id=' + encodeURIComponent(paneId) + '&token=' + ctx.token, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ startup_command: 'claude --resume ' + s.id }),
                            });
                            repoDropdown.classList.add('hidden');
                            ctx.showToast('Resuming session ' + shortId, 'success');
                        } catch (err) {
                            ctx.showToast(err.message || 'Failed', 'warning');
                            sBtn.textContent = shortId;
                        }
                    });
                    resumeContainer.appendChild(sBtn);
                });
                resumeBtn.after(resumeContainer);
            } catch (err) {
                resumeBtn.textContent = 'Error loading sessions';
                setTimeout(() => { resumeBtn.textContent = 'Resume Session...'; }, 2000);
            }
        });
        repoDropdown.appendChild(resumeBtn);
    }

    const restartBtn = document.createElement('button');
    restartBtn.className = 'nav-action-option nav-restart-btn';
    restartBtn.textContent = 'Restart Server';
    let restartConfirmState = false;
    let restartConfirmTimer = null;
    restartBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!restartConfirmState) {
            restartConfirmState = true;
            restartBtn.textContent = 'Tap again to confirm';
            restartBtn.classList.add('confirm');
            restartConfirmTimer = setTimeout(() => {
                restartConfirmState = false;
                restartBtn.textContent = 'Restart Server';
                restartBtn.classList.remove('confirm');
            }, 3000);
            return;
        }
        clearTimeout(restartConfirmTimer);
        restartBtn.textContent = 'Restarting...';
        restartBtn.disabled = true;
        repoDropdown.classList.add('hidden');
        try {
            await apiFetch('/api/restart', { method: 'POST' });
        } catch (_) {}
    });
    repoDropdown.appendChild(restartBtn);

    // High-contrast toggle
    const hcBtn = document.createElement('button');
    hcBtn.className = 'nav-action-option';
    const isHC = document.documentElement.classList.contains('high-contrast');
    hcBtn.textContent = isHC ? 'Normal Contrast' : 'High Contrast';
    hcBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const on = document.documentElement.classList.toggle('high-contrast');
        localStorage.setItem('mto_high_contrast', on ? '1' : '');
        hcBtn.textContent = on ? 'Normal Contrast' : 'High Contrast';
        repoDropdown.classList.add('hidden');
    });
    repoDropdown.appendChild(hcBtn);
}

/**
 * Switch to a different repo/session
 */
async function switchRepo(session) {
    if (session === ctx.currentSession) {
        repoDropdown.classList.add('hidden');
        return;
    }

    statusText.textContent = 'Switching...';
    statusOverlay.classList.remove('hidden');
    repoDropdown.classList.add('hidden');

    // Set flags BEFORE API call - server will close WebSocket with 4003
    intentionalClose = true;
    repoSwitchInProgress = true;
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    reconnectDelay = INITIAL_RECONNECT_DELAY;

    try {
        const response = await apiFetch(`/switch-repo?session=${encodeURIComponent(session)}`, {
            method: 'POST',
        });

        if (!response.ok) {
            throw new Error('Failed to switch repo');
        }

        ctx.currentSession = session;

        // Clear target selection (pane IDs are session-specific)
        ctx.activeTarget = null;
        localStorage.removeItem('mto_active_target');

        // Update unified nav label
        populateRecentRepos();
        updateNavLabel();

        // Immediately hide team UI — new session has no targets yet so
        // isTeamInCurrentRepo() returns false. Prevents stale team bar/tabs.
        updateLogFilterBarVisibility();
        updateTabIndicator();
        if (ctx.currentView === 'team') {
            switchToView('log');
        }
        const sysStrip = document.getElementById('systemStatusStrip');
        if (sysStrip) sysStrip.classList.add('hidden');

        // Reset log state to force fresh load
        logLoaded = false;
        lastLogModified = 0;
        lastLogContentHash = '';

        // Server already closed WebSocket — reconnect just above the 500ms
        // server rate-limit window. Defer terminal/log clearing until
        // reconnect starts so old content stays visible during the gap.
        setTimeout(() => {
            repoSwitchInProgress = false;
            // Clear now, right before reconnect — minimizes blank-screen time
            if (ctx.terminal) ctx.terminal.clear();
            if (logContent) logContent.innerHTML = '<div class="loading">Switching session...</div>';
            if (_preferSSE) connectSSE(); else connect();
            // Refresh log, ctx.targets, and queue after connection established
            setTimeout(async () => {
                await loadTargets();
                loadLogContent();  // Full reload, not incremental refresh
                await reconcileQueue();  // Reconcile queue for new session
                reloadBacklogForProject('');
                // Refresh context banner for new repo
                sessionStorage.removeItem(`mto_context_dismissed_${session}`);
                loadContextBanner().catch(() => {});
            }, 500);
        }, 600);

    } catch (error) {
        console.error('Error switching repo:', error);
        intentionalClose = false;  // Reset on error
        repoSwitchInProgress = false;
        statusText.textContent = 'Switch failed';
        setTimeout(() => {
            statusOverlay.classList.add('hidden');
        }, 2000);
    }
}

/**
 * Populate the collapse-row quick-switcher with panes (targets).
 * Shows other panes in the current session for fast switching.
 */
function populateRecentRepos() {
    const container = document.getElementById('recentRepos');
    if (!container) return;
    container.innerHTML = '';

    // Need multiple targets to show switcher
    if (ctx.targets.length < 2) return;

    // Build team target set to annotate buttons
    const teamTargetIds = new Set();
    if (ctx.teamState?.has_team && ctx.teamState.team) {
        if (ctx.teamState.team.leader) teamTargetIds.add(ctx.teamState.team.leader.target_id);
        for (const a of ctx.teamState.team.agents) teamTargetIds.add(a.target_id);
    }

    // Filter out team panes from quick-switcher
    const nonTeamTargets = ctx.targets.filter(t => !teamTargetIds.has(t.id));
    nonTeamTargets.forEach(target => {
        const isActive = target.id === ctx.activeTarget;
        const btn = document.createElement('button');
        btn.className = 'recent-repo-btn' + (isActive ? ' current' : '');
        // Label: project name or window name, keep it short
        const label = target.project || target.window_name || target.id;
        btn.textContent = label;
        btn.title = target.cwd || target.id;
        if (!isActive) {
            btn.addEventListener('click', () => selectTarget(target.id));
        }
        container.appendChild(btn);
    });

    // Update sidebar sessions if in desktop mode
    if (ctx.uiMode === 'desktop-multipane') populateSidebarSessions();
}

/**
 * Toggle unified nav dropdown visibility
 */
function toggleRepoDropdown() {
    const hasRepos = ctx.config && ((ctx.config.repos && ctx.config.repos.length > 0) || (ctx.config.workspace_dirs && ctx.config.workspace_dirs.length > 0));
    const hasMultiplePanes = ctx.targets.length > 1;

    // Only show dropdown if there's content
    if (!hasRepos && !hasMultiplePanes) {
        return;
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
 * Load available ctx.targets (panes) in current session
 */
async function loadTargets() {
    try {
        const response = await fetchWithTimeout(`/api/targets`, {}, 5000);
        if (!response.ok) return;

        const data = await response.json();
        ctx.targets = data.targets || [];
        ctx.activeTarget = data.active;

        // Get expected repo path from current repo ctx.config
        if (ctx.config && ctx.config.repos) {
            const currentRepo = ctx.config.repos.find(r => r.session === ctx.currentSession);
            expectedRepoPath = currentRepo ? currentRepo.path : null;
        }

        // Update unified nav label and pane quick-switcher
        updateNavLabel();
        populateRecentRepos();

        // Check if locked target still exists
        if (targetLocked && ctx.activeTarget && !data.active_exists) {
            showTargetMissingWarning();
        }

    } catch (error) {
        if (error.name === 'AbortError') {
            console.warn('loadTargets timed out');
        } else {
            console.error('Error loading targets:', error);
        }
    }
}

/**
 * Show warning when locked target pane no longer exists (clears invalid target)
 */
function showTargetMissingWarning() {
    ctx.activeTarget = null;
    localStorage.removeItem('mto_active_target');
    showToast('Target pane no longer exists. Select a new target.', 'warning');
}

/**
 * Shift+Tab session cycling: preview mode
 * Shift+Tab highlights session buttons in the workspace sidebar.
 * Enter confirms the previewed target. Escape or timeout cancels.
 */
let _cyclePreviewIdx = -1;
let _cyclePreviewTimer = null;

function _getSidebarSessionBtns() {
    return Array.from(document.querySelectorAll('#sidebarSessionsBody .sidebar-session-btn'));
}

function cycleTargetPreview() {
    const btns = _getSidebarSessionBtns();
    if (btns.length <= 1) return;

    if (_cyclePreviewIdx < 0) {
        // Start from current active
        _cyclePreviewIdx = btns.findIndex(b => b.classList.contains('current'));
    }
    _cyclePreviewIdx = (_cyclePreviewIdx + 1) % btns.length;

    // Highlight the previewed button
    btns.forEach(b => b.classList.remove('cycle-preview'));
    btns[_cyclePreviewIdx].classList.add('cycle-preview');
    btns[_cyclePreviewIdx].scrollIntoView({ block: 'nearest' });

    // Reset auto-cancel timer (3s)
    clearTimeout(_cyclePreviewTimer);
    _cyclePreviewTimer = setTimeout(cancelCyclePreview, 3000);
}

function confirmCyclePreview() {
    if (_cyclePreviewIdx < 0) return false;
    const btns = _getSidebarSessionBtns();
    const btn = btns[_cyclePreviewIdx];
    clearTimeout(_cyclePreviewTimer);
    _cyclePreviewIdx = -1;
    btns.forEach(b => b.classList.remove('cycle-preview'));
    if (btn && !btn.classList.contains('current')) {
        btn.click();
    }
    return true;
}

function cancelCyclePreview() {
    if (_cyclePreviewIdx < 0) return;
    clearTimeout(_cyclePreviewTimer);
    _cyclePreviewIdx = -1;
    _getSidebarSessionBtns().forEach(b => b.classList.remove('cycle-preview'));
}

/**
 * Select a target pane (optimistic - applies locally first, syncs in background)
 */
let targetSelectController = null;
async function selectTarget(targetId, isInitialSync = false) {
    repoDropdown.classList.add('hidden');

    if (targetId === ctx.activeTarget && !isInitialSync) return;

    // Cancel any in-flight target select request
    if (targetSelectController) targetSelectController.abort();
    targetSelectController = new AbortController();

    const previousTarget = ctx.activeTarget;

    // Save current pane's queue before switching
    if (!isInitialSync) {
        saveQueueToStorage();
    }

    // === OPTIMISTIC: Apply target locally immediately ===
    ctx.activeTarget = targetId;
    localStorage.setItem('mto_active_target', targetId);
    updateNavLabel();
    populateRecentRepos();

    // Immediately update team-scoped UI for the new target's repo
    updateLogFilterBarVisibility();
    updateTabIndicator();
    if (!isTeamInCurrentRepo() && ctx.currentView === 'team') {
        switchToView('log');
    }

    // Load new target's queue from localStorage
    reloadQueueForTarget();
    // Background reconcile with server for the new pane
    reconcileQueue();
    // Reload backlog for current project (server resolves project path)
    reloadBacklogForProject('');

    // Save draft for previous pane, restore draft for new pane
    if (logInput) {
        if (previousTarget && logInput.value && logInput.dataset.autoSuggestion !== 'true') {
            sessionStorage.setItem(`mto_draft_${previousTarget}`, logInput.value);
        }
        const saved = sessionStorage.getItem(`mto_draft_${targetId}`) || '';
        logInput.value = saved;
        logInput.dataset.autoSuggestion = 'false';
        if (saved) sessionStorage.removeItem(`mto_draft_${targetId}`);
    }
    lastSuggestion = '';
    recentSentCommands.clear();
    lastContextPct = -1;
    contextAlertSent = false;
    const ctxPill = document.getElementById('contextPill');
    if (ctxPill) ctxPill.classList.add('hidden');

    // Reset Claude health state for new target
    lastAgentHealth = null;
    agentStartedAt = null;
    updateAgentCrashBanner(false);

    // Show brief loading indicator (non-blocking)
    if (statusOverlay && statusText && !isInitialSync) {
        statusText.textContent = 'Switching to target...';
        statusOverlay.classList.remove('hidden');
    }

    // === BACKGROUND: Sync with server (don't block on this) ===
    try {
        const response = await fetchWithTimeout(
            `/api/target/select?target_id=${encodeURIComponent(targetId)}`,
            { method: 'POST', signal: targetSelectController.signal },
            8000  // 8s timeout for target select
        );

        if (response.status === 409) {
            // Target no longer exists - revert and show error
            console.warn(`Target ${targetId} not found on server`);
            ctx.activeTarget = previousTarget;
            localStorage.setItem('mto_active_target', previousTarget || '');
            updateNavLabel();
            showToast('Target pane not found', 'error');
            loadTargets();  // Refresh list in background
            return;
        }

        if (!response.ok) {
            throw new Error(`Server returned ${response.status}`);
        }

        const data = await response.json();
        console.log(`Target sync success: ${targetId} (epoch=${data.epoch})`);

        // === Hard context switch: clear ctx.terminal and force WebSocket reconnect ===
        if (ctx.terminal && !isInitialSync) {
            ctx.terminal.clear();
            ctx.terminal.reset();
        }

        // Server keeps WebSocket alive — PTY output naturally follows tmux pane switch.
        // Hide status overlay since we're already connected.
        if (statusOverlay && !isInitialSync) {
            statusOverlay.classList.add('hidden');
        }

        // Start health polling if visible
        if (document.visibilityState === 'visible') {
            startAgentHealthPolling();
        }

        // Reload ctx.targets to check cwd mismatch (background, don't await)
        loadTargets();

        // Re-fetch queue and backlog now that server has confirmed the target switch
        reconcileQueue();
        reloadBacklogForProject('');
        if (ctx.uiMode === 'desktop-multipane') updateSidebarCounts();

        // Reset log state so the new pane gets a fresh load
        if (!isInitialSync) {
            logLoaded = false;
            lastLogModified = 0;
            lastLogContentHash = '';

            // Clear log DOM immediately to avoid showing stale content
            if (logContent) {
                logContent.innerHTML = '<div class="log-loading">Loading...</div>';
            }

            // Load log content directly. Earlier code waited 500ms here
            // for "WebSocket reconnect to settle" — but the WS does NOT
            // reconnect on target switch (server keeps it alive and
            // swaps the tmux pane underneath, see server.py:select_target),
            // so the wait was pure dead delay.
            loadLogContent();
        }

    } catch (error) {
        // Handle timeout or network errors gracefully
        if (error.name === 'AbortError') {
            console.warn(`Target select timed out for ${targetId}`);
            showToast('Target sync timed out - using cached', 'warning');
        } else {
            console.error('Error selecting target:', error);
            showToast('Target sync failed - using cached', 'warning');
        }
        // Keep the optimistic local state - don't revert
        // Server will catch up on next request
    } finally {
        // Always hide overlay
        if (statusOverlay) {
            statusOverlay.classList.add('hidden');
        }
    }
}

/**
 * CD to expected repo root in the target pane
 */
/**
 * Check Claude health for the active pane
 */
async function checkAgentHealth() {
    if (!ctx.activeTarget) return;

    // Don't poll when document is hidden
    if (document.visibilityState !== 'visible') return;

    try {
        const response = await apiFetch(`/api/health/agent?pane_id=${encodeURIComponent(ctx.activeTarget)}`);
        if (!response.ok) return;

        const health = await response.json();
        const wasRunning = lastAgentHealth?.running;
        const isNowRunning = health.running;

        lastAgentHealth = health;

        // Track when agent started running
        if (isNowRunning && !wasRunning) {
            agentStartedAt = Date.now();
            // Clear any pending crash debounce
            if (agentCrashDebounceTimer) {
                clearTimeout(agentCrashDebounceTimer);
                agentCrashDebounceTimer = null;
            }
            // Hide crash banner if shown
            updateAgentCrashBanner(false);
        }

        // Detect crash: was running, now not, and was running for at least 3s
        // Skip crash detection if team was just dismissed (teamState cleared)
        if (wasRunning && !isNowRunning && agentStartedAt && ctx.teamState !== null) {
            const runDuration = Date.now() - agentStartedAt;
            if (runDuration > 3000) {
                // Debounce crash detection by 3s to avoid false positives
                if (!agentCrashDebounceTimer) {
                    agentCrashDebounceTimer = setTimeout(() => {
                        agentCrashDebounceTimer = null;
                        // Re-check health before showing banner
                        checkAgentHealthAndShowBanner();
                    }, 3000);
                }
            }
        }

    } catch (error) {
        console.error('Error checking claude health:', error);
    }
}

/**
 * Re-check health and show crash banner if Claude is still not running
 */
async function checkAgentHealthAndShowBanner() {
    if (!ctx.activeTarget) return;

    try {
        const response = await apiFetch(`/api/health/agent?pane_id=${encodeURIComponent(ctx.activeTarget)}`);
        if (!response.ok) return;

        const health = await response.json();
        lastAgentHealth = health;

        if (!health.running && !dismissedCrashPanes.has(ctx.activeTarget)) {
            updateAgentCrashBanner(true);
        }
    } catch (error) {
        console.error('Error re-checking agent health:', error);
    }
}

/**
 * Show or hide the agent crash banner
 */
function updateAgentCrashBanner(show) {
    if (!agentCrashBanner) return;

    if (show) {
        const msgEl = document.getElementById('agentCrashMsg');
        if (msgEl) msgEl.textContent = `${agentName} has stopped. Respawn?`;
        agentCrashBanner.classList.remove('hidden');
    } else {
        agentCrashBanner.classList.add('hidden');
    }
}

/**
 * Respawn agent in the active pane
 */
async function respawnAgent() {
    if (!ctx.activeTarget) return;

    updateAgentCrashBanner(false);

    try {
        // Find repo for current target to get startup command
        const targetInfo = ctx.targets.find(t => t.id === ctx.activeTarget);
        let repoLabel = null;
        if (targetInfo && ctx.config?.repos) {
            const matchingRepo = ctx.config.repos.find(r =>
                targetInfo.cwd && targetInfo.cwd.startsWith(r.path)
            );
            if (matchingRepo) {
                repoLabel = matchingRepo.label;
            }
        }

        const body = repoLabel ? JSON.stringify({ repo_label: repoLabel }) : '{}';

        const response = await apiFetch(`/api/agent/start?pane_id=${encodeURIComponent(ctx.activeTarget)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: body,
        });

        if (response.status === 409) {
            showToast(`${agentName} is already running`, 'info');
            return;
        }

        if (!response.ok) {
            const data = await response.json();
            showToast(data.error || `Failed to start ${agentName}`, 'error');
            return;
        }

        showToast(`${agentName} started`, 'success');

        // Reset health state
        agentStartedAt = Date.now();
        lastAgentHealth = null;

    } catch (error) {
        console.error('Error respawning agent:', error);
        showToast(`Failed to start ${agentName}`, 'error');
    }
}

/**
 * Fetch team state (phase + git info for all team panes)
 */
async function updateTeamState() {
    const hadTeamHere = isTeamInCurrentRepo();
    try {
        const sessParam = ctx.currentSession ? `&session=${encodeURIComponent(ctx.currentSession)}` : '';
        const resp = await fetchWithTimeout(
            `/api/team/state${sessParam}`, {}, 5000
        );
        if (!resp.ok) { ctx.teamState = null; return; }
        ctx.teamState = await resp.json();
        // Re-render dropdown if it's visible
        if (!repoDropdown.classList.contains('hidden')) {
            populateRepoDropdown();
        }
    } catch {
        ctx.teamState = null;
    }
    const hasTeamHere = isTeamInCurrentRepo();
    // Detect team-in-current-repo transitions (covers both global team changes
    // and switching between repos with/without team)
    if (hadTeamHere !== hasTeamHere) {
        updateTabIndicator();
        updateLogFilterBarVisibility();
        // If team not in this repo while viewing team, switch to log
        if (!hasTeamHere && ctx.currentView === 'team') {
            switchToView('log');
        }
        // Hide system strip when team not in this repo
        if (!hasTeamHere) {
            const sysStrip = document.getElementById('systemStatusStrip');
            if (sysStrip) sysStrip.classList.add('hidden');
        }
    }
    // Update sidebar counts when in desktop mode
    if (ctx.uiMode === 'desktop-multipane') updateSidebarCounts();
}

/**
 * Start Claude health polling - singleflight async loop
 * Only one request in flight at a time, pauses when document hidden
 */
async function startAgentHealthPolling() {
    // Stop any existing loop
    stopAgentHealthPolling();
    agentHealthController = new AbortController();
    const signal = agentHealthController.signal;

    // Singleflight async loop - only one request at a time
    while (!signal.aborted) {
        try {
            // Only poll when document is visible
            if (document.visibilityState === 'visible') {
                await Promise.all([checkAgentHealth(), updateAgentPhase(), updateTeamState()]);
            }
            await abortableSleep(HEALTH_POLL_INTERVAL, signal);
        } catch (error) {
            if (error.name === 'AbortError') break;
            console.debug('Health poll loop error:', error);
            // Wait before retry on error
            try { await abortableSleep(2000, signal); } catch { break; }
        }
    }
}

/**
 * Stop Claude health polling
 */
function stopAgentHealthPolling() {
    if (agentHealthController) {
        agentHealthController.abort();
        agentHealthController = null;
    }
    if (agentCrashDebounceTimer) {
        clearTimeout(agentCrashDebounceTimer);
        agentCrashDebounceTimer = null;
    }
}

/**
 * Fetch Claude phase and update status strip
 */
async function updateAgentPhase() {
    const indicator = document.getElementById('headerPhaseIndicator');
    const dot = document.getElementById('headerPhaseDot');
    const labelEl = document.getElementById('headerPhaseLabel');
    if (!indicator) return;

    try {
        const paneParam = ctx.activeTarget ? `?pane_id=${encodeURIComponent(ctx.activeTarget)}` : '';
        const response = await apiFetch(`/api/status/phase${paneParam}`);
        if (!response.ok) return;

        const data = await response.json();
        updateContextFromBackend(data);
        updateProcessesPill(data.descendant_count);
        const prevPhase = lastPhase?.phase;
        lastPhase = data;

        const phase = data.phase;
        const agentRunning = data.agent_running ?? data.claude_running;

        // Hide indicator when idle and agent not running
        if (phase === 'idle' && !agentRunning) {
            indicator.classList.add('hidden');
            return;
        }

        // Show indicator
        indicator.classList.remove('hidden');

        // Update dot
        if (dot) dot.className = 'status-dot ' + phase;

        // Phase labels
        const phaseLabels = {
            waiting: 'Needs Input',
            planning: 'Planning',
            working: 'Working',
            running_task: 'Agent',
            idle: 'Idle',
        };
        if (labelEl) labelEl.textContent = phaseLabels[phase] || phase;

        // (Auto-open drawer on idle transition removed — was disruptive)

    } catch (error) {
        console.debug('Phase update error:', error);
    }
}

/**
 * Load available repos for new window creation
 */
async function loadRepos() {
    try {
        const [reposResp, wsDirsResp] = await Promise.all([
            apiFetch(`/api/repos`),
            apiFetch(`/api/workspace/dirs`)
        ]);
        if (reposResp.ok) {
            const data = await reposResp.json();
            availableRepos = data.repos || [];
        }
        if (wsDirsResp.ok) {
            const data = await wsDirsResp.json();
            availableWorkspaceDirs = data.dirs || [];
        }
    } catch (error) {
        console.error('Error loading repos:', error);
    }
}

/**
 * Show the new window modal
 */
async function showNewWindowModal() {
    // Always reload to pick up new directories
    await loadRepos();

    // Populate repo selector with optgroups
    newWindowRepo.innerHTML = '';

    const hasRepos = availableRepos.length > 0;
    const hasWorkspaceDirs = availableWorkspaceDirs.length > 0;

    if (!hasRepos && !hasWorkspaceDirs) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'No repos or workspace dirs configured';
        newWindowRepo.appendChild(opt);
        newWindowCreate.disabled = true;
    } else {
        // Configured repos optgroup
        if (hasRepos) {
            const repoGroup = document.createElement('optgroup');
            repoGroup.label = 'Configured Repos';
            availableRepos.forEach(repo => {
                const opt = document.createElement('option');
                opt.value = 'repo:' + repo.label;
                opt.textContent = repo.label + (repo.exists ? '' : ' (path missing)');
                opt.disabled = !repo.exists;
                repoGroup.appendChild(opt);
            });
            newWindowRepo.appendChild(repoGroup);
        }

        // Workspace dirs optgroups (grouped by parent)
        if (hasWorkspaceDirs) {
            const byParent = {};
            availableWorkspaceDirs.forEach(d => {
                if (!byParent[d.parent]) byParent[d.parent] = [];
                byParent[d.parent].push(d);
            });
            Object.keys(byParent).forEach(parent => {
                const wsGroup = document.createElement('optgroup');
                wsGroup.label = parent;
                byParent[parent].forEach(d => {
                    const opt = document.createElement('option');
                    opt.value = 'dir:' + d.path;
                    opt.textContent = d.name;
                    wsGroup.appendChild(opt);
                });
                newWindowRepo.appendChild(wsGroup);
            });
        }

        newWindowCreate.disabled = false;
    }

    // Clear previous values
    newWindowName.value = '';
    newWindowAutoStart.checked = false;

    // Show modal
    newWindowModal.classList.remove('hidden');
}

/**
 * Hide the new window modal
 */
function hideNewWindowModal() {
    newWindowModal.classList.add('hidden');
}

/**
 * Create a new window in the selected repo
 */
async function createNewWindow() {
    const selectValue = newWindowRepo.value;
    const windowName = newWindowName.value.trim();
    const autoStartAgent = newWindowAutoStart.checked;

    if (!selectValue) {
        showToast('Please select a repo or directory', 'error');
        return;
    }

    // Build request body based on value prefix
    const bodyObj = {
        window_name: windowName,
        auto_start_agent: autoStartAgent
    };
    if (selectValue.startsWith('dir:')) {
        bodyObj.path = selectValue.slice(4);
    } else if (selectValue.startsWith('repo:')) {
        bodyObj.repo_label = selectValue.slice(5);
    } else {
        // Legacy fallback (shouldn't happen)
        bodyObj.repo_label = selectValue;
    }

    // Disable create button while processing
    newWindowCreate.disabled = true;
    newWindowCreate.textContent = 'Creating...';

    try {
        const response = await apiFetch(`/api/window/new`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(bodyObj)
        });

        const data = await response.json();

        if (!response.ok) {
            showToast(data.error || 'Failed to create window', 'error');
            return;
        }

        showToast(`Created window: ${data.window_name}`, 'success');
        hideNewWindowModal();

        // Try to select the new target with retries
        const newTargetId = data.target_id;
        const newPaneId = data.pane_id;

        // Retry logic: target may not appear immediately
        let retries = 5;
        const trySelectTarget = async () => {
            await loadTargets();

            // Check if the new target exists in the list
            const found = ctx.targets.find(t => t.id === newTargetId || t.pane_id === newPaneId);
            if (found) {
                await selectTarget(found.id);
                return true;
            }

            if (retries > 0) {
                retries--;
                setTimeout(trySelectTarget, 500);
                return false;
            }

            showToast('Window created but could not auto-select', 'warning');
            return false;
        };

        // Start retry loop after a short delay
        setTimeout(trySelectTarget, 300);

    } catch (error) {
        console.error('Error creating window:', error);
        showToast('Error creating window', 'error');
    } finally {
        newWindowCreate.disabled = false;
        newWindowCreate.textContent = 'Create';
    }
}

/**
 * Setup new window modal event listeners
 */
function setupNewWindowModal() {
    if (!newWindowModal) return;

    newWindowClose.addEventListener('click', hideNewWindowModal);
    newWindowCancel.addEventListener('click', hideNewWindowModal);
    newWindowCreate.addEventListener('click', createNewWindow);

    // Close on backdrop click
    newWindowModal.addEventListener('click', (e) => {
        if (e.target === newWindowModal) {
            hideNewWindowModal();
        }
    });

    // Submit on Enter in name field
    newWindowName.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            createNewWindow();
        }
    });
}

/**
 * Setup target selector event listeners
 * Note: Target button/dropdown are now unified into repo dropdown
 * Returns a promise that resolves when saved target is restored (if any)
 */
function setupTargetSelector() {
    // Claude crash banner buttons
    if (agentRespawnBtn) {
        agentRespawnBtn.addEventListener('click', respawnAgent);
    }
    if (agentCrashDismissBtn) {
        agentCrashDismissBtn.addEventListener('click', () => {
            // Dismiss for this pane only
            if (ctx.activeTarget) {
                if (dismissedCrashPanes.size > 500) dismissedCrashPanes.clear();
                dismissedCrashPanes.add(ctx.activeTarget);
            }
            updateAgentCrashBanner(false);
        });
    }

    // Restore saved state on startup
    const savedTarget = localStorage.getItem('mto_active_target');
    const savedLocked = localStorage.getItem('mto_target_locked');

    // Default to locked if not set
    targetLocked = savedLocked !== 'false';

    // Apply saved target OPTIMISTICALLY (locally only, don't block)
    // Server sync happens in background - connect() proceeds immediately
    if (savedTarget) {
        ctx.activeTarget = savedTarget;
        updateNavLabel();
        // Fire and forget - sync with server in background
        selectTarget(savedTarget, true).catch(err => {
            console.warn('Initial target sync failed:', err);
        });
    }

    // Don't return a promise - nothing to await
}

/**
 * Get target params for API calls (session + pane_id)
 */
function getTargetParams() {
    const params = new URLSearchParams();
    if (ctx.currentSession) params.append('session', ctx.currentSession);
    if (ctx.activeTarget) params.append('pane_id', ctx.activeTarget);
    return params.toString();
}

/**
 * Toggle target lock mode
 */
function toggleTargetLock() {
    targetLocked = !targetLocked;
    localStorage.setItem('mto_target_locked', targetLocked);

    if (targetLocked) {
        showToast('Target locked - stays on selected pane', 'success');
    } else {
        showToast('Follow mode - follows tmux active pane', 'warning');
    }
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

    // Sidebar toggle (desktop)
    const sidebarToggle = document.getElementById('sidebarToggle');
    if (sidebarToggle) {
        sidebarToggle.addEventListener('click', () => openToolPanel('team'));
    }

    // Queue "Run" button — send next queued command immediately
    const queueRunBtn = document.getElementById('queueSendNext');
    if (queueRunBtn) queueRunBtn.addEventListener('click', () => sendNextUnsafe());

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
            // Ensure ctx.terminal is focused/active before sending input
            if (ctx.terminal) ctx.terminal.focus();
            const keyName = btn.dataset.key;
            const key = keyMap[keyName] || keyName;
            sendInput(key);
        });
    });

    // Input buttons (numbers, arrows, y/n/enter) - use pointerup for better mobile support
    inputBar.querySelectorAll('.quick-btn').forEach((btn) => {
        btn.addEventListener('pointerup', (e) => {
            e.preventDefault();
            e.stopPropagation();
            // Ensure ctx.terminal is focused/active before sending input
            if (ctx.terminal) ctx.terminal.focus();
            const keyName = btn.dataset.key;
            const key = keyMap[keyName] || keyName;

            // Clear: clear input box and ctx.terminal command line
            if (keyName === 'clear') {
                // Clear input box
                if (logInput) {
                    logInput.value = '';
                    logInput.dataset.autoSuggestion = 'false';
                }
                // Send Ctrl+U to clear ctx.terminal command line
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
        // Don't interfere with button taps, nav controls, or scrollable areas
        if (e.target.closest('button')) return;
        if (e.target.closest('.tab-indicator')) return;
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

    // Scroll ctx.terminal into view when keyboard opens (only if already at bottom)
    // Skip on physical keyboard devices where no soft keyboard resize occurs
    if (!isPhysicalKb && window.visualViewport) {
        window.visualViewport.addEventListener('resize', () => {
            // Only auto-scroll if user was already at bottom (don't interrupt reading)
            const viewport = ctx.terminal.element?.querySelector('.xterm-viewport');
            if (viewport) {
                const nearBottom = (viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight) < 50;
                if (nearBottom) {
                    ctx.terminal.scrollToBottom();
                }
            }
        });
    }

    // Reconnect immediately when returning to app (visibility change)
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') {
            // Close dropdown when backgrounded so returning taps can't
            // hit destructive buttons (e.g. kill-pane ×) via passthrough
            repoDropdown.classList.add('hidden');
            // Stop all health-check timers — browser clamps them when hidden
            // and they fire with stale timestamps, causing false reconnects
            stopHeartbeat();
            if (watchdogTimer) { clearInterval(watchdogTimer); watchdogTimer = null; }
        }
        if (document.visibilityState === 'visible') {
            // Reset timestamps so heartbeat/idle checks don't fire with stale values
            // from when the tab was backgrounded
            lastDataReceived = Date.now();
            lastPongTime = Date.now();

            // Start Claude health polling when visible
            startAgentHealthPolling();

            // Render cached UI immediately (before reconnect completes)
            // This gives instant feedback while connection is being restored
            renderQueueList();  // Show cached queue items
            // Note: log content is already in DOM, no need to re-render

            // If obviously disconnected, reconnect immediately
            // (but not if already connecting — avoids double-connect on page load)
            if (!ctx.socket || (ctx.socket.readyState !== WebSocket.OPEN && ctx.socket.readyState !== WebSocket.CONNECTING)) {
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
                if (_preferSSE) connectSSE(); else connect();
            } else {
                // Socket reports OPEN — don't kill it just because we were backgrounded.
                // Restart timers with fresh baselines (they were stopped on hide).
                console.log('Page visible, socket OPEN — restarting health timers');
                startHeartbeat();
                startIdleCheck();
                startConnectionWatchdog();

                // If any inbound data arrived while hidden, socket is definitely alive
                // If not, normal heartbeat will probe within 15s

                // Only send a non-destructive keepalive — no kill timer
                try {
                    ctx.socket.send(JSON.stringify({ type: 'ping' }));
                } catch (e) {
                    // Send threw — socket is actually dead, reconnect
                    console.log('Keepalive send failed, socket dead — reconnecting');
                    ctx.socket.close();
                }
            }
        } else {
            // Stop Claude health polling when hidden to save resources
            stopAgentHealthPolling();
        }
    });

    // Handle network state changes (mobile networks are flaky)
    function reconnectAfterNetworkChange(reason) {
        if (!ctx.socket || (ctx.socket.readyState !== WebSocket.OPEN && ctx.socket.readyState !== WebSocket.CONNECTING)) {
            console.log(`${reason} — reconnecting immediately`);

            // Clear any pending timers
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
            if (reconnectOverlayTimer) {
                clearTimeout(reconnectOverlayTimer);
                reconnectOverlayTimer = null;
            }

            // connectionBanner handles the visual feedback for network reconnects
            // Reset attempts — failures were due to network, not server
            reconnectAttempts = 0;
            reconnectDelay = INITIAL_RECONNECT_DELAY;

            // Always use SSE after network change — WS upgrades often fail
            // through Tailscale after network switches, and SSE reconnects faster
            _preferSSE = true;
            connectSSE();
        }
    }

    window.addEventListener('online', () => {
        reconnectAfterNetworkChange('Network online');
    });

    window.addEventListener('offline', () => {
        console.log('Network offline');
        updateConnectionIndicator('disconnected');
        stopHeartbeat();
    });

    // Detect network TYPE changes (WiFi↔cellular) which don't fire online/offline
    if (navigator.connection) {
        navigator.connection.addEventListener('change', () => {
            console.log(`Network type changed: ${navigator.connection.type || navigator.connection.effectiveType}`);
            reconnectAfterNetworkChange('Network type changed');
        });
    }
}

// Enable paste from clipboard
function setupClipboard() {
    document.addEventListener('paste', (e) => {
        // If an input or textarea has focus, let the browser handle paste natively
        const tag = document.activeElement?.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA') return;

        const text = e.clipboardData.getData('text');
        if (text) {
            e.preventDefault();
            sendInput(text);
            ctx.terminal.focus();
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
    ctx.terminal.onScroll((scrollPos) => {
        const maxScroll = ctx.terminal.buffer.active.length - ctx.terminal.rows;
        isAtBottom = scrollPos >= maxScroll - 1;
    });

    // Auto-scroll on new output
    // Use requestAnimationFrame to debounce rapid writes during resize
    const originalWrite = ctx.terminal.write.bind(ctx.terminal);
    let scrollPending = false;

    ctx.terminal.write = (data) => {
        const shouldScroll = isAtBottom || forceScrollToBottom;

        originalWrite(data, () => {
            if (shouldScroll && !scrollPending) {
                scrollPending = true;
                requestAnimationFrame(() => {
                    ctx.terminal.scrollToBottom();
                    scrollPending = false;
                });
            }
        });
    };
}

/**
 * Setup compose mode (predictive text + file upload)
 */
function setupComposeMode() {
    // Pane selector
    const composePaneSelect = document.getElementById('composePaneSelect');

    function populateComposePaneSelect() {
        if (!composePaneSelect) return;
        composePaneSelect.innerHTML = '';
        const targets = ctx.targets || [];
        for (const t of targets) {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.project || t.window_name || t.id;
            if (t.id === ctx.activeTarget) opt.selected = true;
            composePaneSelect.appendChild(opt);
        }
    }

    // Open compose modal
    composeBtn.addEventListener('click', () => {
        populateComposePaneSelect();
        composeModal.classList.remove('hidden');
        composeInput.value = composeDraft;
        // Restore saved attachments
        pendingAttachments = draftAttachments.slice();
        renderAttachments();
        setTimeout(() => {
            composeInput.focus();
            composeInput.setSelectionRange(composeInput.value.length, composeInput.value.length);
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
        composeDraft = '';
        draftAttachments = [];
        sessionStorage.removeItem('composeDraft');
        sessionStorage.removeItem('composeDraftAttachments');
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

    // Get selected pane from compose selector (falls back to active target)
    function getComposeTargetPane() {
        return (composePaneSelect && composePaneSelect.value) || ctx.activeTarget;
    }

    // Append any pendingAttachment paths to the composed text. Paths are
    // never injected into the textarea — they live in the preview card —
    // so this is the single point that joins user text + attachment paths
    // for sending.
    //
    // Trims trailing whitespace from the user text before joining so we
    // never produce "text  /path" (double space) when the user happens
    // to leave a trailing space, and so trailing newlines don't end up
    // between the text and the path.
    function withAttachmentPaths(text) {
        if (pendingAttachments.length === 0) return text;
        const paths = pendingAttachments
            .map(a => a.path)
            .filter(p => p && !text.includes(p));
        if (paths.length === 0) return text;
        const joined = paths.join(' ');
        const trimmed = (text || '').replace(/\s+$/, '');
        return trimmed ? `${trimmed} ${joined}` : joined;
    }

    // Wait for any in-flight uploads to finish before sending. Without
    // this, a too-fast Send tap reads pendingAttachments while it's
    // still empty (upload POST hasn't returned), and the file is lost.
    // Brief toast is shown so the user knows why there's a small delay.
    async function awaitInflightUploads() {
        if (inflightUploads.length === 0) return;
        const n = inflightUploads.length;
        showToast(`Waiting for ${n} upload${n > 1 ? 's' : ''}…`, 'info', 1500);
        // Snapshot — new uploads triggered during the wait will not block
        // the current send. Use Promise.allSettled so a failed upload
        // doesn't prevent sending whatever did succeed.
        await Promise.allSettled(inflightUploads.slice());
    }

    // If compose was opened to edit an existing queue item (composeEditingItemId
    // set by prefillCompose), dequeue the original BEFORE the new send/enqueue
    // lands. Awaited so the server sees `remove → enqueue` in order — without
    // the await, parallel POSTs let reconcileQueue (on a WS reconnect during
    // the gap) refetch the original from the server and produce a duplicate.
    // The flag is consumed (cleared) so a subsequent action on the same modal
    // open doesn't try to re-remove a now-gone item.
    async function consumeEditingItemRemoval() {
        if (!composeEditingItemId) return;
        const id = composeEditingItemId;
        composeEditingItemId = null;
        try {
            const params = new URLSearchParams({
                session: ctx.currentSession,
                item_id: id,
                token: ctx.token,
            });
            if (ctx.activeTarget) params.set('pane_id', ctx.activeTarget);
            await apiFetch(`/api/queue/remove?${params}`, { method: 'POST' });
        } catch (e) {
            console.warn('Failed to dequeue edited item before resend:', e);
            // Continue anyway — worst case is the duplicate this tries to
            // prevent, which is the original behavior.
        }
    }

    // Send to terminal (text + attachment paths)
    // If a different pane is selected, use the text API with pane_id
    async function sendComposedText(withEnter = false) {
        await awaitInflightUploads();
        let text = withAttachmentPaths(composeInput.value);
        if (!text) return;

        // If editing an existing queue item, remove it first (awaited)
        // so the send doesn't leave the original behind.
        await consumeEditingItemRemoval();

        const targetPane = getComposeTargetPane();
        if (targetPane && targetPane !== ctx.activeTarget) {
            // Send to a different pane via API
            apiFetch('/api/terminal/text', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, enter: withEnter, pane_id: targetPane, token: ctx.token }),
            }).then(() => {
                closeComposeModal(true);
            }).catch(() => {
                ctx.showToast('Failed to send to pane', 'warning');
            });
        } else if (ctx.socket && ctx.socket.readyState === WebSocket.OPEN) {
            sendTextAtomic(text, withEnter);
            closeComposeModal(true);
        }
    }

    async function queueComposedText() {
        await awaitInflightUploads();
        let text = withAttachmentPaths(composeInput.value);
        if (!text) return;

        // If editing an existing queue item, remove it BEFORE the
        // enqueue so the server sees a clean replace, not a duplicate.
        await consumeEditingItemRemoval();

        const ok = await enqueueCommand(text);
        if (ok) closeComposeModal(true);
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

    // Backlog button - add to backlog (split on lines if multi-line)
    const composeBacklog = document.getElementById('composeBacklog');
    if (composeBacklog) {
        composeBacklog.addEventListener('click', () => {
            const text = composeInput.value.trim();
            if (!text) return;
            const lines = splitBacklogLines(text);
            if (lines.length <= 1) {
                // Single item
                const summary = text.split('\n')[0].slice(0, 120);
                addBacklogItem(summary, text, 'human');
                ctx.showToast('Added to backlog', 'success');
            } else {
                for (const line of lines) {
                    addBacklogItem(line.slice(0, 120), line, 'human');
                }
                ctx.showToast(`Added ${lines.length} items to backlog`, 'success');
            }
            closeComposeModal(true);
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

    // ── @-mention file autocomplete ──
    let mentionSearchTimer = null;
    let mentionActive = false;
    let mentionStartPos = -1;
    let mentionSelectedIndex = 0;
    const mentionDropdown = document.getElementById('composeMentionDropdown');

    composeInput.addEventListener('input', () => {
        composeDraft = composeInput.value;
        sessionStorage.setItem('composeDraft', composeDraft);
        detectMention();
    });

    composeInput.addEventListener('keydown', (e) => {
        if (!mentionActive || !mentionDropdown) return;
        const items = mentionDropdown.querySelectorAll('.mention-item');
        if (items.length === 0) return;

        if (e.key === 'ArrowDown') {
            e.preventDefault();
            mentionSelectedIndex = Math.min(mentionSelectedIndex + 1, items.length - 1);
            updateMentionSelection(items);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            mentionSelectedIndex = Math.max(mentionSelectedIndex - 1, 0);
            updateMentionSelection(items);
        } else if (e.key === 'Enter' || e.key === 'Tab') {
            if (items.length > 0) {
                e.preventDefault();
                e.stopPropagation();
                selectMention(items[mentionSelectedIndex]?.dataset.path);
            }
        } else if (e.key === 'Escape') {
            closeMentionDropdown();
        }
    });

    function detectMention() {
        const val = composeInput.value;
        const cursor = composeInput.selectionStart;

        let atPos = -1;
        for (let i = cursor - 1; i >= 0; i--) {
            if (val[i] === '@' && (i === 0 || /\s/.test(val[i - 1]))) {
                atPos = i;
                break;
            }
            if (/\s/.test(val[i])) break;
        }

        if (atPos < 0) { closeMentionDropdown(); return; }
        const query = val.slice(atPos + 1, cursor);
        if (query.length < 1) { closeMentionDropdown(); return; }

        mentionStartPos = atPos;
        mentionActive = true;
        mentionSelectedIndex = 0;

        clearTimeout(mentionSearchTimer);
        mentionSearchTimer = setTimeout(() => fetchMentionResults(query), 150);
    }

    async function fetchMentionResults(query) {
        try {
            const resp = await apiFetch(`/api/files/search?q=${encodeURIComponent(query)}&limit=10`);
            if (!resp.ok) return;
            const data = await resp.json();
            renderMentionDropdown(data.files || []);
        } catch (e) {
            // Silently skip
        }
    }

    function renderMentionDropdown(files) {
        if (!mentionDropdown || files.length === 0) { closeMentionDropdown(); return; }
        mentionDropdown.innerHTML = '';
        files.forEach((filePath, i) => {
            const item = document.createElement('div');
            item.className = 'mention-item' + (i === 0 ? ' selected' : '');
            item.dataset.path = filePath;
            item.textContent = filePath;
            item.addEventListener('click', () => selectMention(filePath));
            mentionDropdown.appendChild(item);
        });
        mentionDropdown.classList.remove('hidden');
    }

    function updateMentionSelection(items) {
        items.forEach((item, i) => item.classList.toggle('selected', i === mentionSelectedIndex));
        items[mentionSelectedIndex]?.scrollIntoView({ block: 'nearest' });
    }

    function selectMention(filePath) {
        if (!filePath) return;
        const val = composeInput.value;
        const cursor = composeInput.selectionStart;
        const before = val.slice(0, mentionStartPos);
        const after = val.slice(cursor);
        const newVal = before + filePath + ' ' + after;
        composeInput.value = newVal;
        const newPos = mentionStartPos + filePath.length + 1;
        composeInput.setSelectionRange(newPos, newPos);
        composeInput.focus();
        composeDraft = composeInput.value;
        sessionStorage.setItem('composeDraft', composeDraft);
        closeMentionDropdown();
    }

    function closeMentionDropdown() {
        mentionActive = false;
        mentionStartPos = -1;
        if (mentionDropdown) mentionDropdown.classList.add('hidden');
    }
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
    const challengePlanSelect = document.getElementById('challengePlanSelect');
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
    let plansLoaded = false;
    let plansCache = [];
    let modelMetadataCache = {};

    // Fetch available models
    async function loadModels() {
        if (modelsLoaded) return;

        try {
            const response = await apiFetch(`/api/challenge/models`);
            if (!response.ok) {
                throw new Error('Failed to load models');
            }
            const data = await response.json();

            challengeModelSelect.innerHTML = '';
            modelMetadataCache = {};
            if (data.models && data.models.length > 0) {
                data.models.forEach(model => {
                    modelMetadataCache[model.key] = model;
                    const option = document.createElement('option');
                    option.value = model.key;
                    const suffix = model.local ? ' (local)' : '';
                    option.textContent = model.name + suffix;
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

    // Fetch available plans for dropdown
    async function loadPlans() {
        if (!challengePlanSelect) return;

        try {
            const response = await apiFetch(`/api/plans`);
            if (!response.ok) throw new Error('Failed to load plans');
            const data = await response.json();

            plansCache = data.plans || [];
            challengePlanSelect.innerHTML = '<option value="">None</option>';

            if (plansCache.length > 0) {
                plansCache.forEach(plan => {
                    const option = document.createElement('option');
                    option.value = plan.filename;
                    // Truncate title if too long
                    const title = plan.title.length > 40 ? plan.title.slice(0, 40) + '...' : plan.title;
                    option.textContent = title;
                    challengePlanSelect.appendChild(option);
                });
            }
            plansLoaded = true;
        } catch (error) {
            console.error('Failed to load plans:', error);
            challengePlanSelect.innerHTML = '<option value="">Error loading plans</option>';
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
                const response = await apiFetch(`/api/terminal/capture?lines=50`);
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

        // Selected plan
        const selectedPlan = challengePlanSelect?.value;
        if (selectedPlan) {
            try {
                const response = await apiFetch(`/api/plan?filename=${encodeURIComponent(selectedPlan)}`);
                const data = await response.json();
                if (data.content) {
                    const planTitle = plansCache.find(p => p.filename === selectedPlan)?.title || selectedPlan;
                    preview += `## Plan: ${planTitle}\n${data.content}\n\n`;
                } else {
                    preview += `## Plan\n(Failed to load)\n\n`;
                }
            } catch (e) {
                preview += `## Plan\n(Failed to load)\n\n`;
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
        loadPlans();
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
    if (challengePlanSelect) {
        challengePlanSelect.addEventListener('change', loadPreview);
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
        const selectedPlanFile = challengePlanSelect?.value || '';

        const modelName = challengeModelSelect.options[challengeModelSelect.selectedIndex]?.text || selectedModel;

        challengeRun.disabled = true;
        challengeResult.classList.remove('hidden');
        challengeResult.classList.add('loading');
        challengeStatus.textContent = '';

        // Elapsed timer — critical for CLI reviews (60-120s)
        const startTime = Date.now();
        challengeRun.textContent = '0s';
        challengeResultContent.innerHTML = `<div class="loading">Analyzing with ${escapeHtml(modelName)}...</div>`;
        const timerInterval = setInterval(() => {
            const elapsed = Math.round((Date.now() - startTime) / 1000);
            challengeRun.textContent = `${elapsed}s`;
        }, 1000);

        try {
            const params = new URLSearchParams({
                token: ctx.token,
                model: selectedModel,
                problem: problem,
                include_terminal: includeTerminal,
                terminal_lines: 50,
                include_diff: includeDiff,
            });
            if (selectedPlanFile) {
                params.set('plan_filename', selectedPlanFile);
            }

            const response = await apiFetch(`/api/challenge?${params}`, {
                method: 'POST',
            });

            const data = await response.json();

            if (response.status === 409) {
                throw new Error(data.error || 'Another review is in progress');
            }
            if (!response.ok) {
                throw new Error(data.error || 'Challenge failed');
            }

            // Store raw response for copy/export
            lastResponseText = data.content || 'No response received';

            // Format the result with markdown-like headers
            // Escape HTML first to prevent XSS from model output
            let content = escapeHtml(lastResponseText)
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
            challengeResultContent.innerHTML = `<p style="color: var(--danger);">Error: ${escapeHtml(error.message)}</p>`;
            challengeResult.classList.remove('loading');
            challengeStatus.textContent = '';
        } finally {
            clearInterval(timerInterval);
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
 * Get a short label for document file icons based on extension.
 */
function getDocIcon(filename) {
    const ext = (filename || '').split('.').pop().toLowerCase();
    const labels = { pdf: 'PDF', doc: 'DOC', docx: 'DOC', xls: 'XLS', xlsx: 'XLS', csv: 'CSV', txt: 'TXT', md: 'MD' };
    return labels[ext] || 'FILE';
}

/**
 * Upload a file attachment
 * @param {File} file - The file to upload
 * @param {HTMLElement} [triggerBtn] - Optional button to show uploading state on
 */
async function uploadAttachment(file, triggerBtn) {
    // Register this upload as in-flight so a too-fast Send / Run / Queue
    // tap can await it before reading pendingAttachments. Each upload is
    // tracked as the promise itself, removed from the list on settle.
    const tracker = _uploadAttachmentInner(file, triggerBtn);
    inflightUploads.push(tracker);
    try {
        return await tracker;
    } finally {
        const i = inflightUploads.indexOf(tracker);
        if (i >= 0) inflightUploads.splice(i, 1);
    }
}

async function _uploadAttachmentInner(file, triggerBtn) {
    // Show uploading state on the trigger button if provided
    const originalContent = triggerBtn?.textContent;
    if (triggerBtn) {
        triggerBtn.classList.add('uploading');
    }

    // Guard against completely empty files — mobile browsers sometimes
    // hand back a 0-byte File object when a paste fails to marshal the
    // image data. Surface this instead of POSTing an empty upload.
    if (!file || file.size === 0) {
        showToast('Paste produced an empty file — try again', 'error');
        if (triggerBtn) {
            triggerBtn.classList.remove('uploading');
            triggerBtn.textContent = originalContent;
        }
        return;
    }

    try {
        const formData = new FormData();
        formData.append('file', file);

        // Use apiFetch so auth header (X-MTO-Token + X-Client-ID) is sent
        // alongside the query-string token — fixes token-only configs and
        // anything behind base_path.
        const response = await apiFetch('/api/upload', {
            method: 'POST',
            body: formData,
        });

        if (!response.ok) {
            let errMsg = `HTTP ${response.status}`;
            try {
                const err = await response.json();
                if (err && err.error) errMsg = err.error;
            } catch (_) { /* body was not JSON */ }
            throw new Error(errMsg);
        }

        const data = await response.json();
        if (!data || !data.path) {
            throw new Error('Server returned no path');
        }

        // Add to pending attachments
        pendingAttachments.push({
            path: data.path,
            filename: data.filename,
            size: data.size,
            type: file.type,
            localUrl: URL.createObjectURL(file),
        });

        // The path is NOT inserted into the textarea — it lives only in
        // the attachment preview card below. This keeps the compose text
        // clean (no accidental edits, no stray newlines around the path).
        // sendComposedText / queueComposedText will append the attachment
        // paths from pendingAttachments at send time via withAttachmentPaths.

        renderAttachments();
        showToast(`Attached ${data.filename}`, 'success');

    } catch (error) {
        console.error('[uploadAttachment] failed:', error);
        // Distinguish network error from server error so the user knows
        // whether to retry immediately or check server logs.
        const msg = (error && error.name === 'TypeError')
            ? `Network error: ${error.message}`
            : `Upload failed: ${error.message}`;
        showToast(msg, 'error');
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
    composeAttachments.innerHTML = pendingAttachments.map((att, idx) => {
        const isImage = att.type?.startsWith('image/');
        const thumbHtml = isImage
            ? `<img src="${att.localUrl}" alt="" class="attachment-thumb">`
            : `<div class="attachment-icon">${getDocIcon(att.filename)}</div>`;
        return `
        <div class="attachment-item">
            ${thumbHtml}
            <div class="attachment-info">
                <span class="attachment-path">${escapeHtml(att.path)}</span>
                <span class="attachment-size">${formatFileSize(att.size)}</span>
            </div>
            <button class="attachment-remove" data-idx="${idx}">&times;</button>
        </div>`;
    }).join('');

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
// formatFileSize — moved to src/utils.js

function closeComposeModal(clearDraft = false) {
    // Save or clear draft
    if (clearDraft) {
        composeDraft = '';
        draftAttachments = [];
        sessionStorage.removeItem('composeDraft');
        sessionStorage.removeItem('composeDraftAttachments');
    } else {
        composeDraft = composeInput.value;
        draftAttachments = pendingAttachments.slice();
        sessionStorage.setItem('composeDraft', composeDraft);
        sessionStorage.setItem('composeDraftAttachments', JSON.stringify(draftAttachments));
    }
    // Always clear the editing-item marker on close. Cancel path: the
    // original stays in the queue (we never removed it). Send/Queue
    // paths already consumed the marker via consumeEditingItemRemoval
    // before reaching here, so this is a no-op for them. Without this
    // clear, a subsequent NON-edit open of compose would inherit the
    // stale id and try to remove an unrelated item.
    composeEditingItemId = null;
    composeModal.classList.add('hidden');
    composeInput.blur();
    clearAttachments();
}

/**
 * Setup select mode and copy buttons for ctx.terminal
 * Select mode: tap start point, tap end point to select text
 */
let isSelectMode = false;
let selectStart = null;  // {row, col}

/**
 * Setup system metrics pill (CPU/RAM/Disk)
 */
function setupMetricsWidget() {
    const pill = document.getElementById('metricsPill');
    if (!pill) return;

    let metricsTimer = null;

    async function updateMetrics() {
        try {
            const resp = await apiFetch(`/api/metrics`);
            if (!resp.ok) return;
            const data = await resp.json();

            const parts = [];
            if (data.cpu_pct != null) parts.push(`C${Math.round(data.cpu_pct)}%`);
            if (data.mem_pct != null) parts.push(`M${Math.round(data.mem_pct)}%`);
            if (data.disk_pct != null) parts.push(`D${Math.round(data.disk_pct)}%`);
            if (parts.length === 0) { pill.classList.add('hidden'); return; }

            pill.textContent = parts.join(' ');
            pill.classList.remove('hidden', 'metrics-ok', 'metrics-warn', 'metrics-critical');

            const maxPct = Math.max(data.cpu_pct || 0, data.mem_pct || 0, data.disk_pct || 0);
            if (maxPct > 90) pill.classList.add('metrics-critical');
            else if (maxPct > 70) pill.classList.add('metrics-warn');
            else pill.classList.add('metrics-ok');
        } catch (e) {
            // Silently skip on error
        }
    }

    updateMetrics();
    metricsTimer = setInterval(() => {
        if (!document.hidden) updateMetrics();
    }, 15000);
}

/**
 * Setup terminal search bar (xterm SearchAddon)
 */
function setupTerminalSearch() {
    const bar = document.getElementById('termSearchBar');
    const input = document.getElementById('termSearchInput');
    const countEl = document.getElementById('termSearchCount');
    const prevBtn = document.getElementById('termSearchPrev');
    const nextBtn = document.getElementById('termSearchNext');
    const closeBtn = document.getElementById('termSearchClose');
    const openBtn = document.getElementById('termSearchBtn');
    if (!bar || !input || !searchAddon) return;

    function openSearch() {
        if (ctx.outputMode !== 'full') return;
        bar.classList.remove('hidden');
        input.focus();
        input.select();
    }

    function closeSearch() {
        bar.classList.add('hidden');
        input.value = '';
        countEl.textContent = '';
        searchAddon.clearDecorations();
    }

    function doSearch(direction) {
        const query = input.value;
        if (!query) { countEl.textContent = ''; searchAddon.clearDecorations(); return; }
        if (direction === 'prev') {
            searchAddon.findPrevious(query);
        } else {
            searchAddon.findNext(query);
        }
    }

    input.addEventListener('input', () => doSearch('next'));
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            doSearch(e.shiftKey ? 'prev' : 'next');
        }
        if (e.key === 'Escape') {
            e.preventDefault();
            closeSearch();
        }
    });

    nextBtn.addEventListener('click', () => doSearch('next'));
    prevBtn.addEventListener('click', () => doSearch('prev'));
    closeBtn.addEventListener('click', closeSearch);
    if (openBtn) openBtn.addEventListener('click', openSearch);

    // Ctrl+F / Cmd+F to open search (when in terminal view)
    document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'f' && ctx.currentView === 'terminal') {
            e.preventDefault();
            openSearch();
        }
    });
}

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
        setTimeout(() => ctx.terminal.focus(), 100);
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
        const selection = ctx.terminal.getSelection();
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
                ctx.terminal.clearSelection();
            });
        } else {
            const success = fallbackCopy(selection);
            selectCopyBtn.textContent = success ? 'Copied!' : 'Failed';
            ctx.terminal.clearSelection();
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
                ctx.terminal.clearSelection();
            } else if (buttonState === 'tap-start' || buttonState === 'tap-end') {
                // Cancel selection
                resetState();
            } else if (buttonState === 'copy') {
                // Copy selection
                handleCopy();
            }
        });
    }

    // Handle taps on ctx.terminal for selection
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

            // Get ctx.terminal cell dimensions
            const cellWidth = ctx.terminal._core._renderService.dimensions.css.cell.width;
            const cellHeight = ctx.terminal._core._renderService.dimensions.css.cell.height;

            // Get position relative to ctx.terminal viewport
            const screen = terminalContainer.querySelector('.xterm-screen');
            if (!screen) return;
            const rect = screen.getBoundingClientRect();
            const x = clientX - rect.left;
            const y = clientY - rect.top;

            // Convert to row/col
            const col = Math.floor(x / cellWidth);
            const row = Math.floor(y / cellHeight) + ctx.terminal.buffer.active.viewportY;

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
                    ctx.terminal.select(startCol, startRow, length);
                } else {
                    ctx.terminal.selectLines(startRow, endRow);
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

    ctx.terminal.onKey(({ key, domEvent }) => {
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

// ===== UIState Mapping Layer =====
// deriveUIState, deriveSystemSummary — moved to src/ui-state.js

/**
 * View toggle: Log | Terminal | Context | Touch
 */
// ctx.currentView initialized by context.js ('log')

// ===== Desktop multi-pane state =====
// ctx.uiMode initialized by context.js ('mobile-single')
const DESKTOP_BREAKPOINT = 1024;
let desktopFocusedPane = 'log'; // 'team' | 'log' | 'terminal'
let desktopResizeTimer = null;
let activeToolPanel = null;        // Currently open tool panel name
let toolsPanelOrigParents = new Map(); // Track reparented drawer tab content
let sidebarOrigParents = new Map();    // Track reparented sidebar content
let sidebarShowingTool = null;         // Currently open tool in sidebar

function shouldLogRefreshRun() {
    return document.visibilityState === 'visible' &&
           (ctx.uiMode === 'desktop-multipane' || ctx.currentView === 'log');
}
// Auto-refresh for log view - singleflight async loop
let logRefreshController = null;  // AbortController for singleflight loop
const LOG_REFRESH_INTERVAL = 5000;  // Wait 5s between requests

// Active Prompt functions are defined earlier - these are aliases for compatibility
function startTailViewport() { startActivePrompt(); }
function stopTailViewport() { stopActivePrompt(); }
function updateTailViewport() { refreshActivePrompt(); }

function setupViewToggle() {
    // Views are now: log (primary), ctx.terminal
    // Context and touch moved to Docs modal
    // Tab buttons removed - using swipe and dots now

    // Log input handling
    setupLogInput();

    // Interactive mode toggle
    const interactiveToggle = document.getElementById('interactiveToggle');
    if (interactiveToggle) {
        interactiveToggle.addEventListener('click', toggleInteractiveMode);
    }

    // Terminal agent selector
    const agentSelect = document.getElementById('terminalAgentSelect');
    if (agentSelect) {
        agentSelect.addEventListener('change', () => {
            const targetId = agentSelect.value;
            if (targetId) {
                selectTarget(targetId);
            }
        });
    }
}

// updateTerminalAgentSelector — moved to src/features/team.js

/**
 * Check if the active target is in the same repo as the team members.
 * Returns true if team exists AND shares a CWD prefix with active pane.
 * Returns false if no team, or team is in a different repo.
 */
function isTeamInCurrentRepo() {
    if (!ctx.teamState || !ctx.teamState.has_team || !ctx.teamState.team) return false;
    const activeTarget = ctx.targets?.find(t => t.id === ctx.activeTarget);
    const activeCwd = activeTarget?.cwd;
    if (!activeCwd) return true; // No CWD info — assume same repo
    const allMembers = [ctx.teamState.team.leader, ...(ctx.teamState.team.agents || [])].filter(Boolean);
    const teamCwds = allMembers.map(a => a.cwd).filter(Boolean);
    if (teamCwds.length === 0) return true; // No CWD info on team — assume same repo
    return teamCwds.some(tc => activeCwd.startsWith(tc) || tc.startsWith(activeCwd));
}

// Dynamic tab order based on team presence in current repo
function getTabOrder() {
    if (isTeamInCurrentRepo()) return ['team', 'log', 'terminal'];
    return ['log', 'terminal'];
}

/**
 * Update the view switcher to reflect current view and available tabs.
 * Shows/hides switcher based on team presence, updates active state.
 */
function updateViewSwitcher() {
    const switcher = document.getElementById('viewSwitcher');
    if (!switcher) return;

    const order = getTabOrder();

    // Always show dots when there are 2+ navigable views
    if (order.length >= 2) {
        switcher.classList.remove('hidden');
    } else {
        switcher.classList.add('hidden');
        return;
    }

    // Update active state on all dots, hide those not in current order
    switcher.querySelectorAll('.view-dot').forEach(dot => {
        const view = dot.dataset.view;
        dot.classList.toggle('active', view === ctx.currentView);
        dot.style.display = order.includes(view) ? '' : 'none';
    });
}

// Backward compat — old callers reference updateTabIndicator
function updateTabIndicator() {
    updateViewSwitcher();
}

/**
 * Setup view switcher click handlers
 */
function setupViewSwitcher() {
    const switcher = document.getElementById('viewSwitcher');
    if (!switcher) return;

    let switchHandled = false;
    switcher.addEventListener('click', (e) => {
        const dot = e.target.closest('.view-dot');
        if (!dot || switchHandled) return;
        switchHandled = true;
        const view = dot.dataset.view;
        if (view && view !== ctx.currentView) {
            switchToView(view);
        }
        setTimeout(() => { switchHandled = false; }, 300);
    });
}

/**
 * Switch to next tab (swipe left)
 */
function switchToNextTab() {
    const order = getTabOrder();
    const currentIndex = order.indexOf(ctx.currentView);
    if (currentIndex < order.length - 1) {
        const nextView = order[currentIndex + 1];
        switchToView(nextView);
    }
}

/**
 * Switch to previous tab (swipe right)
 */
function switchToPrevTab() {
    const order = getTabOrder();
    const currentIndex = order.indexOf(ctx.currentView);
    if (currentIndex > 0) {
        const prevView = order[currentIndex - 1];
        switchToView(prevView);
    }
}

/**
 * Switch to a specific view by name
 */
function switchToView(viewName) {
    if (ctx.uiMode === 'desktop-multipane') {
        if (viewName === 'terminal') {
            openDesktopTerminal();
        } else {
            switchDesktopFocus(viewName);
        }
        return;
    }
    switch (viewName) {
        case 'log':
            switchToLogView();
            break;
        case 'team':
            switchToTeamView();
            break;
        case 'terminal':
            switchToTerminalView();
            break;
    }
}

/**
 * Setup swipe gesture detection for tab navigation
 */
/**
 * Setup draggable resize handle between log and tail areas
 */
function setupTailResize() {
    const handle = document.getElementById('tailResizeHandle');
    const output = document.getElementById('activePromptContent');
    if (!handle || !output) return;

    const MIN_HEIGHT = 60;
    const MAX_HEIGHT = Math.round(window.innerHeight * 0.5);
    const STORAGE_KEY = 'mto_tail_height';

    // Restore saved height
    const saved = parseInt(localStorage.getItem(STORAGE_KEY));
    if (saved && saved >= MIN_HEIGHT && saved <= MAX_HEIGHT) {
        output.style.height = saved + 'px';
    }

    let dragging = false;
    let startY = 0;
    let startHeight = 0;

    function onStart(e) {
        dragging = true;
        startY = e.touches ? e.touches[0].clientY : e.clientY;
        startHeight = output.offsetHeight;
        handle.classList.add('active');
        e.preventDefault();
    }

    function onMove(e) {
        if (!dragging) return;
        const y = e.touches ? e.touches[0].clientY : e.clientY;
        // Dragging up = larger tail, dragging down = smaller tail
        const delta = startY - y;
        const newHeight = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, startHeight + delta));
        output.style.height = newHeight + 'px';
    }

    function onEnd() {
        if (!dragging) return;
        dragging = false;
        handle.classList.remove('active');
        const h = output.offsetHeight;
        localStorage.setItem(STORAGE_KEY, h.toString());
    }

    handle.addEventListener('touchstart', onStart, { passive: false });
    handle.addEventListener('mousedown', onStart);
    document.addEventListener('touchmove', onMove, { passive: false });
    document.addEventListener('mousemove', onMove);
    document.addEventListener('touchend', onEnd);
    document.addEventListener('mouseup', onEnd);
}

function setupSwipeNavigation() {
    const containers = [
        document.getElementById('logView'),
        document.getElementById('teamView'),
        document.getElementById('terminalView'),
    ];

    const SWIPE_THRESHOLD = 80;    // Minimum px to trigger
    const SWIPE_TIMEOUT = 300;     // Max ms for swipe
    const DIRECTION_RATIO = 1.5;   // deltaX must be > deltaY * ratio

    let touchStartX = 0;
    let touchStartY = 0;
    let touchStartTime = 0;

    const handleTouchStart = (e) => {
        if (ctx.uiMode === 'desktop-multipane') return;
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

    // Dot event handlers are now created dynamically in updateTabIndicator()
}

function hideAllContainers() {
    if (ctx.uiMode === 'desktop-multipane') return;
    if (logView) logView.classList.add('hidden');
    const teamViewEl = document.getElementById('teamView');
    if (teamViewEl) teamViewEl.classList.add('hidden');
    const dispatchBar = document.getElementById('teamDispatchBar');
    if (dispatchBar) dispatchBar.classList.add('hidden');
    if (terminalView) terminalView.classList.add('hidden');
    closeFabMenu();
    // Stop auto-refresh when leaving log view
    stopLogAutoRefresh();
    stopTailViewport();
    stopTeamCardRefresh();
}

function switchToLogView() {
    ctx.currentView = 'log';
    // Auto-disable interactive mode when leaving ctx.terminal view
    if (interactiveMode) {
        interactiveMode = false;
        clearInteractiveIdleTimer();
        updateInteractiveBadge();
    }
    hideAllContainers();
    if (logView) logView.classList.remove('hidden');
    // Show control bars (always — lock removed)
    controlBarsContainer.classList.remove('hidden');
    updateViewSwitcher();
    updateActionBar();
    // Restore active prompt pre (hidden in ctx.terminal view)
    if (activePromptContent) activePromptContent.style.display = '';
    // Switch to tail mode - no xterm rendering, lightweight updates
    setOutputMode('tail');
    // Reset scroll state - user should start at bottom when switching to log view
    userAtBottom = true;
    pendingLogContent = null;
    hideNewContentIndicator();
    // Reset and load log content fresh
    logLoaded = false;
    loadLogContent();
    // Start auto-refresh
    startLogAutoRefresh();
    // Start tail viewport refresh
    startTailViewport();
}

function switchToTerminalView() {
    if (ctx.uiMode === 'desktop-multipane') {
        openDesktopTerminal();
        return;
    }
    ctx.currentView = 'terminal';
    hideAllContainers();
    if (terminalView) terminalView.classList.remove('hidden');
    controlBarsContainer.classList.remove('hidden');
    updateViewSwitcher();
    updateActionBar();
    updateTerminalAgentSelector();
    // Hide active prompt pre — live xterm already shows ctx.terminal content
    if (activePromptContent) activePromptContent.style.display = 'none';

    // CRITICAL ORDER: fit + resize FIRST, then set_mode
    // The resize triggers tmux to redraw at the correct ctx.terminal size.
    // If we set_mode first, tmux redraws at the OLD size → garbled output.
    // Double-rAF ensures browser has completed layout reflow after
    // display:none → display:flex, so fitAddon gets correct dimensions.
    requestAnimationFrame(() => {
        requestAnimationFrame(() => {
            if (fitAddon) fitAddon.fit();
            sendResize();
            // Now switch to full mode - server starts forwarding PTY data
            // The resize we just sent will trigger a clean tmux redraw
            setOutputMode('full');
            // Auto-focus ctx.terminal to enable keyboard input (desktop only)
            // On touch devices, focusing opens the soft keyboard which is disruptive
            if (ctx.terminal && !('ontouchstart' in window)) {
                ctx.terminal.focus();
            }
        });
    });
}

function switchToTeamView() {
    ctx.currentView = 'team';
    // Auto-disable interactive mode
    if (interactiveMode) {
        interactiveMode = false;
        clearInteractiveIdleTimer();
        updateInteractiveBadge();
    }
    hideAllContainers();
    const teamViewEl = document.getElementById('teamView');
    if (teamViewEl) teamViewEl.classList.remove('hidden');
    // Show same bottom bars as log/terminal
    controlBarsContainer.classList.remove('hidden');
    updateViewSwitcher();
    updateActionBar();
    // Lightweight mode -- no xterm rendering
    setOutputMode('tail');
    // Load plans and auto-refresh team cards
    activateTeamView();
}

/**
 * Append standard action buttons (Git, Stop, Compose, •••) to a bar element.
 */
function appendStandardActionButtons(bar) {
    const gitBtn = document.createElement('button');
    gitBtn.className = 'action-bar-btn action-bar-git';
    gitBtn.textContent = 'Commit';
    gitBtn.addEventListener('click', () => onGitButtonClick(gitBtn));
    bar.appendChild(gitBtn);
    updateGitButton(gitBtn);

    const btn3 = document.createElement('button');
    btn3.className = 'action-bar-btn action-bar-stop';
    btn3.textContent = 'Stop';
    btn3.addEventListener('click', () => {
        sendStopInterrupt();
        showToast('Interrupt sent', 'success');
    });
    bar.appendChild(btn3);

    const btn4 = document.createElement('button');
    btn4.className = 'action-bar-btn action-bar-compose';
    btn4.textContent = 'Compose';
    btn4.addEventListener('click', () => { if (composeBtn) composeBtn.click(); });
    bar.appendChild(btn4);

    const btn1 = document.createElement('button');
    btn1.className = 'action-bar-btn';
    btn1.innerHTML = '&bull;&bull;&bull;';
    btn1.addEventListener('click', () => toggleFabMenu());
    bar.appendChild(btn1);
}

/**
 * Check git status and update button label: "Commit" if dirty, "Push" if clean+ahead.
 */
async function updateGitButton(btn) {
    if (!btn) return;
    try {
        const paneParam = ctx.activeTarget ? `?pane_id=${encodeURIComponent(ctx.activeTarget)}` : '';
        const resp = await apiFetch(`/api/rollback/git/status${paneParam}`);
        const data = await resp.json();
        if (!data.has_repo) return;

        if (data.is_dirty) {
            btn.textContent = 'Commit';
            btn.dataset.gitAction = 'commit';
        } else if (data.ahead > 0) {
            btn.textContent = `Push (${data.ahead})`;
            btn.dataset.gitAction = 'push';
        } else {
            btn.textContent = 'Commit';
            btn.dataset.gitAction = 'commit';
        }
    } catch {
        // Leave as-is on error
    }
}

/**
 * Handle git button click — commit or push based on current state.
 */
async function onGitButtonClick(btn) {
    if (btn.dataset.gitAction === 'push') {
        await doGitPush();
        updateGitButton(btn);
    } else {
        await promptGitCommit();
        updateGitButton(btn);
    }
}

/**
 * Prompt for a commit message, then POST /api/git/commit.
 */
async function promptGitCommit() {
    const message = prompt('Commit message:');
    if (!message || !message.trim()) return;

    try {
        const params = new URLSearchParams({ token: ctx.token });
        if (ctx.currentSession) params.set('session', ctx.currentSession);
        if (ctx.activeTarget) params.set('pane_id', ctx.activeTarget);

        const resp = await apiFetch(`/api/git/commit?${params}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: message.trim() }),
        });
        const data = await resp.json();
        if (data.success) {
            showToast(`Committed ${data.hash}`, 'success');
        } else {
            showToast(data.error || 'Commit failed', 'error');
        }
    } catch (err) {
        showToast('Commit failed: ' + err.message, 'error');
    }
}

/**
 * POST /api/git/push — push current branch.
 */
async function doGitPush() {
    try {
        showToast('Pushing...', 'success');
        const params = new URLSearchParams({ token: ctx.token });
        if (ctx.currentSession) params.set('session', ctx.currentSession);
        if (ctx.activeTarget) params.set('pane_id', ctx.activeTarget);

        const resp = await apiFetch(`/api/git/push?${params}`, {
            method: 'POST',
        });
        const data = await resp.json();
        if (data.success) {
            showToast(`Pushed ${data.branch}`, 'success');
        } else {
            showToast(data.error || 'Push failed', 'error');
        }
    } catch (err) {
        showToast('Push failed: ' + err.message, 'error');
    }
}

/**
 * Update the contextual action bar based on current view and system state.
 * Called on view switch and team state update.
 */
function updateActionBar() {
    const bar = document.getElementById('actionBar');
    if (!bar) return;

    bar.innerHTML = '';
    const hasTeam = ctx.teamState && ctx.teamState.has_team;

    if (ctx.currentView === 'team' && hasTeam) {
        const summary = getLastSystemSummary();
        if (summary && summary.attentionCount > 0) {
            // Show approval banner above standard buttons
            const banner = document.createElement('div');
            banner.className = 'approval-banner';

            const text = document.createElement('span');
            text.className = 'approval-banner-text';
            text.textContent = summary.attentionCount + ' approval' +
                (summary.attentionCount > 1 ? 's' : '') + ' pending';
            banner.appendChild(text);

            const btn = document.createElement('button');
            btn.className = 'approval-banner-btn';
            btn.textContent = 'Review Now';
            btn.addEventListener('click', scrollToFirstAttention);
            banner.appendChild(btn);

            bar.appendChild(banner);
        }
        // Same standard buttons as log/terminal
        appendStandardActionButtons(bar);
        bar.classList.remove('hidden');
        // Show dispatch bar inline
        const dispatchBar = document.getElementById('teamDispatchBar');
        if (dispatchBar) dispatchBar.classList.remove('hidden');
    } else if (ctx.currentView === 'terminal' || ctx.currentView === 'log') {
        // Terminal + Log view: same button bar
        appendStandardActionButtons(bar);
        bar.classList.remove('hidden');
    } else {
        bar.classList.add('hidden');
    }
}


/**
 * Clean ctx.terminal output by removing clutter
 * - Collapse multiple blank lines
 * - Remove spinner lines (Braille spinners, etc.)
 * - Remove progress-only lines
 * - Clean up carriage return artifacts
 */
// cleanTerminalOutput — moved to src/utils.js

// stripAnsi — moved to src/utils.js

// escapeRegExp — moved to src/utils.js

/**
 * Load log content for the hybrid view
 */
let logLoaded = false;
let activeLogFilter = 'all';  // Current filter type

/**
 * Classify a log message group for filtering.
 * Returns: 'error' | 'permission' | 'output' | 'system'
 */
// classifyLogEntry — moved to src/utils.js

/**
 * Apply current filter to all log cards in the DOM.
 */
function applyLogFilter() {
    if (!logContent) return;
    const cards = logContent.querySelectorAll('.log-card');
    cards.forEach(card => {
        if (activeLogFilter === 'all') {
            card.classList.remove('filtered-out');
            return;
        }
        const type = card.dataset.logType || 'system';
        // Map filter button to entry types
        const filterMap = {
            'errors': ['error'],
            'permissions': ['permission'],
            'output': ['output', 'system'],
        };
        const allowedTypes = filterMap[activeLogFilter] || [];
        if (allowedTypes.includes(type)) {
            card.classList.remove('filtered-out');
        } else {
            card.classList.add('filtered-out');
        }
    });
}

/**
 * Setup log filter bar handlers and show/hide based on team presence.
 */
function setupLogFilterBar() {
    const filterBar = document.getElementById('logFilterBar');
    if (!filterBar) return;

    // Type filter buttons
    filterBar.querySelectorAll('.log-type-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            filterBar.querySelectorAll('.log-type-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeLogFilter = btn.dataset.type;
            applyLogFilter();
        });
    });

    // Agent filter (populate from team state)
    const agentSelect = document.getElementById('logAgentFilter');
    if (agentSelect) {
        agentSelect.addEventListener('change', () => {
            // Agent filtering is a stretch goal — for now just re-apply type filter
            applyLogFilter();
        });
    }
}

/**
 * Show/hide filter bar based on team presence (only useful with multi-agent).
 * Also populates the agent filter dropdown from current team state.
 */
function updateLogFilterBarVisibility() {
    const filterBar = document.getElementById('logFilterBar');
    if (!filterBar) return;
    const teamHere = isTeamInCurrentRepo();
    if (teamHere) {
        filterBar.classList.remove('hidden');
        // Populate agent dropdown from team state
        const agentSelect = document.getElementById('logAgentFilter');
        if (agentSelect && ctx.teamState?.team) {
            const current = agentSelect.value;
            agentSelect.innerHTML = '<option value="all">All Agents</option>';
            const members = [ctx.teamState.team.leader, ...(ctx.teamState.team.agents || [])].filter(Boolean);
            for (const m of members) {
                const opt = document.createElement('option');
                opt.value = m.agent_name || m.target_id;
                const label = m.team_role === 'leader'
                    ? 'Leader'
                    : (m.agent_name || '').replace(/^a-/, '');
                opt.textContent = label;
                agentSelect.appendChild(opt);
            }
            // Restore previous selection if still valid
            if (current && [...agentSelect.options].some(o => o.value === current)) {
                agentSelect.value = current;
            }
        }
    } else {
        filterBar.classList.add('hidden');
    }
}

async function loadLogContent() {
    if (!logContent) return;

    // Only load once per session (refresh button can reload)
    if (logLoaded) return;

    // Only show loading indicator if content area is empty (initial load)
    // On subsequent switches, keep existing content visible while fetching
    const hasContent = logContent.children.length > 0 &&
                       !logContent.querySelector('.log-loading') &&
                       !logContent.querySelector('.log-empty') &&
                       !logContent.querySelector('.log-error');
    if (!hasContent) {
        logContent.innerHTML = '<div class="log-loading">Loading context...</div>';
    }

    try {
        // Include pane_id to avoid race condition with other tabs
        const paneParam = ctx.activeTarget ? `?pane_id=${encodeURIComponent(ctx.activeTarget)}` : '';
        const response = await apiFetch(`/api/log${paneParam}`);
        if (!response.ok) {
            throw new Error('Failed to fetch log');
        }
        const data = await response.json();

        if (!data.exists || (!data.content && !data.messages)) {
            // Only show empty message if we don't have existing content
            if (!hasContent) {
                logContent.innerHTML = '<div class="log-empty">No recent activity</div>';
            }
            logLoaded = true;
            return;
        }

        // Parse and render log entries
        // Use messages array if available (preserves code blocks), fall back to content string
        renderLogEntries(data.messages || data.content, data.cached);
        logLoaded = true;

        // Update last modified time for change detection
        lastLogModified = data.modified || 0;

    } catch (error) {
        console.error('Log error:', error);
        // Only show error if we don't have existing content
        if (!hasContent) {
            logContent.innerHTML = `<div class="log-error">Error loading log: ${escapeHtml(error.message)}</div>`;
        }
    }
}

/**
 * Render log entries in the hybrid view log section
 * NON-BLOCKING: Uses chunked DOM insertion with yielding to prevent UI freezes
 * Parses conversation format: $ user | • Tool: | assistant text
 * @param {string|string[]} contentOrMessages - Either content string or array of messages
 * @param {boolean} cached - Whether content is from cache (after /clear)
 */
const LOG_MAX_ENTRIES = 200;  // Cap DOM entries to prevent unbounded growth
const LOG_CHUNK_SIZE = 10;    // Entries per chunk before yielding
let logRenderAbort = null;    // AbortController for cancelling in-progress renders

function renderLogEntries(contentOrMessages, cached = false) {
    if (!logContent) return;

    // Cancel any in-progress render
    if (logRenderAbort) {
        logRenderAbort.abort();
    }
    logRenderAbort = new AbortController();
    const signal = logRenderAbort.signal;

    // Handle both array (new API) and string (legacy/cache) input
    // Each entry is either a string or {text: string, tool: {...}}
    let blocks;
    if (Array.isArray(contentOrMessages)) {
        blocks = contentOrMessages.map(msg => {
            if (typeof msg === 'object' && msg !== null && msg.text) {
                return { text: stripAnsi(msg.text).trim(), tool: msg.tool || null };
            }
            return { text: stripAnsi(String(msg)).trim(), tool: null };
        }).filter(b => b.text);
    } else {
        // Legacy format: string split by double newline
        const content = stripAnsi(contentOrMessages);
        blocks = content.split('\n\n').filter(b => b.trim()).map(t => ({ text: t.trim(), tool: null }));
    }

    if (blocks.length === 0) {
        logContent.innerHTML = '<div class="log-empty">No recent activity</div>';
        return;
    }

    // Group consecutive messages by role
    const messages = [];
    let currentGroup = null;

    for (const block of blocks) {
        const trimmed = block.text;
        if (!trimmed) continue;

        let role, text;

        if (trimmed.startsWith('$ ')) {
            role = 'user';
            text = trimmed.slice(2);
        } else if (trimmed.startsWith('• ')) {
            role = 'tool';
            text = trimmed;
        } else {
            role = 'assistant';
            text = trimmed;
        }

        if (currentGroup && (
            (currentGroup.role === 'assistant' && role === 'assistant') ||
            (currentGroup.role === 'assistant' && role === 'tool') ||
            (currentGroup.role === 'tool' && role === 'tool')
        )) {
            currentGroup.blocks.push({ role, text, tool: block.tool });
        } else {
            if (currentGroup) messages.push(currentGroup);
            currentGroup = {
                role: role === 'tool' ? 'assistant' : role,
                blocks: [{ role, text, tool: block.tool }]
            };
        }
    }
    if (currentGroup) messages.push(currentGroup);

    // Create DOM elements (non-blocking via chunked insertion)
    renderLogEntriesChunked(messages, cached, signal, contentOrMessages);
}

/**
 * Chunked DOM insertion with yielding - prevents main thread blocking.
 *
 * Builds new content in a DocumentFragment OFF-DOM so the user's existing
 * view (and scroll position) is untouched during the chunked build. Only
 * at the end do we atomically swap old children for new via
 * ``replaceChildren``. This avoids the "log jumps to the top then snaps
 * back to bottom" flash that happens when you clear innerHTML first,
 * append chunks over multiple paints, and then re-pin to bottom.
 */
async function renderLogEntriesChunked(messages, cached, signal, contentOrMessages) {
    // Remember whether the user was pinned to the bottom before the swap,
    // so we can restore that state after replaceChildren. refreshLogContent
    // only calls in here when userAtBottom was true, but full (re)loads
    // come through here too and may happen mid-scroll.
    const wasAtBottom = userAtBottom;

    // Build everything off-DOM. The existing logContent children remain
    // visible (and scrollable) until we swap them out.
    const staging = document.createDocumentFragment();

    // Add cached banner if needed
    if (cached) {
        const banner = document.createElement('div');
        banner.className = 'log-cached-banner';
        banner.textContent = 'Showing cached log (session was cleared)';
        staging.appendChild(banner);
    }

    // Cap message count up front so we don't build DOM we're about to drop.
    const startIdx = Math.max(0, messages.length - LOG_MAX_ENTRIES);
    const visibleMessages = startIdx > 0 ? messages.slice(startIdx) : messages;

    // Process messages in chunks with yielding. Appending to a fragment
    // does no layout/paint — the yield just lets other work interleave.
    for (let i = 0; i < visibleMessages.length; i += LOG_CHUNK_SIZE) {
        if (signal.aborted) return;

        const chunk = visibleMessages.slice(i, i + LOG_CHUNK_SIZE);
        for (const msg of chunk) {
            staging.appendChild(createLogCard(msg));
        }

        // Yield to browser after each chunk (let UI breathe)
        if (i + LOG_CHUNK_SIZE < visibleMessages.length) {
            await yieldToMain();
        }
    }

    if (signal.aborted) return;

    // Apply collapse passes on the off-DOM fragment BEFORE the swap.
    // This is the critical step: if we collapse after inserting, the
    // async idle callback shrinks the content and the pin-to-bottom we
    // did moments ago is wrong — the user sees the log "jump up". By
    // pre-collapsing, the fragment's final layout is settled before it
    // becomes visible.
    applyCollapseSync(staging);

    // Lock scroll-tracking briefly so the DOM swap and the pin-to-bottom
    // that follows don't fire scroll handlers that flip userAtBottom off.
    scrollLockUntil = Date.now() + 300;

    // Atomic swap: old children (which the user was looking at) are
    // replaced in one shot by the freshly-built tree. The scroll position
    // resets to 0 here because the referenced nodes are gone, but the
    // very next line re-pins it — the user never sees the top.
    logContent.replaceChildren(staging);

    // Restore scroll position. If the user was at the bottom before, pin
    // to the new bottom. Otherwise keep them where they were (handled by
    // refreshLogContent, which won't call us unless they're at bottom).
    if (wasAtBottom) {
        logContent.scrollTop = logContent.scrollHeight;
        // RAF one more time in case layout is still pending after paint
        requestAnimationFrame(() => {
            logContent.scrollTop = logContent.scrollHeight;
        });
    }

    // Plan previews still run async (they fetch + parse markdown), and
    // scheduleCollapse/scheduleSuperCollapse are kept for click handlers
    // that re-collapse after toggling expansion — they're harmless on
    // already-collapsed content (hash check short-circuits).
    schedulePlanPreviews();

    // Extract prompts
    const contentString = Array.isArray(contentOrMessages)
        ? contentOrMessages.join('\n\n')
        : (contentOrMessages || '');
    extractPendingPrompt(contentString);
}

/**
 * Create a single log card DOM element
 * DEFERRED MARKDOWN: Uses data-markdown attribute, parses lazily
 */
function createLogCard(msg) {
    const card = document.createElement('div');
    card.className = `log-card ${msg.role === 'user' ? 'user' : 'assistant'}`;

    // Tag for filtering
    card.dataset.logType = classifyLogEntry(msg);

    const header = document.createElement('div');
    header.className = 'log-card-header';
    header.innerHTML = `<span class="log-role-badge">${msg.role === 'user' ? 'You' : escapeHtml(agentName)}</span>`;
    card.appendChild(header);

    const body = document.createElement('div');
    body.className = 'log-card-body';

    for (let bi = 0; bi < msg.blocks.length; bi++) {
        const block = msg.blocks[bi];
        // Inline question card for AskUserQuestion entries
        // Collect the ❓ line + subsequent numbered-option lines into one card
        if (block.text && block.text.startsWith('❓')) {
            let qText = block.text;
            while (bi + 1 < msg.blocks.length && /^\s+\d+\./.test(msg.blocks[bi + 1].text)) {
                bi++;
                qText += '\n' + msg.blocks[bi].text;
            }
            body.appendChild(createQuestionCard(qText));
            continue;
        }
        if (block.role === 'tool') {
            const toolMatch = block.text.match(/^• (\w+):?\s*(.*)/s);
            if (toolMatch) {
                const toolName = toolMatch[1];
                const toolDetail = toolMatch[2] || '';
                const toolMeta = block.tool || null;

                const summary = toolDetail.length > 60 ? toolDetail.slice(0, 60) + '...' : toolDetail;
                const summaryKey = (summary || toolName).slice(0, 40).replace(/[^a-zA-Z0-9]/g, '_');

                const details = document.createElement('details');
                details.className = 'log-tool';
                details.dataset.tool = toolName;
                details.dataset.toolKey = `${toolName}:${summaryKey}`;
                if (toolMeta && toolMeta.tool_use_id) {
                    details.dataset.toolUseId = toolMeta.tool_use_id;
                }

                const summaryEl = document.createElement('summary');
                summaryEl.className = 'log-tool-summary';

                let summaryHtml = `<span class="log-tool-name">${escapeHtml(toolName)}</span> <span class="log-tool-detail">${escapeHtml(summary)}</span>`;

                // Add result badge from structured metadata
                if (toolMeta && toolMeta.result_summary) {
                    const badgeClass = toolMeta.result_status === 'error' ? 'error' : 'ok';
                    summaryHtml += ` <span class="log-tool-result ${badgeClass}">${escapeHtml(toolMeta.result_summary)}</span>`;
                }

                summaryEl.innerHTML = summaryHtml;
                details.appendChild(summaryEl);

                const content = document.createElement('div');
                content.className = 'log-tool-content';
                content.textContent = toolDetail;
                details.appendChild(content);

                body.appendChild(details);
            } else {
                const div = document.createElement('div');
                div.className = 'log-tool-inline';
                div.textContent = block.text;
                body.appendChild(div);
            }
        } else if (block.role === 'user') {
            const div = document.createElement('div');
            div.className = 'log-text user-text';
            div.textContent = block.text;
            body.appendChild(div);
        } else {
            // Assistant text - DEFERRED markdown parsing
            const div = document.createElement('div');
            div.className = 'log-text assistant-text';
            // Store raw text, parse lazily when visible
            div.dataset.markdown = block.text;
            // Show plain text initially (fast)
            div.textContent = block.text.slice(0, 500) + (block.text.length > 500 ? '...' : '');
            // Schedule markdown parsing for idle time
            scheduleMarkdownParse(div);
            body.appendChild(div);
        }
    }

    card.appendChild(body);

    // "Ask AI" action for error log cards (not user input)
    if (card.dataset.logType === 'error' && msg.role !== 'user') {
        const actionRow = document.createElement('div');
        actionRow.className = 'log-card-actions';
        const askBtn = document.createElement('button');
        askBtn.className = 'log-ask-ai-btn';
        askBtn.textContent = 'Ask AI';
        askBtn.title = 'Send error context to agent for debugging';
        askBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            askAIAboutError(msg);
        });
        actionRow.appendChild(askBtn);
        card.appendChild(actionRow);
    }

    return card;
}

// Tracks which queue item (if any) is currently being edited in the
// compose modal. Set by prefillCompose when called from queue.js's
// "Edit" path; consumed by sendComposedText/queueComposedText to
// dequeue the original BEFORE the new send/enqueue lands. Cleared on
// modal close. Without this, a parallel remove+enqueue can produce
// duplicates when reconcileQueue (e.g. on mobile WS reconnect)
// refetches server state in the gap between the two POSTs.
let composeEditingItemId = null;

/**
 * Pre-fill compose modal with text. Optional ``editingItemId`` marks
 * this open as an edit of an existing queue item, so on Send/Queue
 * the original is removed first (awaited) and only then re-enqueued.
 *
 * Exposed on window so ES module features (activity.js, queue.js)
 * can call it.
 */
function prefillCompose(text, editingItemId = null) {
    composeEditingItemId = editingItemId;
    composeDraft = text;
    composeInput.value = text;
    // Populate pane selector
    const ps = document.getElementById('composePaneSelect');
    if (ps) {
        ps.innerHTML = '';
        for (const t of (ctx.targets || [])) {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.project || t.window_name || t.id;
            if (t.id === ctx.activeTarget) opt.selected = true;
            ps.appendChild(opt);
        }
    }
    composeModal.classList.remove('hidden');
    setTimeout(() => {
        composeInput.focus();
        composeInput.setSelectionRange(text.length, text.length);
    }, 100);
}
window.prefillCompose = prefillCompose;

/**
 * Pre-fill compose modal with error context for AI debugging
 */
function askAIAboutError(msg) {
    const errorText = msg.blocks.map(b => b.text).join('\n').trim();
    const maxLen = 1500;
    const truncated = errorText.length > maxLen
        ? errorText.slice(0, maxLen) + '\n[...truncated]'
        : errorText;

    const prompt = `Debug this error:\n\n${truncated}\n\nAnalyze the error above and suggest a fix.`;
    prefillCompose(prompt);
}

// yieldToMain — moved to src/utils.js


/**
 * Extract pending prompt from log content
 * Detects AskUserQuestion tool calls (❓ prefix) and heuristic patterns
 * Updates pendingPrompt state and shows/hides banner
 */
function extractPendingPrompt(content) {
    // Track when log content last changed (for confirmation settling)
    const contentTail = content.slice(-200);
    if (contentTail !== _lastLogContentHash) {
        _lastLogContentHash = contentTail;
        _logSettledAt = Date.now();
    }

    // Check if last message is from assistant (not user)
    const blocks = content.split('\n\n').filter(b => b.trim());
    if (blocks.length === 0) {
        clearPendingPrompt();
        return;
    }

    // Find the last assistant turn (scan backwards until we hit a user message)
    let lastAssistantBlocks = [];
    for (let i = blocks.length - 1; i >= 0; i--) {
        const trimmed = blocks[i].trim();
        if (trimmed.startsWith('$ ')) {
            // Hit user message, stop
            break;
        }
        lastAssistantBlocks.unshift(trimmed);
    }

    console.debug('[PromptDetect] lastAssistantBlocks:', lastAssistantBlocks.length);

    if (lastAssistantBlocks.length === 0) {
        clearPendingPrompt();
        return;
    }

    // Join last assistant blocks and check for question markers
    const assistantContent = lastAssistantBlocks.join('\n\n');

    // Method 1: Structured detection (AskUserQuestion tool) - marked with ❓
    const questionMatch = assistantContent.match(/❓\s*(.+?)(?=\n\n|\n  \d\.|\n$|$)/s);
    if (questionMatch) {
        const questionText = questionMatch[1].trim();

        // Extract numbered options that follow
        const optionsMatch = assistantContent.match(/❓[^\n]*\n((?:\s+\d+\..+\n?)+)/);
        let choices = [];
        if (optionsMatch) {
            const optionLines = optionsMatch[1].trim().split('\n');
            for (const line of optionLines) {
                const optMatch = line.match(/^\s*(\d+)\.\s*(.+?)(?:\s+-\s+(.+))?$/);
                if (optMatch) {
                    choices.push({
                        num: optMatch[1],
                        label: optMatch[2].trim(),
                        description: optMatch[3] ? optMatch[3].trim() : ''
                    });
                }
            }
        }

        // Generate unique ID from question content
        const promptId = simpleHash(questionText + choices.map(c => c.label).join(''));

        // Check if this prompt was already dismissed
        if (dismissedPrompts.has(promptId)) {
            clearPendingPrompt();
            return;
        }

        // Check if this is the same prompt we already have
        if (pendingPrompt && pendingPrompt.id === promptId) {
            // Same prompt, don't update (preserve answered state)
            return;
        }

        pendingPrompt = {
            id: promptId,
            kind: 'question',
            text: questionText,
            choices: choices,
            answered: false,
            sentChoice: null
        };

        showPromptBanner();
        return;
    }

    // Method 2: Heuristic detection - numbered list at end of message
    const numberedListMatch = assistantContent.match(/([^\n]+(?:\n[^\n]+)*)\n\n?((?:\d+\.\s+.+\n?)+)$/);
    if (numberedListMatch) {
        const questionText = numberedListMatch[1].trim();
        const optionsText = numberedListMatch[2].trim();

        // Parse numbered options
        const choices = [];
        const optionLines = optionsText.split('\n');
        for (const line of optionLines) {
            const optMatch = line.match(/^(\d+)\.\s+(.+)$/);
            if (optMatch) {
                choices.push({
                    num: optMatch[1],
                    label: optMatch[2].trim(),
                    description: ''
                });
            }
        }

        // --- Rejection gates (all must pass) ---
        let rejected = false;
        let rejectReason = '';

        // Gate 1: Prompt intent — must end with ? OR contain a prompt-intent phrase
        const promptIntentRe = /\b(choose|pick|select|which|what do you want|would you like|do you prefer|do you want)\b/i;
        if (!questionText.endsWith('?') && !promptIntentRe.test(questionText)) {
            rejected = true;
            rejectReason = 'no question mark or prompt-intent phrase';
        }

        // Gate 2: Option count 2-6
        if (!rejected && (choices.length < 2 || choices.length > 6)) {
            rejected = true;
            rejectReason = 'option count out of range: ' + choices.length;
        }

        // Gate 3: Markdown formatting in labels (documentation, not choices)
        if (!rejected) {
            const hasMarkdown = choices.some(c => /\*\*|`/.test(c.label));
            if (hasMarkdown) {
                rejected = true;
                rejectReason = 'markdown in labels';
            }
        }

        // Gate 4: Label length — reject if max > 120 OR (avg > 60 AND 4+ choices)
        if (!rejected && choices.length > 0) {
            const maxLen = Math.max(...choices.map(c => c.label.length));
            const avgLen = choices.reduce((sum, c) => sum + c.label.length, 0) / choices.length;
            if (maxLen > 120 || (avgLen > 60 && choices.length >= 4)) {
                rejected = true;
                rejectReason = `label length: max=${maxLen}, avg=${Math.round(avgLen)}`;
            }
        }

        // Gate 5: Menu-like check — at least one option must look like a real choice
        if (!rejected) {
            const choiceTokenRe = /\b(other|none|cancel|back|yes|no|skip|default)\b/i;
            const hasMenuLikeOption = choices.some(c =>
                c.label.length <= 32 || choiceTokenRe.test(c.label)
            );
            if (!hasMenuLikeOption) {
                rejected = true;
                rejectReason = 'no menu-like options (all labels > 32 chars, no choice tokens)';
            }
        }

        if (rejected) {
            console.debug('[PromptDetect] Heuristic rejected:', rejectReason);
            // Fall through to confirmation detection (Method 3)
        } else {
            const promptId = simpleHash(questionText + choices.map(c => c.label).join(''));

            if (dismissedPrompts.has(promptId)) {
                clearPendingPrompt();
                return;
            }

            if (pendingPrompt && pendingPrompt.id === promptId) {
                return;
            }

            pendingPrompt = {
                id: promptId,
                kind: 'heuristic',
                text: questionText,
                choices: choices,
                answered: false,
                sentChoice: null
            };

            showPromptBanner();
            return;
        }
    }

    // Method 3: Confirmation pattern detection
    // STRICT: Only match explicit binary yes/no confirmations, NOT open questions
    // Triggers: plan approval, tool confirmations, explicit proceed/continue requests
    // Does NOT trigger on: open-ended questions like "would you like me to help?"
    const confirmPatterns = [
        // Explicit binary indicators
        /\(y\/n\)/i,
        /\(yes\/no\)/i,
        // Plan mode approval (very specific)
        /ready (for|to get) (your )?(approval|feedback)\s*\?/i,
        /approve (this|the) plan\s*\?/i,
        /plan.*(ready|complete|finalized).*\?/i,
        /proceed with (this|the) (plan|implementation)\s*\?/i,
        /written.*plan.*review/i,
        /plan.*written.*approval/i,
        /shall I (begin|start|proceed with) (the )?implement/i,
        // Explicit action confirmations (require "?" at sentence end)
        /\bproceed\s*\?\s*$/im,
        /\bcontinue\s*\?\s*$/im,
        /\bconfirm\s*\?\s*$/im,
        // Git/commit specific
        /\bcommit (these|the|this|your)?\s*(changes?)?\s*\?\s*$/im,
        /ready to (commit|push)\s*\?\s*$/im,
        // Destructive action confirmations
        /is (this|that) (ok|okay|correct|right)\s*\?\s*$/im,
        /(delete|remove|discard|overwrite).*\?\s*$/im
    ];

    // Filter to text blocks only (not tool calls starting with •, ❓, 📋, ✅, 🤖, 📝)
    const textBlocks = lastAssistantBlocks.filter(b =>
        !b.startsWith('•') && !b.startsWith('❓') && !b.startsWith('📋') &&
        !b.startsWith('✅') && !b.startsWith('🤖') && !b.startsWith('📝')
    );

    console.debug('[PromptDetect] textBlocks:', textBlocks.length, textBlocks.map(b => b.slice(0, 50)));

    // Only show confirmation banners when agent is truly idle:
    // 1. Terminal not busy (prompt visible)
    // 2. Log content settled for 2+ seconds (no new output)
    // This prevents false positives on mid-conversation questions.
    const agentIdle = !terminalBusy && (Date.now() - _logSettledAt > 2000);

    if (agentIdle) {
        // Only check the LAST text block — mid-conversation blocks are not prompts
        const lastBlock = textBlocks[textBlocks.length - 1];
        if (lastBlock) {
            for (const pattern of confirmPatterns) {
                if (pattern.test(lastBlock)) {
                    const promptId = simpleHash(lastBlock);

                    if (dismissedPrompts.has(promptId)) {
                        break;
                    }

                    if (pendingPrompt && pendingPrompt.id === promptId) {
                        return;  // Same prompt already showing
                    }

                    pendingPrompt = {
                        id: promptId,
                        kind: 'confirmation',
                        text: lastBlock.slice(0, 200),
                        choices: [
                            { num: '1', label: 'Yes', description: '' },
                            { num: '2', label: 'No', description: '' }
                        ],
                        answered: false,
                        sentChoice: null
                    };

                    showPromptBanner();
                    return;
                }
            }
        }
    }

    // No pending prompt detected in log
    // But don't clear if we have a permission prompt from ctx.terminal capture
    if (!pendingPrompt || pendingPrompt.kind !== 'permission') {
        clearPendingPrompt();
    }
}

/**
 * Clear pending prompt state and hide banner
 */
function clearPendingPrompt() {
    pendingPrompt = null;
    hidePromptBanner();
}

/**
 * Extract permission prompts from ctx.terminal capture
 * Detects Claude Code's built-in permission prompts like:
 * "Do you want to proceed?"
 * "❯ 1. Yes"
 * "  2. Yes, and don't ask again..."
 */
function extractPermissionPrompt(terminalContent) {
    if (!terminalContent) return;

    // Suppress for 5s after auto-approval (terminal still shows old prompt briefly)
    if (Date.now() - _permAutoApprovedAt < 5000) return;

    // Skip Claude's session rating prompt — not actionable
    if (ctx.agentType === 'claude' && /How is Claude doing/i.test(terminalContent)) return;

    // Strip box-drawing characters (Claude Code wraps prompts in TUI boxes)
    // Characters: │ ╭ ╮ ╰ ╯ ─ ┌ ┐ └ ┘ ├ ┤ ┬ ┴ ┼
    const stripBox = (s) => s.replace(/[│╭╮╰╯─┌┐└┘├┤┬┴┼]/g, '').trim();

    // Look for permission prompt patterns
    // Pattern: question line followed by numbered options with ❯ selector
    const lines = terminalContent.split('\n');

    let questionLine = null;
    let questionLineIdx = -1;
    let choices = [];
    let inOptions = false;

    for (let i = 0; i < lines.length; i++) {
        const line = stripBox(lines[i]);
        if (!line) continue;

        // Detect question line (ends with ?)
        if (line.endsWith('?') && !line.startsWith('❯') && !line.match(/^\d+\./)) {
            questionLine = line;
            questionLineIdx = i;
            choices = [];
            inOptions = true;
            continue;
        }

        // Detect option lines (❯ 1. or just 1. or 2. etc)
        if (inOptions) {
            const optMatch = line.match(/^[❯>]?\s*(\d+)\.\s*(.+)$/);
            if (optMatch) {
                choices.push({
                    num: optMatch[1],
                    label: optMatch[2].trim().slice(0, 50),  // Truncate long labels
                    description: ''
                });
            } else if (choices.length > 0 && !line.match(/^\s/)) {
                // Non-indented non-option line after options - end of options
                break;
            }
        }
    }

    // Standalone option detection — only if selector (❯ or >) present
    if (!questionLine && choices.length === 0) {
        let hasSelector = false;
        for (let i = 0; i < lines.length; i++) {
            const line = stripBox(lines[i]);
            if (!line) continue;
            const optMatch = line.match(/^([❯>])\s*(\d+)\.\s*(.+)$/);
            if (optMatch) {
                hasSelector = true;
                choices.push({
                    num: optMatch[2],
                    label: optMatch[3].trim().slice(0, 50),
                    description: ''
                });
            } else {
                // Also collect non-selector numbered items IF we already found a selector
                const plainMatch = line.match(/^\s*(\d+)\.\s*(.+)$/);
                if (plainMatch && hasSelector) {
                    choices.push({
                        num: plainMatch[1],
                        label: plainMatch[2].trim().slice(0, 50),
                        description: ''
                    });
                }
            }
        }
        if (choices.length >= 2 && hasSelector) {
            questionLine = 'Select an option:';
        } else {
            choices = [];
        }
    }

    // Need a question and at least one choice
    if (!questionLine || choices.length === 0) {
        // No permission prompt in ctx.terminal - clear if we had one
        if (pendingPrompt && pendingPrompt.kind === 'permission') {
            clearPendingPrompt();
        }
        return;
    }

    const promptId = simpleHash(questionLine + choices.map(c => c.label).join(''));

    // Check if dismissed
    if (dismissedPrompts.has(promptId)) {
        return;
    }

    // Check if same prompt already showing
    if (pendingPrompt && pendingPrompt.id === promptId) {
        return;
    }

    // Extract tool name and target from lines preceding the question
    // Claude Code TUI formats:
    //   1. Box header: "╭─ Bash ──────╮" → after stripBox → "Bash"
    //   2. Prose: "Claude wants to use Bash" or "use the Edit tool"
    //   3. Standalone tool name on its own line
    let permTool = null;
    let permTarget = '';
    const knownTools = ['Bash', 'Edit', 'Write', 'Read', 'Glob', 'Grep', 'WebFetch', 'WebSearch', 'Agent', 'NotebookEdit'];
    const knownToolsLower = knownTools.map(t => t.toLowerCase());
    const prosePattern = /\b(?:use|wants to use|using)\s+(?:the\s+)?(\w+?)(?:\s+tool)?\b/i;
    if (questionLineIdx > 0) {
        // Scan lines above the question for tool mention
        for (let j = Math.max(0, questionLineIdx - 10); j < questionLineIdx; j++) {
            const prevLine = stripBox(lines[j]);
            if (!prevLine) continue;

            let found = null;

            // Method 1: Line IS a known tool name (box header after strip)
            const trimmed = prevLine.trim();
            const idx = knownToolsLower.indexOf(trimmed.toLowerCase());
            if (idx !== -1) {
                found = knownTools[idx];
            }

            // Method 2: Line starts with or contains a known tool name as standalone word
            if (!found) {
                for (let ti = 0; ti < knownTools.length; ti++) {
                    const re = new RegExp('\\b' + knownTools[ti] + '\\b', 'i');
                    if (re.test(prevLine)) {
                        found = knownTools[ti];
                        break;
                    }
                }
            }

            // Method 3: Prose pattern "wants to use <Tool>"
            if (!found) {
                const proseMatch = prevLine.match(prosePattern);
                if (proseMatch) {
                    const pi = knownToolsLower.indexOf(proseMatch[1].toLowerCase());
                    if (pi !== -1) found = knownTools[pi];
                }
            }

            if (found) {
                permTool = found;
                // Target is on following non-empty lines until question
                const targetParts = [];
                for (let k = j + 1; k < questionLineIdx; k++) {
                    const tl = stripBox(lines[k]);
                    // Skip lines that are just the tool name or empty
                    if (tl && tl.toLowerCase() !== found.toLowerCase()) {
                        targetParts.push(tl);
                    }
                }
                permTarget = targetParts.join(' ').trim();
                break;
            }
        }
    }

    console.debug('[PermissionPrompt] Detected:', questionLine, choices, 'tool:', permTool, 'target:', permTarget);

    pendingPrompt = {
        id: promptId,
        kind: 'permission',
        text: questionLine,
        choices: choices,
        answered: false,
        sentChoice: null,
        tool: permTool,
        target: permTarget,
    };

    showPromptBanner();
}

/**
 * Show sticky prompt banner at bottom of log view
 */
function showPromptBanner() {
    if (!promptBanner || !pendingPrompt) return;

    const { text, choices, kind, answered, sentChoice } = pendingPrompt;

    // Detect "Other" choices by label pattern
    const otherPattern = /^other\b/i;
    const otherAltPattern = /free.?text|custom.?response|feedback|something.?else|specify/i;

    // Build choice buttons HTML
    const manyChoices = choices.length >= 4;
    let choicesHtml = '';
    for (const choice of choices) {
        const isSelected = answered && sentChoice === choice.num;
        const isOther = otherPattern.test(choice.label) || otherAltPattern.test(choice.label);
        const btnClass = isSelected ? 'prompt-choice-btn selected' : 'prompt-choice-btn';
        const title = choice.description || choice.label;
        const otherAttr = isOther ? ` data-other="true"` : '';
        const descHtml = (choice.description && choice.description !== choice.label)
            ? `<span class="prompt-choice-desc">${escapeHtml(choice.description)}</span>`
            : '';
        choicesHtml += `<button class="${btnClass}" data-choice="${choice.num}"${otherAttr} title="${escapeHtml(title)}">
            <span class="prompt-choice-label">${escapeHtml(choice.label)}</span>
            ${descHtml}
        </button>`;
    }

    // Add "Always" buttons for permission prompts (fall back to Bash if tool unknown)
    let alwaysHtml = '';
    if (kind === 'permission') {
        alwaysHtml = `
            <button class="prompt-always-btn always-repo" data-scope="repo" title="Auto-allow for this repo">Always&middot;Repo</button>
            <button class="prompt-always-btn always-global" data-scope="global" title="Auto-allow everywhere">Always</button>
        `;
    }

    // Add dismiss button
    choicesHtml += `<button class="prompt-dismiss-btn" title="Dismiss (won't ask again)">✕</button>`;

    const displayText = text;

    const choicesClass = manyChoices ? 'prompt-banner-choices many-choices' : 'prompt-banner-choices';
    promptBanner.innerHTML = `
        <div class="prompt-banner-content">
            <span class="prompt-banner-icon">${kind === 'confirmation' ? '⚠️' : kind === 'permission' ? '🔒' : '❓'}</span>
            <span class="prompt-banner-text">${escapeHtml(displayText)}</span>
        </div>
        <div class="${choicesClass}">${choicesHtml}</div>
        ${alwaysHtml ? `<div class="prompt-banner-always">${alwaysHtml}</div>` : ''}
    `;

    promptBanner.classList.add('visible');

    // Wire up event handlers
    setupPromptBannerHandlers();

    // Auto-focus banner for keyboard interaction (desktop)
    if (ctx.uiMode === 'desktop-multipane' && !answered) {
        promptBanner.setAttribute('tabindex', '-1');
        requestAnimationFrame(() => promptBanner.focus({ preventScroll: true }));
    }
}

/**
 * Hide prompt banner
 */
function hidePromptBanner() {
    if (!promptBanner) return;
    promptBanner.classList.remove('visible');
    promptBanner.classList.remove('expanded');
    // Return focus to input on desktop
    if (ctx.uiMode === 'desktop-multipane' && logInput) {
        logInput.focus();
    }
}

/**
 * Setup event handlers for prompt banner buttons
 */
function setupPromptBannerHandlers() {
    if (!promptBanner) return;

    // Choice buttons
    promptBanner.querySelectorAll('.prompt-choice-btn').forEach(btn => {
        btn.onclick = (e) => {
            e.preventDefault();
            e.stopPropagation();
            const choice = btn.dataset.choice;
            if (btn.dataset.other === 'true') {
                console.log('[PromptBanner] "Other" button clicked, choice:', choice);
                showOtherInput(choice);
            } else {
                console.log('[PromptBanner] Button clicked, choice:', choice);
                sendPromptChoice(choice);
            }
        };
    });

    // Tap text to expand/collapse
    const textEl = promptBanner.querySelector('.prompt-banner-text');
    if (textEl) {
        textEl.onclick = () => {
            promptBanner.classList.toggle('expanded');
        };
    }

    // "Always" buttons for permission prompts
    promptBanner.querySelectorAll('.prompt-always-btn').forEach(btn => {
        btn.onclick = async (e) => {
            e.preventDefault();
            e.stopPropagation();
            const scope = btn.dataset.scope;
            if (!pendingPrompt) return;
            const perm = {
                tool: pendingPrompt.tool || 'Bash',
                target: pendingPrompt.target || '',
                repo: '',
            };
            // Fetch current repo path from permissions API
            try {
                const resp = await apiFetch(`/api/permissions/rules`);
                const data = await resp.json();
                if (data.repo) perm.repo = data.repo;
            } catch (_) {}
            await createPermissionRule(perm, scope);
            // Send "Yes" (first choice) to approve
            if (pendingPrompt.choices.length > 0) {
                sendPromptChoice(String(pendingPrompt.choices[0].num));
            }
            ctx.showToast(
                scope === 'repo' ? 'Rule created for this repo' : 'Global rule created',
                'success', 3000
            );
        };
    });

    // Dismiss button
    const dismissBtn = promptBanner.querySelector('.prompt-dismiss-btn');
    if (dismissBtn) {
        dismissBtn.onclick = () => {
            if (pendingPrompt) {
                if (dismissedPrompts.size > 500) dismissedPrompts.clear();
                dismissedPrompts.add(pendingPrompt.id);
            }
            clearPendingPrompt();
        };
    }

    // Keyboard shortcuts: number keys select choice, Enter confirms first, Escape dismisses
    promptBanner.onkeydown = (e) => {
        if (!pendingPrompt || pendingPrompt.answered) return;
        const { choices } = pendingPrompt;

        // Number keys 1-9 → select matching choice
        const num = parseInt(e.key, 10);
        if (num >= 1 && num <= choices.length) {
            e.preventDefault();
            const choice = choices[num - 1];
            if (choice.num != null) {
                sendPromptChoice(String(choice.num));
            }
            return;
        }

        // Enter → select first choice (primary action)
        if (e.key === 'Enter' && choices.length > 0) {
            e.preventDefault();
            sendPromptChoice(String(choices[0].num));
            return;
        }

        // Escape → dismiss
        if (e.key === 'Escape') {
            e.preventDefault();
            if (dismissedPrompts.size > 500) dismissedPrompts.clear();
            dismissedPrompts.add(pendingPrompt.id);
            clearPendingPrompt();
            // Return focus to input
            if (logInput) logInput.focus();
            return;
        }

        // Y/N for 2-choice prompts (yes/no, allow/deny style)
        if (choices.length === 2 && (e.key === 'y' || e.key === 'Y' || e.key === 'n' || e.key === 'N')) {
            e.preventDefault();
            const idx = (e.key === 'y' || e.key === 'Y') ? 0 : 1;
            sendPromptChoice(String(choices[idx].num));
            return;
        }
    };
}

/**
 * Send user's choice to ctx.terminal (idempotent)
 */
function sendPromptChoice(choice) {
    console.log('[sendPromptChoice] called with:', choice, 'pendingPrompt:', pendingPrompt);

    // Always send - don't bail if pendingPrompt was cleared by race condition
    // The user clicked the button, so they clearly want to send this choice

    // Idempotency: don't re-send if already sent this choice
    if (pendingPrompt && pendingPrompt.answered && pendingPrompt.sentChoice === choice) {
        console.log('[sendPromptChoice] Already sent this choice, returning');
        return;
    }

    // Mark as answered if we have a prompt
    if (pendingPrompt) {
        pendingPrompt.answered = true;
        pendingPrompt.sentChoice = choice;
    }

    // Update button UI to show selected state (without full re-render)
    if (promptBanner) {
        promptBanner.querySelectorAll('.prompt-choice-btn').forEach(btn => {
            if (btn.dataset.choice === choice) {
                btn.classList.add('selected');
            }
        });
    }

    // Send to ctx.terminal atomically via tmux send-keys
    console.log('[sendPromptChoice] Socket state:', ctx.socket?.readyState, 'WebSocket.OPEN:', WebSocket.OPEN);
    if (ctx.socket && ctx.socket.readyState === WebSocket.OPEN) {
        const choiceStr = String(choice).trim();
        console.log('[sendPromptChoice] Sending choice:', choiceStr);
        sendTextAtomic(choiceStr, true);
        setTerminalBusy(true);
        captureSnapshot('user_send');

        recentSentCommands.add(choiceStr);
        lastSuggestion = '';
        // Clear terminal tail so the question text doesn't linger
        if (activePromptContent) activePromptContent.textContent = '';
    } else {
        console.error('[sendPromptChoice] Socket not open!');
    }

    // Clear prompt after short delay (let the answer process)
    setTimeout(() => {
        clearPendingPrompt();
    }, 1500);
}

/**
 * Show textarea for "Other" option — no ctx.terminal I/O until Send
 */
function showOtherInput(choiceNum) {
    const choicesDiv = promptBanner.querySelector('.prompt-banner-choices');
    if (!choicesDiv) return;

    choicesDiv.innerHTML = `
        <div class="prompt-other-input">
            <textarea class="prompt-other-textarea"
                      placeholder="Type your feedback..." rows="3"></textarea>
            <div class="prompt-other-actions">
                <button class="prompt-other-cancel">Back</button>
                <button class="prompt-other-send">Send</button>
            </div>
        </div>
    `;

    const textarea = choicesDiv.querySelector('.prompt-other-textarea');
    const cancelBtn = choicesDiv.querySelector('.prompt-other-cancel');
    const sendBtn = choicesDiv.querySelector('.prompt-other-send');

    // Auto-focus textarea so mobile keyboard opens
    if (textarea) {
        setTimeout(() => textarea.focus(), 50);
    }

    cancelBtn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        restorePromptChoices();
    };

    sendBtn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        const text = textarea ? textarea.value.trim() : '';
        if (!text) {
            textarea.focus();
            return;
        }
        sendOtherFeedback(choiceNum, text);
    };
}

/**
 * Restore original choice buttons (Back from Other input)
 */
function restorePromptChoices() {
    showPromptBanner();
}

/**
 * Send "Other" choice + user feedback text to ctx.terminal
 * Sequence: choice number → Ctrl+U (clear prefill) → feedback text
 */
function sendOtherFeedback(choiceNum, text) {
    const choiceStr = String(choiceNum).trim();
    console.log('[sendOtherFeedback] Sending choice:', choiceStr, 'then feedback:', text);

    sendTextAtomic(choiceStr, true);

    setTimeout(() => {
        sendInput('\x15'); // Ctrl+U: clear any prefilled text
        sendTextAtomic(text, true);
    }, 50);

    if (pendingPrompt) {
        pendingPrompt.answered = true;
        pendingPrompt.sentChoice = choiceStr;
    }

    setTerminalBusy(true);
    captureSnapshot('user_send');

    setTimeout(() => {
        clearPendingPrompt();
    }, 1500);
}


/**
 * Setup scroll tracking for log view
 * Show floating "Backlog" action when text is selected in log view.
 */
function setupSelectionBacklog(container) {
    if (!container) return;

    // Create floating action container
    const fabContainer = document.createElement('div');
    fabContainer.className = 'selection-backlog-fab hidden';

    const fabSingle = document.createElement('button');
    fabSingle.className = 'selection-fab-btn';
    fabSingle.textContent = '+ Backlog';
    fabContainer.appendChild(fabSingle);

    const fabSplit = document.createElement('button');
    fabSplit.className = 'selection-fab-btn split';
    fabSplit.textContent = '+ Split';
    fabContainer.appendChild(fabSplit);

    document.body.appendChild(fabContainer);

    let hideTimer = null;
    let currentText = '';

    function showFab(rect, text) {
        currentText = text;
        const lines = splitBacklogLines(text);
        // Show split button only for multi-line selections (3+ items)
        fabSplit.classList.toggle('hidden', lines.length < 3);
        fabSplit.textContent = lines.length >= 3 ? `+ Split (${lines.length})` : '+ Split';

        // Always below selection (bottom of selected range)
        const top = rect.bottom + window.scrollY + 6;
        fabContainer.style.top = top + 'px';
        fabContainer.style.left = Math.min(rect.left + rect.width / 2 - 60, window.innerWidth - 160) + 'px';
        fabContainer.classList.remove('hidden');
        clearTimeout(hideTimer);
    }

    function hideFab() {
        hideTimer = setTimeout(() => fabContainer.classList.add('hidden'), 200);
    }

    document.addEventListener('selectionchange', () => {
        const sel = window.getSelection();
        if (!sel || sel.isCollapsed || !sel.toString().trim()) {
            hideFab();
            return;
        }
        const anchor = sel.anchorNode;
        if (!anchor || !container.contains(anchor)) {
            hideFab();
            return;
        }
        const text = sel.toString().trim();
        if (text.length < 5 || text.length > 5000) {
            hideFab();
            return;
        }
        try {
            const range = sel.getRangeAt(0);
            const rect = range.getBoundingClientRect();
            showFab(rect, text);
        } catch (_) {
            hideFab();
        }
    });

    // Single item add
    fabSingle.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (!currentText) return;
        const firstLine = currentText.split('\n')[0].slice(0, 120);
        addBacklogItem(firstLine, currentText, 'human');
        ctx.showToast('Added to backlog', 'success');
        clearSelectionAndHide();
    });

    // Split into multiple items
    fabSplit.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (!currentText) return;
        const lines = splitBacklogLines(currentText);
        let count = 0;
        for (const line of lines) {
            addBacklogItem(line.slice(0, 120), line, 'human');
            count++;
        }
        ctx.showToast(`Added ${count} items to backlog`, 'success');
        clearSelectionAndHide();
    });

    function clearSelectionAndHide() {
        const sel = window.getSelection();
        if (sel) sel.removeAllRanges();
        fabContainer.classList.add('hidden');
        currentText = '';
    }

    container.addEventListener('scroll', () => hideFab(), { passive: true });

    // === Card-tap selection mode (long-press to enter, tap to toggle) ===
    let cardSelectMode = false;
    let selectedCards = new Set();
    let longPressTimer = null;

    // Action bar (fixed at bottom)
    const cardBar = document.createElement('div');
    cardBar.className = 'card-select-bar hidden';
    const cardBarCount = document.createElement('span');
    cardBarCount.className = 'card-select-count';
    cardBar.appendChild(cardBarCount);
    const cardBarBacklog = document.createElement('button');
    cardBarBacklog.className = 'selection-fab-btn';
    cardBarBacklog.textContent = '+ Backlog';
    cardBar.appendChild(cardBarBacklog);
    const cardBarSplit = document.createElement('button');
    cardBarSplit.className = 'selection-fab-btn split';
    cardBarSplit.textContent = '+ Split';
    cardBar.appendChild(cardBarSplit);
    const cardBarCopy = document.createElement('button');
    cardBarCopy.type = 'button';
    cardBarCopy.className = 'selection-fab-btn';
    cardBarCopy.textContent = 'Copy';
    cardBar.appendChild(cardBarCopy);
    const cardBarDone = document.createElement('button');
    cardBarDone.type = 'button';
    cardBarDone.className = 'selection-fab-btn';
    cardBarDone.textContent = 'Done';
    cardBarDone.style.background = 'var(--bg-tertiary)';
    cardBarDone.style.color = 'var(--text-secondary)';
    cardBar.appendChild(cardBarDone);
    document.body.appendChild(cardBar);

    function enterCardSelect(card) {
        cardSelectMode = true;
        selectedCards.clear();
        toggleCard(card);
        container.classList.add('card-select-active');
    }

    function exitCardSelect() {
        cardSelectMode = false;
        selectedCards.forEach(c => c.classList.remove('card-selected'));
        selectedCards.clear();
        container.classList.remove('card-select-active');
        cardBar.classList.add('hidden');
    }

    function toggleCard(card) {
        if (selectedCards.has(card)) {
            selectedCards.delete(card);
            card.classList.remove('card-selected');
        } else {
            selectedCards.add(card);
            card.classList.add('card-selected');
        }
        updateCardBar();
    }

    function getSelectedText() {
        const texts = [];
        // Iterate in DOM order
        container.querySelectorAll('.log-card.card-selected').forEach(card => {
            const body = card.querySelector('.log-card-body');
            if (body) texts.push(body.textContent.trim());
        });
        return texts.join('\n\n');
    }

    function updateCardBar() {
        const count = selectedCards.size;
        if (count === 0) {
            exitCardSelect();
            return;
        }
        cardBarCount.textContent = count + ' selected';
        const text = getSelectedText();
        const lines = splitBacklogLines(text);
        cardBarSplit.classList.toggle('hidden', lines.length < 3);
        cardBarSplit.textContent = lines.length >= 3 ? '+ Split (' + lines.length + ')' : '+ Split';
        cardBar.classList.remove('hidden');
    }

    // Long-press detection on log cards
    container.addEventListener('touchstart', (e) => {
        if (cardSelectMode) return;
        const card = e.target.closest('.log-card');
        if (!card) return;
        longPressTimer = setTimeout(() => {
            longPressTimer = null;
            enterCardSelect(card);
            // Prevent text selection
            window.getSelection()?.removeAllRanges();
        }, 500);
    }, { passive: true });

    container.addEventListener('touchend', () => {
        if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
    });
    container.addEventListener('touchmove', () => {
        if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
    });

    // Tap to toggle in card-select mode
    container.addEventListener('click', (e) => {
        if (!cardSelectMode) return;
        const card = e.target.closest('.log-card');
        if (!card) return;
        e.preventDefault();
        e.stopPropagation();
        toggleCard(card);
    }, true);

    // Action bar buttons
    cardBarBacklog.addEventListener('click', () => {
        const text = getSelectedText();
        if (!text) return;
        const firstLine = text.split('\n')[0].slice(0, 120);
        addBacklogItem(firstLine, text, 'human');
        ctx.showToast('Added to backlog', 'success');
        exitCardSelect();
    });

    cardBarSplit.addEventListener('click', () => {
        const text = getSelectedText();
        if (!text) return;
        const lines = splitBacklogLines(text);
        for (const line of lines) addBacklogItem(line.slice(0, 120), line, 'human');
        ctx.showToast('Added ' + lines.length + ' items to backlog', 'success');
        exitCardSelect();
    });

    cardBarCopy.addEventListener('click', async () => {
        const text = getSelectedText();
        if (!text) return;
        try {
            // Modern path: navigator.clipboard. Requires secure context
            // (https or localhost). Falls through to the legacy path if
            // unavailable (e.g. plain http on an internal IP).
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
            } else {
                // Legacy fallback via a hidden textarea + execCommand.
                // Works in non-secure contexts where the Clipboard API
                // is gated. Deprecated but widely supported.
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                ta.style.pointerEvents = 'none';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }
            const n = selectedCards.size;
            ctx.showToast(`Copied ${n} card${n === 1 ? '' : 's'}`, 'success');
        } catch (e) {
            console.error('Copy failed:', e);
            ctx.showToast('Copy failed', 'error');
            return;
        }
        exitCardSelect();
    });

    cardBarDone.addEventListener('click', () => exitCardSelect());
}

/**
 * Split text into individual backlog items.
 * Handles: "- item", "* item", "N. item", "N) item", plain lines.
 * Skips empty lines and markdown headers used as context.
 */
function splitBacklogLines(text) {
    const lines = text.split('\n');
    const items = [];
    for (const raw of lines) {
        // Strip list prefixes: "- ", "* ", "1. ", "1) "
        const stripped = raw.replace(/^\s*[-*]\s+/, '').replace(/^\s*\d+[.)]\s+/, '').trim();
        if (!stripped) continue;
        // Skip markdown headers and horizontal rules
        if (/^#{1,4}\s/.test(stripped) || /^[-=]{3,}$/.test(stripped)) continue;
        // Skip lines that are just bold headers like "**Something**"
        if (/^\*\*[^*]+\*\*$/.test(stripped)) continue;
        items.push(stripped);
    }
    return items;
}

/**
 * Tracks if user is at bottom to control auto-scroll
 */
function setupScrollTracking() {
    if (!logContent) return;

    logContent.addEventListener('scroll', () => {
        // After a programmatic scroll-to-bottom, ignore events briefly
        if (Date.now() < scrollLockUntil) return;

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
            // Lock scroll tracking briefly so render reflows don't reset userAtBottom
            scrollLockUntil = Date.now() + 500;
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



/**
 * Extract last complete Claude message from ctx.terminal output
 * Detects message boundaries via prompt (❯) reappearance
 */
function extractLastAgentMessage(content) {
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
    const lastMessage = extractLastAgentMessage(content);
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
 * Escape HTML entities
 */
// escapeHtml — moved to src/utils.js

/**
 * Start auto-refresh for log view - singleflight async loop
 */
async function startLogAutoRefresh() {
    stopLogAutoRefresh();
    logRefreshController = new AbortController();
    const signal = logRefreshController.signal;

    // Singleflight async loop - only one request at a time
    while (!signal.aborted) {
        try {
            // Skip if page not visible or not in relevant view
            if (shouldLogRefreshRun()) {
                await refreshLogContent(signal);
            }
            await abortableSleep(LOG_REFRESH_INTERVAL, signal);
        } catch (error) {
            if (error.name === 'AbortError') break;
            console.debug('Log refresh loop error:', error);
            try { await abortableSleep(2000, signal); } catch { break; }
        }
    }
}

/**
 * Stop auto-refresh for log view
 */
function stopLogAutoRefresh() {
    if (logRefreshController) {
        logRefreshController.abort();
        logRefreshController = null;
    }
}

// Track last log modified time and content hash to avoid unnecessary re-renders
let lastLogModified = 0;
let lastLogContentHash = '';  // Simple hash to detect content changes

function simpleHash(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        const char = str.charCodeAt(i);
        hash = ((hash << 5) - hash) + char;
        hash = hash & hash;  // Convert to 32bit integer
    }
    return hash.toString();
}

/**
 * Refresh log content without resetting logLoaded flag
 * Uses singleflight pattern - caller manages concurrency via AbortController
 */
// Store pending content when user is scrolling
let pendingLogContent = null;

async function refreshLogContent(signal) {
    if (!logContent) return;
    // If logLoaded is false, a full loadLogContent is pending — don't race with it
    if (!logLoaded) return;

    try {
        // Include pane_id to avoid race condition with other tabs
        const paneParam = ctx.activeTarget ? `?pane_id=${encodeURIComponent(ctx.activeTarget)}` : '';
        const response = await apiFetch(`/api/log${paneParam}`, { signal });
        if (!response.ok) return;

        const data = await response.json();
        if (!data.exists || (!data.content && !data.messages)) {
            // Clear stale content from a previous pane
            if (lastLogContentHash === '' && logContent.children.length > 0) {
                logContent.innerHTML = '<div class="log-empty">No recent activity</div>';
            }
            return;
        }

        // Only re-render if content actually changed (check both mtime and hash)
        const contentHash = simpleHash(data.content || '');
        if (data.modified === lastLogModified && contentHash === lastLogContentHash) {
            return;  // No change, skip re-render
        }
        lastLogModified = data.modified || 0;
        lastLogContentHash = contentHash;

        // Use messages array if available (preserves code blocks)
        const logData = data.messages || data.content;

        // If user is NOT at bottom, don't re-render (would cause scroll jump)
        // Just store the content and show indicator
        if (!userAtBottom) {
            pendingLogContent = logData;
            showNewContentIndicator();
            return;
        }

        // User is at bottom - safe to re-render, then pin to bottom
        renderLogEntries(logData);
        pendingLogContent = null;
        logContent.scrollTop = logContent.scrollHeight;

        // Auto-hide permission banner if log content changed after a delay
        // (new content means Claude moved past the approval point)
        if (activePermissionId && permissionShownAt && (Date.now() - permissionShownAt > 3000)) {
            hidePermissionBanner();
        }

    } catch (error) {
        if (error.name === 'AbortError') throw error;  // Re-throw abort
        console.debug('Log auto-refresh failed:', error);
    }
}

/**
 * Setup log input field for sending commands
 */
function setupLogInput() {
    if (!logInput || !logSend) return;

    // Send on Enter, navigate history on ArrowUp/Down
    logInput.addEventListener('keydown', (e) => {
        // Escape → send ESC to terminal (interrupt agent)
        if (e.key === 'Escape') {
            e.preventDefault();
            sendTextAtomic('\x1b', false);
            return;
        }
        if ((e.key === 'Enter' || e.keyCode === 13) && !e.shiftKey) {
            e.preventDefault();
            sendLogCommand();
            historyIndex = -1;
            return;
        }
        // Shift+Enter → queue command instead of sending
        if ((e.key === 'Enter' || e.keyCode === 13) && e.shiftKey) {
            e.preventDefault();
            queueLogCommand();
            return;
        }
        if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (commandHistory.length === 0) return;
            if (historyIndex === -1) {
                historySavedInput = logInput.value;
                historyIndex = commandHistory.length - 1;
            } else if (historyIndex > 0) {
                historyIndex--;
            }
            logInput.value = commandHistory[historyIndex];
            return;
        }
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (historyIndex === -1) return;
            if (historyIndex < commandHistory.length - 1) {
                historyIndex++;
                logInput.value = commandHistory[historyIndex];
            } else {
                historyIndex = -1;
                logInput.value = historySavedInput || '';
            }
            return;
        }
    });

    // Send button - smart mode: send when idle, queue when busy
    logSend.addEventListener('click', () => {
        if (terminalBusy) {
            queueLogCommand();
        } else {
            sendLogCommand();
        }
    });

    // Queue button (desktop) — always queues
    const logQueue = document.getElementById('logQueue');
    if (logQueue) {
        logQueue.addEventListener('click', () => queueLogCommand());
    }

    // Focus mode: when input is tapped, refresh the active prompt
    logInput.addEventListener('focus', () => {
        refreshActivePrompt();
    });

    // Paste images/files into log input — upload and insert path
    logInput.addEventListener('paste', async (e) => {
        const items = e.clipboardData?.items;
        if (!items) return;
        for (const item of items) {
            if (item.type.startsWith('image/') || item.kind === 'file') {
                e.preventDefault();
                const file = item.getAsFile();
                if (file) await uploadAndInsertPath(file, logInput);
                return;
            }
        }
    });

    // Drag-and-drop files onto log view
    if (logView) {
        logView.addEventListener('dragover', (e) => {
            e.preventDefault();
            logView.classList.add('drag-over');
        });
        logView.addEventListener('dragleave', () => {
            logView.classList.remove('drag-over');
        });
        logView.addEventListener('drop', async (e) => {
            e.preventDefault();
            logView.classList.remove('drag-over');
            if (e.dataTransfer?.files) {
                for (const file of e.dataTransfer.files) {
                    await uploadAndInsertPath(file, logInput);
                }
            }
        });
    }
}

/**
 * Upload a file and insert its server path into an input element.
 *
 * Wrapped to register the upload as in-flight so a too-fast send from
 * the same input (Enter-after-paste before the upload returns) can
 * await it and not lose the placeholder→path swap.
 */
async function uploadAndInsertPath(file, inputEl) {
    const tracker = _uploadAndInsertPathInner(file, inputEl);
    inflightUploads.push(tracker);
    try {
        return await tracker;
    } finally {
        const i = inflightUploads.indexOf(tracker);
        if (i >= 0) inflightUploads.splice(i, 1);
    }
}

async function _uploadAndInsertPathInner(file, inputEl) {
    // Guard against empty files — mobile browsers sometimes hand back a
    // 0-byte File when a paste fails to marshal the image data.
    if (!file || file.size === 0) {
        showToast('Paste produced an empty file — try again', 'error');
        return;
    }

    const placeholder = `[uploading ${file.name}...]`;
    const sel = inputEl.selectionStart;
    const start = (sel == null) ? inputEl.value.length : sel;
    const endSel = inputEl.selectionEnd;
    const end = (endSel == null) ? start : endSel;
    const before = inputEl.value.substring(0, start);
    const after = inputEl.value.substring(end);
    inputEl.value = before + placeholder + after;

    try {
        const formData = new FormData();
        formData.append('file', file);
        const response = await apiFetch('/api/upload', {
            method: 'POST',
            body: formData,
        });
        if (!response.ok) {
            let errMsg = `HTTP ${response.status}`;
            try {
                const err = await response.json();
                if (err && err.error) errMsg = err.error;
            } catch (_) { /* body was not JSON */ }
            throw new Error(errMsg);
        }
        const data = await response.json();
        if (!data || !data.path) {
            throw new Error('Server returned no path');
        }
        inputEl.value = inputEl.value.replace(placeholder, data.path);
        showToast(`Uploaded ${data.filename}`, 'success');
    } catch (error) {
        console.error('[uploadAndInsertPath] failed:', error);
        inputEl.value = inputEl.value.replace(placeholder, '');
        const msg = (error && error.name === 'TypeError')
            ? `Network error: ${error.message}`
            : `Upload failed: ${error.message}`;
        showToast(msg, 'error');
    }
    inputEl.focus();
}

/**
 * Send command from log input to ctx.terminal.
 * Atomic send: command + carriage return as single write.
 *
 * Now async because we have to wait for any pending upload that the
 * paste handler kicked off — otherwise a fast user pastes an image
 * and immediately hits Enter, and we'd send the literal placeholder
 * "[uploading file.png...]" instead of the resolved server path.
 */
async function sendLogCommand() {
    if (isPreviewMode()) return;  // No input in preview mode
    if (!ctx.socket || ctx.socket.readyState !== WebSocket.OPEN) {
        showToast('Not connected yet', 'error');
        return;
    }

    if (inflightUploads.length > 0) {
        const n = inflightUploads.length;
        showToast(`Waiting for ${n} upload${n > 1 ? 's' : ''}…`, 'info', 1500);
        await Promise.allSettled(inflightUploads.slice());
    }

    const command = logInput ? logInput.value.trim() : '';

    // If empty, just send Enter for confirming prompts
    if (!command) {
        sendTextAtomic('', true);
        setTerminalBusy(true);
        scheduleEarlyBusyCheck();
        captureSnapshot('user_send');
        return;
    }

    // Atomic send via tmux send-keys (no PTY interleaving)
    sendTextAtomic(command, true);

    // Mark ctx.terminal as busy after sending
    setTerminalBusy(true);
    scheduleEarlyBusyCheck();
    captureSnapshot('user_send');

    // Clear input — track sent command so it won't be re-suggested
    logInput.value = '';
    logInput.dataset.autoSuggestion = 'false';
    lastSuggestion = command;
    recentSentCommands.add(command);
    // Cap set size at 20
    if (recentSentCommands.size > 20) {
        recentSentCommands.delete(recentSentCommands.values().next().value);
    }

    // Add to command history
    if (command && commandHistory[commandHistory.length - 1] !== command) {
        commandHistory.push(command);
        if (commandHistory.length > MAX_HISTORY_SIZE) {
            commandHistory.shift();
        }
        localStorage.setItem('terminalHistory', JSON.stringify(commandHistory));
    }

    // Invalidate log cache so next refresh picks up changes.
    // Use incremental refreshLogContent (not loadLogContent) to avoid
    // replacing the entire DOM — preserves scroll position and keeps
    // previous context (e.g. team summary) visible.
    lastLogContentHash = '';
    lastLogModified = 0;
    setTimeout(() => refreshLogContent(), 500);
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


// ============================================================================
// PREVIEW MODE FUNCTIONS
// ============================================================================

/**
 * Capture a snapshot (server-side)
 */
async function captureSnapshot(label = 'manual') {
    if (previewMode) return;  // Don't capture while previewing

    try {
        const resp = await apiFetch(`/api/rollback/preview/capture?label=${label}`, {
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
        const resp = await apiFetch(`/api/rollback/previews`);
        const data = await resp.json();
        console.log('Snapshots response:', data);
        previewSnapshots = data.snapshots || [];
        console.log('Snapshot count:', previewSnapshots.length);
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
        const resp = await apiFetch(`/api/rollback/preview/${snapId}`);
        if (!resp.ok) throw new Error('Snapshot not found');

        previewSnapshot = await resp.json();
        previewMode = snapId;

        // Notify server
        await apiFetch(`/api/rollback/preview/select?snap_id=${snapId}`, {
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
    await apiFetch(`/api/rollback/preview/select`, { method: 'POST' });

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
 * Render ctx.terminal from snapshot
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
 * Toggle the FAB menu (vertical list that opens drawer tabs)
 */
function toggleFabMenu() {
    const menu = document.getElementById('fabMenu');
    if (!menu) return;
    menu.classList.toggle('hidden');
}

function closeFabMenu() {
    const menu = document.getElementById('fabMenu');
    if (menu) menu.classList.add('hidden');
}

/**
 * Open unified drawer (defaults to queue tab)
 */
function openDrawer(tab) {
    const drawer = document.getElementById('previewDrawer');
    const backdrop = document.getElementById('drawerBackdrop');
    if (drawer) {
        drawer.classList.remove('hidden');
        if (backdrop) backdrop.classList.remove('hidden');
        drawerOpen = true;
        switchRollbackTab(tab || 'queue');
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
    stopActivity();
}

/**
 * Toggle pin state for a snapshot
 */
async function toggleSnapshotPin(snapId, pinned) {
    try {
        const resp = await apiFetch(`/api/rollback/preview/${snapId}/pin?pinned=${pinned}`, {
            method: 'POST'
        });
        if (resp.ok) {
            // Update local state and re-render
            const snap = previewSnapshots.find(s => s.id === snapId);
            if (snap) snap.pinned = pinned;
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
        const url = `/api/rollback/preview/${snapId}/export`;
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
 * Get friendly display name for snapshot label
 */
function getLabelDisplay(label) {
    const displays = {
        'user_send': 'User',
        'tool_call': 'Tool',
        'agent_done': 'Done',
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

}

/**
 * Setup preview event handlers
 */
function setupPreviewHandlers() {
    // Back to live button
    document.getElementById('previewBackToLive')?.addEventListener('click', exitPreviewMode);

    // Drawer close
    document.getElementById('previewDrawerClose')?.addEventListener('click', closePreviewDrawer);

    // Backdrop tap to close drawer + FAB menu
    document.getElementById('drawerBackdrop')?.addEventListener('click', () => {
        closePreviewDrawer();
        closeFabMenu();
    });

    // Close FAB menu when tapping anywhere outside it
    // Use mousedown (fires before click) to avoid race with toggleFabMenu
    document.addEventListener('mousedown', (e) => {
        const menu = document.getElementById('fabMenu');
        if (!menu || menu.classList.contains('hidden')) return;
        // Don't close if tap is inside the menu itself
        if (menu.contains(e.target)) return;
        // Don't close if tap is on the ••• button (let toggleFabMenu handle it)
        if (e.target.closest('.action-bar-btn')) return;
        closeFabMenu();
    });

    // FAB menu items — open surface at selected tab (or special actions)
    document.querySelectorAll('.fab-menu-item').forEach(item => {
        item.addEventListener('click', () => {
            const tab = item.dataset.tab;
            const action = item.dataset.action;
            closeFabMenu();
            if (action === 'launchTeam') {
                showLaunchTeamModal();
                return;
            }
            if (action === 'docs') {
                document.getElementById('docsModal')?.classList.remove('hidden');
                return;
            }
            if (tab) openSurface(tab);
        });
    });
    document.getElementById('fabMenuClose')?.addEventListener('click', closeFabMenu);

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

    // History tab — initialized via initHistory() in DOMContentLoaded

    // Process tab handlers
    document.getElementById('processRefreshBtn')?.addEventListener('click', () => {
        loadProcessStatus();
        loadDescendantProcesses();
    });
    document.getElementById('processTerminateBtn')?.addEventListener('click', () => terminateProcess(false));
    document.getElementById('processKillBtn')?.addEventListener('click', () => terminateProcess(true));
    document.getElementById('processRespawnBtn')?.addEventListener('click', respawnProcess);

    // MCP tab — initialized via initMcp() in DOMContentLoaded

    // Env tab — initialized via initEnv() in DOMContentLoaded
}

// MCP tab functions — moved to src/features/mcp.js
// Env tab functions — moved to src/features/env.js
// History tab functions — moved to src/features/history.js

/**
 * Switch between drawer tabs (Queue / Runner / Dev / History / Process)
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
    const historyContent = document.getElementById('historyTabContent');
    const processContent = document.getElementById('processTabContent');
    const pluginsContent = document.getElementById('pluginsTabContent');
    const mcpContent = document.getElementById('mcpTabContent');
    const envContent = document.getElementById('envTabContent');
    const activityContent = document.getElementById('activityTabContent');
    const backlogContent = document.getElementById('backlogTabContent');
    const permissionsContent = document.getElementById('permissionsTabContent');

    // Hide all tabs
    queueContent?.classList.add('hidden');
    queueContent?.classList.remove('active');
    backlogContent?.classList.add('hidden');
    backlogContent?.classList.remove('active');
    runnerContent?.classList.add('hidden');
    runnerContent?.classList.remove('active');
    devContent?.classList.add('hidden');
    devContent?.classList.remove('active');
    historyContent?.classList.add('hidden');
    historyContent?.classList.remove('active');
    processContent?.classList.add('hidden');
    processContent?.classList.remove('active');
    pluginsContent?.classList.add('hidden');
    pluginsContent?.classList.remove('active');
    mcpContent?.classList.add('hidden');
    mcpContent?.classList.remove('active');
    envContent?.classList.add('hidden');
    envContent?.classList.remove('active');
    activityContent?.classList.add('hidden');
    activityContent?.classList.remove('active');
    permissionsContent?.classList.add('hidden');
    permissionsContent?.classList.remove('active');
    stopActivity();

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
    } else if (tabName === 'history') {
        historyContent?.classList.remove('hidden');
        historyContent?.classList.add('active');
        loadHistory();
        loadGitStatus();  // Load git status for dirty checks
    } else if (tabName === 'process') {
        processContent?.classList.remove('hidden');
        processContent?.classList.add('active');
        loadProcessStatus();
        loadDescendantProcesses();
    } else if (tabName === 'plugins') {
        pluginsContent?.classList.remove('hidden');
        pluginsContent?.classList.add('active');
        loadPluginsTab();
    } else if (tabName === 'mcp') {
        mcpContent?.classList.remove('hidden');
        mcpContent?.classList.add('active');
        loadMcp();
    } else if (tabName === 'env') {
        envContent?.classList.remove('hidden');
        envContent?.classList.add('active');
        loadEnv();
    } else if (tabName === 'activity') {
        activityContent?.classList.remove('hidden');
        activityContent?.classList.add('active');
        loadActivity();
    } else if (tabName === 'backlog') {
        backlogContent?.classList.remove('hidden');
        backlogContent?.classList.add('active');
        refreshBacklogList();
    } else if (tabName === 'permissions') {
        permissionsContent?.classList.remove('hidden');
        permissionsContent?.classList.add('active');
        loadPermissions();
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
        const resp = await apiFetch(`/api/process/status`);
        processStatus = await resp.json();

        let html = '';
        if (processStatus.is_running) {
            const session = processStatus.session ? escapeHtml(processStatus.session) : '';
            html = `<span class="process-status-running">Running</span> ${session}`;
            banner.className = 'process-status-banner running';
        } else if (processStatus.pid) {
            html = `<span class="process-status-dead">Stopped</span>`;
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

// ── Pane Descendant Processes ─────────────────────────────────────────

/**
 * Load descendant processes for the active pane
 */
async function loadDescendantProcesses() {
    const listDiv = document.getElementById('descendantList');
    if (!listDiv) return;

    if (!ctx.activeTarget) {
        listDiv.innerHTML = '<div class="descendant-empty">No active pane</div>';
        return;
    }

    listDiv.innerHTML = '<div class="descendant-empty">Scanning...</div>';

    try {
        const resp = await apiFetch(
            `/api/process/children?pane_id=${encodeURIComponent(ctx.activeTarget)}`
        );
        if (!resp.ok) {
            listDiv.innerHTML = '<div class="descendant-empty">Error loading</div>';
            return;
        }
        const data = await resp.json();
        renderDescendantList(data, listDiv);
    } catch (e) {
        console.error('Failed to load descendants:', e);
        listDiv.innerHTML = '<div class="descendant-empty">Error loading</div>';
    }
}

/**
 * Render descendant process list
 */
function renderDescendantList(data, container) {
    if (!data.processes || data.processes.length === 0) {
        container.innerHTML = '<div class="descendant-empty">No background processes in this pane</div>';
        return;
    }

    const agentPid = data.agent_pid;
    // Separate agent runtime from background work
    const agentProcs = agentPid ? data.processes.filter(p => p.pid === agentPid) : [];
    const bgProcs = agentPid ? data.processes.filter(p => p.pid !== agentPid) : data.processes;

    let html = '';

    // Agent runtime (if detected)
    if (agentProcs.length > 0) {
        const proc = agentProcs[0];
        const age = formatElapsed(proc.elapsed_s);
        const cpuStr = proc.cpu_pct > 0 ? `${proc.cpu_pct}%` : '';
        const memStr = proc.mem_mb > 0 ? `${proc.mem_mb}M` : '';
        html += `<div class="descendant-item descendant-agent">` +
            `<div class="descendant-main">` +
            `<span class="descendant-label">Agent</span>` +
            `<span class="descendant-name">${escapeHtml(proc.name)}</span>` +
            `<span class="descendant-pid">${proc.pid}</span>` +
            (cpuStr ? `<span class="descendant-cpu">${cpuStr}</span>` : '') +
            (memStr ? `<span class="descendant-mem">${memStr}</span>` : '') +
            `<span class="descendant-age">${age}</span>` +
            `</div></div>`;
    }

    // Background processes
    html += bgProcs.map(proc => {
        const age = formatElapsed(proc.elapsed_s);
        const cpuStr = proc.cpu_pct > 0 ? `${proc.cpu_pct}%` : '';
        const memStr = proc.mem_mb > 0 ? `${proc.mem_mb}M` : '';
        const stateClass = proc.state === 'R' ? 'state-running'
            : proc.state === 'Z' ? 'state-zombie'
            : proc.state === 'D' ? 'state-blocked'
            : '';
        const indent = proc.depth > 1 ? ` style="padding-left: ${12 + (proc.depth - 1) * 12}px;"` : '';
        const shortCmd = proc.command.length > 60
            ? proc.command.slice(0, 60) + '...'
            : proc.command;

        return `<div class="descendant-item"${indent}>` +
            `<div class="descendant-main">` +
            `<span class="descendant-name">${escapeHtml(proc.name)}</span>` +
            `<span class="descendant-pid">${proc.pid}</span>` +
            (stateClass ? `<span class="descendant-state ${stateClass}">${proc.state}</span>` : '') +
            (cpuStr ? `<span class="descendant-cpu">${cpuStr}</span>` : '') +
            (memStr ? `<span class="descendant-mem">${memStr}</span>` : '') +
            `<span class="descendant-age">${age}</span>` +
            `</div>` +
            `<div class="descendant-cmd" title="${escapeHtml(proc.command)}">${escapeHtml(shortCmd)}</div>` +
            `</div>`;
    }).join('');

    if (bgProcs.length === 0 && agentProcs.length > 0) {
        html += '<div class="descendant-empty">No other background processes</div>';
    }

    container.innerHTML = html;
}

/**
 * Format elapsed seconds to compact human-readable string
 */
function formatElapsed(seconds) {
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86400)}d`;
}

/**
 * Load processes into the desktop sidebar section
 */
async function loadSidebarProcesses() {
    const body = document.getElementById('sidebarProcessBody');
    if (!body || !ctx.activeTarget) return;

    body.innerHTML = '<div class="descendant-empty">Scanning...</div>';
    try {
        const resp = await apiFetch(
            `/api/process/children?pane_id=${encodeURIComponent(ctx.activeTarget)}`
        );
        if (!resp.ok) { body.innerHTML = ''; return; }
        const data = await resp.json();
        renderSidebarProcessList(data, body);
    } catch {
        body.innerHTML = '';
    }
}

/**
 * Compact process list for the sidebar (no agent row — just background procs)
 */
function renderSidebarProcessList(data, container) {
    const agentPid = data.agent_pid;
    const procs = data.processes ? data.processes.filter(p => p.pid !== agentPid) : [];

    if (procs.length === 0) {
        container.innerHTML = '<div class="descendant-empty">No background processes</div>';
        return;
    }

    container.innerHTML = procs.map(proc => {
        const age = formatElapsed(proc.elapsed_s);
        const cpuStr = proc.cpu_pct > 0 ? ` ${proc.cpu_pct}%` : '';
        const memStr = proc.mem_mb > 0 ? ` ${proc.mem_mb}M` : '';
        return `<div class="sidebar-process-item" title="${escapeHtml(proc.command)}">` +
            `<span class="sidebar-proc-name">${escapeHtml(proc.name)}</span>` +
            `<span class="sidebar-proc-meta">${age}${cpuStr}${memStr}</span>` +
            `</div>`;
    }).join('');
}

/**
 * Update the processes pill in the header bar
 */
function updateProcessesPill(count) {
    const pill = document.getElementById('processesPill');
    if (!pill) return;

    if (!count || count === 0) {
        pill.classList.add('hidden');
        return;
    }

    pill.textContent = `${count} proc${count !== 1 ? 's' : ''}`;
    pill.classList.remove('hidden', 'procs-ok', 'procs-warn');
    pill.classList.add(count > 5 ? 'procs-warn' : 'procs-ok');
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
        const resp = await apiFetch(`/api/process/terminate?force=${force}&${getTargetParams()}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (data.success) {
            resultDiv.classList.add('success');
            resultDiv.innerHTML = `<pre>Process terminated (${escapeHtml(data.method)})\nPID: ${escapeHtml(String(data.pid))}</pre>`;
            showToast('Process terminated', 'success');
        } else {
            resultDiv.classList.add('error');
            resultDiv.innerHTML = `<pre>Error: ${escapeHtml(data.error)}</pre>`;
        }
    } catch (e) {
        console.error('Terminate failed:', e);
        resultDiv.classList.add('error');
        resultDiv.innerHTML = `<pre>Error: ${escapeHtml(e.message)}</pre>`;
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
        const resp = await apiFetch(`/api/process/respawn?${getTargetParams()}`, {
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
                if (ctx.socket) {
                    ctx.socket.close();
                }
            }, 500);
        } else {
            resultDiv.classList.add('error');
            resultDiv.innerHTML = `<pre>Error: ${escapeHtml(data.error)}</pre>`;
        }
    } catch (e) {
        console.error('Respawn failed:', e);
        resultDiv.classList.add('error');
        resultDiv.innerHTML = `<pre>Error: ${escapeHtml(e.message)}</pre>`;
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
        const resp = await apiFetch(`/api/runner/commands`);
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
            <button class="runner-cmd-btn" data-cmd-id="${escapeHtml(id)}" title="${escapeHtml(cmd.description)}">
                <span class="runner-cmd-icon">${escapeHtml(cmd.icon)}</span>
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
        const resp = await apiFetch(`/api/runner/execute?command_id=${commandId}&${getTargetParams()}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (data.success) {
            showToast(`Running: ${data.label}`, 'success');
            // Close drawer and switch to ctx.terminal view to see output
            closePreviewDrawer();
            if (ctx.currentView !== 'terminal') {
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
        const resp = await apiFetch(`/api/runner/custom?command=${encodeURIComponent(command)}&${getTargetParams()}`, {
            method: 'POST'
        });
        const data = await resp.json();

        if (data.success) {
            showToast('Command sent', 'success');
            input.value = '';
            // Close drawer and switch to ctx.terminal view
            closePreviewDrawer();
            if (ctx.currentView !== 'terminal') {
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
        const resp = await apiFetch(`/api/preview/config`);
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
                <button class="dev-service-tab ${isActive ? 'active' : ''}" data-service-id="${escapeHtml(svc.id)}">
                    <span class="dev-status-dot ${status}"></span>
                    <span class="dev-service-label">${escapeHtml(svc.label)}</span>
                    <span class="dev-service-port">:${escapeHtml(String(svc.port))}</span>
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
    const logsBtn = document.getElementById('devLogsBtn');

    [startBtn, restartBtn, stopBtn, openBtn, copyBtn, logsBtn].forEach(btn => {
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

    const placeholder = document.getElementById('devPreviewPlaceholder');
    const info = document.getElementById('devServiceInfo');
    const link = document.getElementById('devServiceLink');
    const linkLabel = document.getElementById('devServiceLinkLabel');
    const linkUrl = document.getElementById('devServiceLinkUrl');
    const statusLabel = document.getElementById('devServiceStatus');
    const status = devPreviewStatus[serviceId]?.status;
    const url = buildDevPreviewUrl(svc);

    if (placeholder) placeholder.classList.add('hidden');
    if (info) info.classList.remove('hidden');
    if (link) link.dataset.url = url;
    if (linkLabel) linkLabel.textContent = `Open ${svc.label}`;
    if (linkUrl) linkUrl.textContent = url;
    if (statusLabel) {
        if (status === 'running') {
            statusLabel.textContent = 'Running';
            statusLabel.style.color = 'var(--success)';
        } else if (status === 'stopped') {
            statusLabel.textContent = 'Stopped';
            statusLabel.style.color = 'var(--text-muted)';
        } else {
            statusLabel.textContent = status || 'Unknown';
            statusLabel.style.color = 'var(--warning)';
        }
    }
}

/**
 * Build preview URL for service (using Tailscale ctx.config or localhost fallback)
 */
function buildDevPreviewUrl(service) {
    if (devPreviewConfig?.tailscaleServe?.urlPattern) {
        return devPreviewConfig.tailscaleServe.urlPattern
            .replace('{hostname}', devPreviewConfig.tailscaleServe.hostname || '')
            .replace('{port}', service.port);
    }
    // If accessed over Tailscale HTTPS, use same origin + service path
    // (raw HTTP ports aren't exposed over Tailscale)
    if (window.location.protocol === 'https:' && window.location.hostname.includes('.ts.net')) {
        const path = service.path || '/';
        return `${window.location.origin}${path}`;
    }
    // Fallback: use the current host with service port (local network)
    const host = window.location.hostname || 'localhost';
    const path = service.path || '/';
    return `http://${host}:${service.port}${path}`;
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
    // Skip if page not visible (save resources)
    if (document.visibilityState !== 'visible') return;

    try {
        const resp = await apiFetch(`/api/preview/status`);
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
        const resp = await apiFetch(`/api/preview/start?service_id=${activeDevService}&${getTargetParams()}`, {
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
        const resp = await apiFetch(`/api/preview/stop?service_id=${activeDevService}&${getTargetParams()}`, {
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
async function openDevPreview() {
    const svc = devPreviewConfig?.services?.find(s => s.id === activeDevService);
    if (!svc) return;
    const url = buildDevPreviewUrl(svc);
    if (navigator.share) {
        try {
            await navigator.share({ title: svc.label, url });
            return;
        } catch (e) {
            if (e.name === 'AbortError') return;
        }
    }
    window.open(url, '_blank', 'noopener,noreferrer');
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
 * Toggle dev log viewer for the active service
 */
async function toggleDevLogs() {
    const viewer = document.getElementById('devLogViewer');
    const content = document.getElementById('devLogContent');
    const meta = document.getElementById('devLogMeta');
    if (!viewer || !content) return;

    // Toggle off
    if (!viewer.classList.contains('hidden')) {
        viewer.classList.add('hidden');
        return;
    }

    if (!activeDevService) {
        showToast('Select a service first', 'error');
        return;
    }

    viewer.classList.remove('hidden');
    content.textContent = 'Loading...';
    if (meta) meta.textContent = '';

    try {
        const resp = await apiFetch(
            `/api/preview/logs?service_id=${activeDevService}&tail=500`
        );
        const data = await resp.json();

        if (!data.exists || !data.content) {
            content.textContent = 'No logs yet. Start the service to begin capturing.';
        } else {
            content.textContent = data.content;
            content.scrollTop = content.scrollHeight;
            if (meta) {
                meta.textContent = `${data.lines} / ${data.total_lines} lines`;
            }
        }
    } catch (e) {
        content.textContent = `Error loading logs: ${e.message}`;
    }
}

/**
 * Copy dev log content to clipboard
 */
function copyDevLogs() {
    const content = document.getElementById('devLogContent');
    if (!content || !content.textContent) return;
    navigator.clipboard.writeText(content.textContent).then(() => {
        showToast('Logs copied', 'success');
    }).catch(() => {
        showToast('Copy failed', 'error');
    });
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
    document.getElementById('devLogsBtn')?.addEventListener('click', toggleDevLogs);
    document.getElementById('devLogCopyBtn')?.addEventListener('click', copyDevLogs);
    document.getElementById('devLogCloseBtn')?.addEventListener('click', () => {
        document.getElementById('devLogViewer')?.classList.add('hidden');
    });

    // Link card: share sheet (lets user pick browser/incognito), fallback to window.open
    document.getElementById('devServiceLink')?.addEventListener('click', async () => {
        const url = document.getElementById('devServiceLink')?.dataset.url;
        if (!url) return;
        if (navigator.share) {
            try {
                await navigator.share({ title: activeDevService || 'Preview', url });
                return;
            } catch (e) {
                if (e.name === 'AbortError') return; // User cancelled
            }
        }
        window.open(url, '_blank', 'noopener,noreferrer');
    });
}

// ============================================================================
// END DEV PREVIEW
// ============================================================================

// ============================================================================
// V2 MESSAGE HANDLER + PERMISSION BANNER
// ============================================================================

let activePermissionId = null;
let activePermissionPayload = null;
let permissionShownAt = 0;

/**
 * Route v2 typed messages to their handlers
 */
function handleTypedMessage(msg) {
    switch (msg.type) {
        case 'permission_request':
            handlePermissionRequest(msg.payload);
            break;
        case 'device_state':
            break;
        case 'push_config':
            break;
        case 'backlog_candidate':
            handleCandidateMessage(msg.payload);
            break;
        case 'permission_auto': {
            const d = msg.payload;
            const verb = d.decision === 'allow' ? 'Auto-approved' : 'Auto-denied';
            const tgt = (d.target || '').slice(0, 40);
            const repo = d.repo ? `[${d.repo}] ` : '';
            ctx.showToast(`${repo}${verb}: ${d.tool} ${tgt}`,
                d.decision === 'allow' ? 'info' : 'warning', 4000);
            // Suppress permission banner for a few seconds (terminal still shows old prompt)
            _permAutoApprovedAt = Date.now();
            // Clear any existing permission banner that was shown before auto-approve
            if (pendingPrompt && pendingPrompt.kind === 'permission') {
                clearPendingPrompt();
            }
            hidePermissionBanner();
            break;
        }
        default:
            console.debug('Unknown v2 type:', msg.type);
    }
}

/**
 * Show permission banner when Claude needs tool approval
 */
function handlePermissionRequest(payload) {
    const banner = document.getElementById('permissionBanner');
    if (!banner) return;

    activePermissionId = payload.id;
    activePermissionPayload = payload;
    permissionShownAt = Date.now();
    document.getElementById('permissionTool').textContent = payload.tool || 'Tool';
    document.getElementById('permissionTarget').textContent = payload.target || '';
    document.getElementById('permissionPreview').textContent = payload.context || payload.target || '';
    document.getElementById('permissionContext')?.classList.add('hidden');
    banner.classList.remove('hidden');

    // Permission banner supersedes prompt banner
    hidePromptBanner();
}

/**
 * Hide permission banner
 */
function hidePermissionBanner() {
    const banner = document.getElementById('permissionBanner');
    if (banner) banner.classList.add('hidden');
    activePermissionId = null;
    activePermissionPayload = null;
    permissionShownAt = 0;
}

/**
 * Setup permission banner button handlers (called once from DOMContentLoaded)
 */
function setupPermissionBanner() {
    document.getElementById('permissionAllow')?.addEventListener('click', () => {
        sendTextAtomic('y', true);
        setTerminalBusy(true);
        recentSentCommands.add('y');
        lastSuggestion = '';
        if (activePromptContent) activePromptContent.textContent = '';
        hidePermissionBanner();
    });

    document.getElementById('permissionDeny')?.addEventListener('click', () => {
        sendTextAtomic('n', true);
        setTerminalBusy(true);
        recentSentCommands.add('n');
        lastSuggestion = '';
        if (activePromptContent) activePromptContent.textContent = '';
        hidePermissionBanner();
    });

    document.getElementById('permissionMore')?.addEventListener('click', () => {
        document.getElementById('permissionContext')?.classList.toggle('hidden');
    });

    document.getElementById('permissionAlwaysRepo')?.addEventListener('click', async () => {
        if (activePermissionPayload) {
            await createPermissionRule(activePermissionPayload, 'repo');
        }
        sendTextAtomic('y', true);
        setTerminalBusy(true);
        recentSentCommands.add('y');
        lastSuggestion = '';
        if (activePromptContent) activePromptContent.textContent = '';
        hidePermissionBanner();
        ctx.showToast('Rule created for this repo', 'success');
    });

    document.getElementById('permissionAlways')?.addEventListener('click', async () => {
        if (activePermissionPayload) {
            await createPermissionRule(activePermissionPayload, 'global');
        }
        sendTextAtomic('y', true);
        setTerminalBusy(true);
        recentSentCommands.add('y');
        lastSuggestion = '';
        if (activePromptContent) activePromptContent.textContent = '';
        hidePermissionBanner();
        ctx.showToast('Global rule created', 'success');
    });
}

/**
 * Extract base command for rule matching.
 * "pytest tests/ -q" → "pytest"
 * "npm run build" → "npm run build"
 * "git status" → "git status"
 * Returns empty string for garbage input (TUI artifacts, comments, etc.)
 */
function extractBaseCommand(cmd) {
    const raw = (cmd || '').trim();
    if (!raw) return '';

    // Reject obvious garbage from terminal scraping
    // TUI artifacts, box-drawing leftovers, comment-only text
    if (/^[⎿╭╮╰╯│─┌┐└┘#]/.test(raw)) return '';
    if (raw.length > 200) return '';           // captured command body
    if (/\n/.test(raw)) return '';             // multiline = not a command name
    if (/^(Command |Contains |Running)/.test(raw)) return '';  // Claude warning text

    const parts = raw.split(/\s+/);
    if (['npm', 'npx', 'pnpm', 'yarn'].includes(parts[0]) && parts[1] === 'run') {
        return parts.slice(0, 3).join(' ');
    }
    if (['git'].includes(parts[0]) && parts.length > 1) {
        return parts.slice(0, 2).join(' ');
    }
    return parts[0] || '';
}

/**
 * Create a permission rule via the API.
 */
async function createPermissionRule(perm, scope) {
    const tool = perm.tool;
    const isCommand = tool === 'Bash';
    const baseCmd = isCommand ? extractBaseCommand(perm.target) : '';
    // For file tools, validate the target looks like an actual path
    let pathTarget = '';
    if (!isCommand && perm.target) {
        const t = perm.target.trim();
        // Reject if it looks like terminal garbage (too long, has TUI chars, spaces without path separators)
        if (t.length < 200 && !(/[⎿╭╮╰╯│─]/.test(t)) && (t.startsWith('/') || t.startsWith('.') || !t.includes(' '))) {
            pathTarget = t;
        }
    }
    const hasMatcher = isCommand ? !!baseCmd : !!pathTarget;
    const params = new URLSearchParams({
        tool,
        matcher_type: hasMatcher ? (isCommand ? 'command' : 'path') : 'tool_only',
        matcher: isCommand ? baseCmd : pathTarget,
        scope,
        scope_value: scope === 'repo' ? (perm.repo || '') : '',
        action: 'allow',
        created_from: 'banner',
        token: ctx.token,
    });
    try {
        await apiFetch(`/api/permissions/rules?${params}`, { method: 'POST' });
    } catch (e) {
        console.error('Failed to create permission rule:', e);
    }
}

/**
 * Create an inline question card for AskUserQuestion entries in the log view
 */
function createQuestionCard(text) {
    const card = document.createElement('div');
    card.className = 'log-question-card';

    const lines = text.split('\n');
    const question = lines[0].replace(/^❓\s*/, '');
    const options = lines.slice(1).filter(l => /^\s+\d+\./.test(l));

    const questionDiv = document.createElement('div');
    questionDiv.className = 'question-text';
    questionDiv.textContent = question;
    card.appendChild(questionDiv);

    if (options.length > 0) {
        const btnRow = document.createElement('div');
        btnRow.className = 'question-btn-row';

        options.forEach(opt => {
            const match = opt.match(/^\s+(\d+)\.\s+(.+)/);
            if (!match) return;
            const btn = document.createElement('button');
            btn.className = 'question-choice-btn';
            btn.textContent = match[2].split(' - ')[0];  // Label only
            btn.title = match[2];  // Full text on long-press
            btn.onclick = () => {
                sendTextAtomic(match[1], true);
                btnRow.querySelectorAll('button').forEach(b => b.disabled = true);
                btn.classList.add('selected');
            };
            btnRow.appendChild(btn);
        });

        card.appendChild(btnRow);
    }

    return card;
}


/**
 * Voice input via SpeechRecognition API
 */
/**
 * Push notification subscription
 */
let vapidPublicKey = null;

function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; i++) {
        outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
}

async function setupPushNotifications() {
    if (!('PushManager' in window) || !('serviceWorker' in navigator)) return;
    try {
        const resp = await apiFetch('/api/push/vapid-key');
        if (!resp.ok) return;
        const data = await resp.json();
        vapidPublicKey = data.key;
    } catch { return; }

    const btn = document.getElementById('pushToggleBtn');
    if (btn) {
        btn.classList.remove('hidden');
        btn.addEventListener('click', togglePushSubscription);
        updatePushButton();
    }

    navigator.serviceWorker.addEventListener('message', (event) => {
        if (event.data?.type === 'permission_response') {
            sendTextAtomic(event.data.choice, true);
        } else if (event.data?.type === 'respawn_agent') {
            // Respawn Claude from push notification action
            respawnAgent();
        }
    });
}

async function togglePushSubscription() {
    try {
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.getSubscription();
        if (sub) {
            await sub.unsubscribe();
            await apiFetch('/api/push/subscribe', {
                method: 'DELETE',
                body: JSON.stringify(sub.toJSON()),
                headers: {'Content-Type': 'application/json'}
            });
            showToast('Push disabled', 'info');
        } else {
            const newSub = await reg.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(vapidPublicKey)
            });
            await apiFetch('/api/push/subscribe', {
                method: 'POST',
                body: JSON.stringify(newSub.toJSON()),
                headers: {'Content-Type': 'application/json'}
            });
            showToast('Push enabled', 'success');
        }
        updatePushButton();
    } catch (e) {
        showToast('Push setup failed: ' + e.message, 'error');
    }
}

async function updatePushButton() {
    const btn = document.getElementById('pushToggleBtn');
    if (!btn) return;
    try {
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.getSubscription();
        btn.classList.toggle('subscribed', !!sub);
        btn.textContent = sub ? '\u{1F514}' : '\u{1F515}';
    } catch {}
}

// ============================================================================
// END COMPANION FEATURES
// ============================================================================

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
ctx.showToast = showToast;


// ===== Desktop Multi-Pane Layout =====

/**
 * Surface abstraction: routes to tools panel (desktop) or drawer (mobile)
 */
function openSurface(name) {
    if (ctx.uiMode === 'desktop-multipane') {
        openToolPanel(name);
    } else {
        openDrawer(name);
    }
}

/**
 * Map drawer tab names to their content element IDs
 */
const TOOL_TAB_MAP = {
    queue: 'queueTabContent',
    backlog: 'backlogTabContent',
    runner: 'runnerTabContent',
    dev: 'devTabContent',
    history: 'historyTabContent',
    process: 'processTabContent',
    plugins: 'pluginsTabContent',
    mcp: 'mcpTabContent',
    env: 'envTabContent',
    activity: 'activityTabContent',
    permissions: 'permissionsTabContent',
};

/**
 * Friendly display names for tool panels
 */
const TOOL_TITLES = {
    queue: 'Queue',
    backlog: 'Backlog',
    runner: 'Runner',
    dev: 'Dev Preview',
    history: 'History',
    process: 'Process',
    mcp: 'MCP',
    env: 'Env',
    team: 'Team',
    activity: 'Activity',
    permissions: 'Permissions',
};

/**
 * Open a tool panel on desktop. For 'team'/'queue', scroll to sidebar section.
 * For other tools, open in sidebar via openSidebarTool.
 */
function openToolPanel(name) {
    if (ctx.uiMode !== 'desktop-multipane') return;

    const sidebarSections = {
        team: 'sidebarTeamSection',
        queue: 'sidebarQueueSection',
        process: 'sidebarProcessSection',
        backlog: 'sidebarBacklogSection',
        permissions: 'sidebarPermissionsSection',
    };
    if (sidebarSections[name]) {
        // Scroll to sidebar section + expand if collapsed
        restoreSidebarToolContent();
        const section = document.getElementById(sidebarSections[name]);
        if (section) {
            section.classList.remove('collapsed');
            section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            if (name === 'process') loadSidebarProcesses();
            if (name === 'permissions') loadPermissions();
        }
    } else {
        // Toggle: clicking same tool closes it
        if (sidebarShowingTool === name) {
            restoreSidebarToolContent();
            return;
        }
        openSidebarTool(name);
    }

    // Re-fit terminal if open
    if (fitAddon) requestAnimationFrame(() => fitAddon.fit());
}

/**
 * Close the current tool panel
 */
function closeToolPanel() {
    if (ctx.uiMode !== 'desktop-multipane') return;

    restoreSidebarToolContent();

    // Re-fit terminal
    if (fitAddon) requestAnimationFrame(() => fitAddon.fit());
}

/**
 * Restore reparented drawer tab content back to original parents
 */
function restoreToolContent() {
    for (const [name, info] of toolsPanelOrigParents) {
        const { element, parent, nextSibling } = info;
        if (parent) {
            // Reset classes before returning
            element.classList.add('hidden');
            element.classList.remove('active');
            if (nextSibling) {
                parent.insertBefore(element, nextSibling);
            } else {
                parent.appendChild(element);
            }
        }
    }
    toolsPanelOrigParents.clear();
}

// ===== Desktop Sidebar Functions =====

/**
 * Populate sidebar sessions list (mirrors populateRecentRepos but for sidebar)
 */
function populateSidebarSessions() {
    const body = document.getElementById('sidebarSessionsBody');
    if (!body) return;
    body.innerHTML = '';

    if (!ctx.targets || ctx.targets.length === 0) return;

    // Build team target set to filter out team panes
    const teamTargetIds = new Set();
    if (ctx.teamState?.has_team && ctx.teamState.team) {
        if (ctx.teamState.team.leader) teamTargetIds.add(ctx.teamState.team.leader.target_id);
        for (const a of ctx.teamState.team.agents) teamTargetIds.add(a.target_id);
    }

    const nonTeamTargets = ctx.targets.filter(t => !teamTargetIds.has(t.id));
    nonTeamTargets.forEach(target => {
        const isActive = target.id === ctx.activeTarget;
        const btn = document.createElement('button');
        btn.className = 'sidebar-session-btn' + (isActive ? ' current' : '');
        const label = target.project || target.window_name || target.id;
        btn.textContent = label;
        btn.title = target.cwd || target.id;
        if (!isActive) {
            btn.addEventListener('click', () => selectTarget(target.id));
        }
        body.appendChild(btn);
    });
}

/**
 * Populate desktop sidebar: reparent team cards + queue, build sessions
 */
function updateSidebarTop() {
    const vc = document.getElementById('viewsContainer');
    if (vc) {
        document.documentElement.style.setProperty(
            '--sidebar-top', vc.getBoundingClientRect().top + 'px'
        );
    }
}

function populateDesktopSidebar() {
    const sidebar = document.getElementById('desktopSidebar');
    if (!sidebar) return;

    // Measure where views-container starts (below header + banners)
    updateSidebarTop();

    // Reparent team cards container into sidebar
    const teamCards = document.getElementById('teamCardsContainer');
    const sidebarTeamBody = document.getElementById('sidebarTeamBody');
    if (teamCards && sidebarTeamBody) {
        sidebarOrigParents.set('teamCards', {
            element: teamCards,
            parent: teamCards.parentElement,
            nextSibling: teamCards.nextElementSibling,
        });
        sidebarTeamBody.appendChild(teamCards);
    }

    // Reparent queue tab content into sidebar
    const queueContent = document.getElementById('queueTabContent');
    const sidebarQueueBody = document.getElementById('sidebarQueueBody');
    if (queueContent && sidebarQueueBody) {
        sidebarOrigParents.set('queueContent', {
            element: queueContent,
            parent: queueContent.parentElement,
            nextSibling: queueContent.nextElementSibling,
        });
        sidebarQueueBody.appendChild(queueContent);
        queueContent.classList.remove('hidden');
        queueContent.classList.add('active');
    }

    // Reparent backlog tab content into sidebar
    const backlogContent = document.getElementById('backlogTabContent');
    const sidebarBacklogBody = document.getElementById('sidebarBacklogBody');
    if (backlogContent && sidebarBacklogBody) {
        sidebarOrigParents.set('backlogContent', {
            element: backlogContent,
            parent: backlogContent.parentElement,
            nextSibling: backlogContent.nextElementSibling,
        });
        sidebarBacklogBody.appendChild(backlogContent);
        backlogContent.classList.remove('hidden');
        backlogContent.classList.add('active');
    }

    // Reparent permissions tab content into sidebar
    const permContent = document.getElementById('permissionsTabContent');
    const sidebarPermBody = document.getElementById('sidebarPermissionsBody');
    if (permContent && sidebarPermBody) {
        sidebarOrigParents.set('permContent', {
            element: permContent,
            parent: permContent.parentElement,
            nextSibling: permContent.nextElementSibling,
        });
        sidebarPermBody.appendChild(permContent);
        permContent.classList.remove('hidden');
        permContent.classList.add('active');
    }

    // Build sessions list
    populateSidebarSessions();

    // Show sidebar
    sidebar.classList.remove('hidden');

    // Always show team section — empty state CTA handled by renderTeamCards
    const teamSection = document.getElementById('sidebarTeamSection');
    if (teamSection) {
        teamSection.style.display = '';
    }

    // Update counts
    updateSidebarCounts();
}

/**
 * Restore desktop sidebar: return reparented content to original parents
 */
function restoreDesktopSidebar() {
    // Restore any tool content first
    restoreSidebarToolContent();

    // Restore reparented elements
    for (const [, info] of sidebarOrigParents) {
        const { element, parent, nextSibling } = info;
        if (parent) {
            // Reset queue/backlog classes before returning
            if (element.id === 'queueTabContent' || element.id === 'backlogTabContent') {
                element.classList.add('hidden');
                element.classList.remove('active');
            }
            if (nextSibling) {
                parent.insertBefore(element, nextSibling);
            } else {
                parent.appendChild(element);
            }
        }
    }
    sidebarOrigParents.clear();

    // Hide sidebar
    const sidebar = document.getElementById('desktopSidebar');
    if (sidebar) sidebar.classList.add('hidden');
}

/**
 * Open a tool in the sidebar (for secondary tools: history, process, runner, dev, mcp, env)
 */
function openSidebarTool(name) {
    if (sidebarShowingTool === name) {
        restoreSidebarToolContent();
        return;
    }

    // Restore previous tool content if any
    restoreToolContent();

    const contentId = TOOL_TAB_MAP[name];
    const sourceEl = contentId ? document.getElementById(contentId) : null;
    const sidebarToolEl = document.getElementById('sidebarToolContent');
    const sidebarDefault = document.getElementById('sidebarDefaultContent');

    if (sourceEl && sidebarToolEl) {
        // Save original parent for restore
        toolsPanelOrigParents.set(name, {
            element: sourceEl,
            parent: sourceEl.parentElement,
            nextSibling: sourceEl.nextElementSibling,
        });
        sidebarToolEl.innerHTML = '';
        sidebarToolEl.appendChild(sourceEl);
        sourceEl.classList.remove('hidden');
        sourceEl.classList.add('active');
    }

    // Hide default content, show tool content
    if (sidebarDefault) sidebarDefault.classList.add('hidden');
    if (sidebarToolEl) sidebarToolEl.classList.remove('hidden');

    // Show back button + tool title in sidebar header
    const backBtn = document.getElementById('sidebarBackBtn');
    const toolTitle = document.getElementById('sidebarToolTitle');
    const mainTitle = document.querySelector('.desktop-sidebar-title');
    if (backBtn) backBtn.classList.remove('hidden');
    if (toolTitle) {
        toolTitle.textContent = TOOL_TITLES[name] || name;
        toolTitle.classList.remove('hidden');
    }
    if (mainTitle) mainTitle.classList.add('hidden');

    // Update rail active state
    document.querySelectorAll('.tools-rail-btn[data-tool]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tool === name);
    });

    sidebarShowingTool = name;
    activeToolPanel = name;
    localStorage.setItem('mto_desktop_tool', name);

    // Trigger tab-specific load
    switchRollbackTab(name);
}

/**
 * Restore sidebar tool content back to default view
 */
function restoreSidebarToolContent() {
    // Stop activity polling if leaving that tab
    stopActivity();
    // Return reparented elements to drawer
    restoreToolContent();

    const sidebarDefault = document.getElementById('sidebarDefaultContent');
    const sidebarToolEl = document.getElementById('sidebarToolContent');
    if (sidebarDefault) sidebarDefault.classList.remove('hidden');
    if (sidebarToolEl) {
        sidebarToolEl.classList.add('hidden');
        sidebarToolEl.innerHTML = '';
    }

    // Reset header
    const backBtn = document.getElementById('sidebarBackBtn');
    const toolTitle = document.getElementById('sidebarToolTitle');
    const mainTitle = document.querySelector('.desktop-sidebar-title');
    if (backBtn) backBtn.classList.add('hidden');
    if (toolTitle) toolTitle.classList.add('hidden');
    if (mainTitle) mainTitle.classList.remove('hidden');

    // Clear rail active state
    document.querySelectorAll('.tools-rail-btn[data-tool]').forEach(btn => {
        btn.classList.remove('active');
    });

    sidebarShowingTool = null;
    activeToolPanel = null;
    localStorage.removeItem('mto_desktop_tool');
}

/**
 * Update sidebar count badges for team and queue
 */
function updateSidebarCounts() {
    const teamCountEl = document.getElementById('sidebarTeamCount');
    if (teamCountEl) {
        const teamVisible = isTeamInCurrentRepo();
        const agents = teamVisible ? ctx.teamState?.team?.agents : null;
        const count = agents ? agents.length : 0;
        teamCountEl.textContent = count.toString();
        teamCountEl.classList.toggle('hidden', count === 0);
    }

    const queueCountEl = document.getElementById('sidebarQueueCount');
    if (queueCountEl) {
        const items = getQueueItems();
        const queuedCount = items.filter(i => i.status === 'queued').length;
        queueCountEl.textContent = queuedCount.toString();
        queueCountEl.classList.toggle('hidden', queuedCount === 0);

        // Auto-collapse queue section when empty, expand when items arrive
        const queueSection = document.getElementById('sidebarQueueSection');
        if (queueSection) {
            queueSection.classList.toggle('collapsed', queuedCount === 0);
        }
    }

    // Always show team section — empty state CTA handled by renderTeamCards
    const teamSection = document.getElementById('sidebarTeamSection');
    if (teamSection) {
        teamSection.style.display = '';
    }

    // Process count — driven by descendant_count from health poll
    const procCountEl = document.getElementById('sidebarProcessCount');
    const procSection = document.getElementById('sidebarProcessSection');
    if (procCountEl && lastPhase) {
        const dCount = lastPhase.descendant_count || 0;
        procCountEl.textContent = dCount.toString();
        procCountEl.classList.toggle('hidden', dCount === 0);
        if (procSection) {
            procSection.classList.toggle('collapsed', dCount === 0);
        }
    }
}

/**
 * Setup desktop layout detection and resize handler
 */
function setupDesktopLayout() {
    checkDesktopLayout();
    window.addEventListener('resize', () => {
        clearTimeout(desktopResizeTimer);
        desktopResizeTimer = setTimeout(checkDesktopLayout, 250);
    });

    // Pane focus via click
    const teamViewEl = document.getElementById('teamView');
    const logViewEl = document.getElementById('logView');
    if (teamViewEl) {
        teamViewEl.addEventListener('click', () => {
            if (ctx.uiMode === 'desktop-multipane') switchDesktopFocus('team');
        });
    }
    if (logViewEl) {
        logViewEl.addEventListener('click', () => {
            if (ctx.uiMode === 'desktop-multipane') switchDesktopFocus('log');
        });
    }

    // Terminal close button
    const closeBtn = document.getElementById('terminalCloseBtn');
    if (closeBtn) {
        closeBtn.addEventListener('click', () => {
            if (ctx.uiMode === 'desktop-multipane') closeDesktopTerminal();
        });
    }

    // Tools rail: tool buttons
    document.querySelectorAll('.tools-rail-btn[data-tool]').forEach(btn => {
        btn.addEventListener('click', () => {
            if (ctx.uiMode === 'desktop-multipane') openToolPanel(btn.dataset.tool);
        });
    });

    // Tools rail: action buttons
    document.querySelectorAll('.tools-rail-btn[data-action]').forEach(btn => {
        btn.addEventListener('click', () => {
            if (ctx.uiMode !== 'desktop-multipane') return;
            const action = btn.dataset.action;
            if (action === 'terminal') {
                if (document.querySelector('.app')?.classList.contains('desktop-terminal-open')) {
                    closeDesktopTerminal();
                } else {
                    openDesktopTerminal();
                }
            }
            else if (action === 'select' && selectCopyBtn) selectCopyBtn.click();
            else if (action === 'stop') {
                sendStopInterrupt();
                showToast('Interrupt sent', 'success');
            }
            else if (action === 'compose' && composeBtn) composeBtn.click();
        });
    });

    // Tools content close button
    document.getElementById('toolsContentClose')?.addEventListener('click', () => {
        closeToolPanel();
    });

    // Team filters (density toggle handled by initTeam)
    setupTeamFilters();

    // Sidebar section collapse/expand toggles
    document.querySelectorAll('.sidebar-section-toggle').forEach(btn => {
        btn.addEventListener('click', () => {
            const section = btn.closest('.sidebar-section');
            if (section) {
                section.classList.toggle('collapsed');
                // Load processes when expanding the Processes section
                if (!section.classList.contains('collapsed') && section.id === 'sidebarProcessSection') {
                    loadSidebarProcesses();
                }
            }
        });
    });

    // Sidebar back button
    document.getElementById('sidebarBackBtn')?.addEventListener('click', () => {
        restoreSidebarToolContent();
    });

    // Keyboard shortcuts
    setupDesktopShortcuts();
}

/**
 * Check if we should be in desktop or mobile mode
 */
function checkDesktopLayout() {
    const shouldBeDesktop = window.innerWidth >= DESKTOP_BREAKPOINT;
    const wasDesktop = ctx.uiMode === 'desktop-multipane';

    if (shouldBeDesktop && !wasDesktop) {
        enterDesktopLayout();
    } else if (!shouldBeDesktop && wasDesktop) {
        exitDesktopLayout();
    }
    // Keep sidebar top in sync with header/banner changes
    if (ctx.uiMode === 'desktop-multipane') updateSidebarTop();
}

/**
 * Enter desktop multi-pane mode
 */
function enterDesktopLayout() {
    ctx.uiMode = 'desktop-multipane';
    const app = document.querySelector('.app');
    if (app) app.classList.add('desktop-multipane');

    // Show tools panel + log
    const toolsPanel = document.getElementById('toolsPanel');
    if (toolsPanel) toolsPanel.classList.remove('hidden');
    if (logView) logView.classList.remove('hidden');

    // Start refresh timers
    startLogAutoRefresh();
    startTailViewport();
    startTeamCardRefresh();
    refreshTeamCards();

    // Load log if needed
    if (!logLoaded) loadLogContent();

    // Populate sidebar and apply compact density
    populateDesktopSidebar();
    applyDensity('compact');

    // Restore saved secondary tool in sidebar
    const savedTool = localStorage.getItem('mto_desktop_tool');
    if (savedTool && savedTool !== 'team' && savedTool !== 'queue') {
        openSidebarTool(savedTool);
    }

    // Terminal stays in tail mode by default on desktop.
    // User can press 3 to open full xterm panel when needed.

    console.debug('[Desktop] Entered multi-pane layout');
}

/**
 * Exit desktop mode, restore mobile single-view
 */
function exitDesktopLayout() {
    ctx.uiMode = 'mobile-single';
    const app = document.querySelector('.app');

    // Restore sidebar reparented content + tool content
    restoreDesktopSidebar();
    restoreToolContent();
    activeToolPanel = null;

    // Hide tools panel
    const toolsPanel = document.getElementById('toolsPanel');
    if (toolsPanel) toolsPanel.classList.add('hidden');

    // Hide team view (mobile shows it via switchToView)
    const teamViewEl = document.getElementById('teamView');
    if (teamViewEl) teamViewEl.classList.add('hidden');

    if (app) {
        app.classList.remove('desktop-multipane', 'desktop-terminal-open');
        app.classList.remove('density-comfortable', 'density-compact', 'density-ultra');
    }

    // Close terminal column
    if (terminalView) terminalView.classList.remove('desktop-panel');

    // Remove pane focus indicators
    document.querySelectorAll('.pane-focused').forEach(el => el.classList.remove('pane-focused'));

    // Restore mobile view
    switchToView(ctx.currentView === 'terminal' ? 'terminal' : ctx.currentView === 'team' ? 'team' : 'log');

    console.debug('[Desktop] Exited to mobile layout');
}

/**
 * Switch focus between panes in desktop mode (team/log)
 */
function switchDesktopFocus(viewName) {
    desktopFocusedPane = viewName;
    ctx.currentView = viewName; // Keep ctx.currentView in sync for API compat

    // Update pane focus indicators
    const teamViewEl = document.getElementById('teamView');
    const logViewEl = document.getElementById('logView');

    document.querySelectorAll('.pane-focused').forEach(el => el.classList.remove('pane-focused'));
    if (viewName === 'team' && teamViewEl) teamViewEl.classList.add('pane-focused');
    if (viewName === 'log' && logViewEl) logViewEl.classList.add('pane-focused');
    if (viewName === 'terminal' && terminalView) terminalView.classList.add('pane-focused');
}

/**
 * Open terminal as bottom dock panel (desktop)
 */
function openDesktopTerminal() {
    const app = document.querySelector('.app');
    if (!app || !terminalView) return;

    app.classList.add('desktop-terminal-open');
    terminalView.classList.add('desktop-panel');
    terminalView.classList.remove('hidden');
    switchDesktopFocus('terminal');

    // Mark rail button active
    const termBtn = document.querySelector('.tools-rail-btn[data-action="terminal"]');
    if (termBtn) termBtn.classList.add('active');

    updateTerminalAgentSelector();

    // Fit xterm to panel and switch to full mode
    requestAnimationFrame(() => {
        if (fitAddon) fitAddon.fit();
        sendResize();
        setOutputMode('full');
        if (ctx.terminal) ctx.terminal.focus();
    });

    // Setup vertical resize handle
    setupDesktopTerminalResize();
}

/**
 * Close terminal bottom dock panel (desktop)
 */
function closeDesktopTerminal() {
    const app = document.querySelector('.app');
    if (!app || !terminalView) return;

    app.classList.remove('desktop-terminal-open');
    terminalView.classList.remove('desktop-panel');
    terminalView.classList.add('hidden');

    // Clear rail button active state
    const termBtn = document.querySelector('.tools-rail-btn[data-action="terminal"]');
    if (termBtn) termBtn.classList.remove('active');

    // Switch back to tail mode
    setOutputMode('tail');

    // Restore active prompt
    if (activePromptContent) activePromptContent.style.display = '';

    // Return focus to log
    switchDesktopFocus('log');
}

/**
 * Setup vertical resize handle for desktop terminal panel
 */
let desktopTerminalResizeCleanup = null;
function setupDesktopTerminalResize() {
    // Clean up previous handler
    if (desktopTerminalResizeCleanup) desktopTerminalResizeCleanup();

    const container = document.getElementById('viewsContainer');
    if (!container || !terminalView) return;

    let dragging = false;
    let startY = 0;
    let startHeight = 0;
    const MIN_HEIGHT = 120;
    const MAX_HEIGHT = Math.round(window.innerHeight * 0.7);

    function onPointerDown(e) {
        // Only start drag from the border area (top 8px of terminal panel)
        const termRect = terminalView.getBoundingClientRect();
        if (Math.abs(e.clientY - termRect.top) > 8) return;

        dragging = true;
        startY = e.clientY;
        startHeight = terminalView.offsetHeight;
        document.body.style.cursor = 'ns-resize';
        document.body.style.userSelect = 'none';
        e.preventDefault();
    }

    function onPointerMove(e) {
        if (!dragging) return;
        const delta = startY - e.clientY;
        const newHeight = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, startHeight + delta));
        container.style.setProperty('--terminal-panel-height', newHeight + 'px');
        if (fitAddon) requestAnimationFrame(() => fitAddon.fit());
    }

    function onPointerUp() {
        if (!dragging) return;
        dragging = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        const h = terminalView.offsetHeight;
        localStorage.setItem('mto_desktop_terminal_height', h.toString());
        sendResize();
    }

    terminalView.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('pointermove', onPointerMove);
    document.addEventListener('pointerup', onPointerUp);

    // Restore saved height
    const saved = parseInt(localStorage.getItem('mto_desktop_terminal_height'));
    if (saved && saved >= MIN_HEIGHT && saved <= MAX_HEIGHT) {
        container.style.setProperty('--terminal-panel-height', saved + 'px');
    }

    desktopTerminalResizeCleanup = () => {
        terminalView.removeEventListener('pointerdown', onPointerDown);
        document.removeEventListener('pointermove', onPointerMove);
        document.removeEventListener('pointerup', onPointerUp);
        desktopTerminalResizeCleanup = null;
    };
}


/**
 * Desktop keyboard shortcuts
 */
function setupDesktopShortcuts() {
    // Terminal control keys: work even when input is focused
    document.addEventListener('keydown', (e) => {
        if (ctx.uiMode !== 'desktop-multipane') return;

        // Skip if compose/challenge/palette modal is open
        if (document.querySelector('.compose-modal:not(.hidden)') ||
            document.querySelector('.challenge-modal:not(.hidden)') ||
            document.querySelector('.palette-overlay:not(.hidden)')) return;

        // Ctrl+B → tmux prefix
        if (e.ctrlKey && e.key === 'b') {
            e.preventDefault();
            sendKeyDebounced('\x02');
            return;
        }

        // Only intercept remaining keys when logInput is focused
        const isLogInput = document.activeElement?.id === 'logInput';
        if (!isLogInput) return;

        // Escape → cancel cycle preview or blur input
        if (e.key === 'Escape') {
            e.preventDefault();
            if (_cyclePreviewIdx >= 0) {
                cancelCyclePreview();
            } else {
                document.activeElement.blur();
                if (document.querySelector('.app')?.classList.contains('desktop-terminal-open')) {
                    closeDesktopTerminal();
                }
            }
            return;
        }

        // Enter → confirm cycle preview if active, otherwise normal behavior
        if (e.key === 'Enter' && _cyclePreviewIdx >= 0) {
            e.preventDefault();
            confirmCyclePreview();
            return;
        }

        // Ctrl+C → send interrupt when input is empty
        if (e.ctrlKey && e.key === 'c') {
            if (logInput && !logInput.value.trim()) {
                e.preventDefault();
                sendStopInterrupt();
            }
            return;
        }

        // Shift+Tab → cycle session/pane preview
        if (e.shiftKey && e.key === 'Tab') {
            e.preventDefault();
            cycleTargetPreview();
            return;
        }

        // Tab → send to terminal (completion)
        if (e.key === 'Tab') {
            e.preventDefault();
            sendKeyWithSync('\t', 200);
            return;
        }
    });

    document.addEventListener('keydown', (e) => {
        if (ctx.uiMode !== 'desktop-multipane') return;

        // Skip if input/textarea/select focused
        const tag = document.activeElement?.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

        // Skip if any modal visible
        if (document.querySelector('.compose-modal:not(.hidden)') ||
            document.querySelector('.challenge-modal:not(.hidden)') ||
            document.querySelector('.docs-modal:not(.hidden)') ||
            document.querySelector('.palette-overlay:not(.hidden)') ||
            document.querySelector('.shortcut-help-modal.visible')) return;

        // Skip if interactive mode
        if (typeof interactiveMode !== 'undefined' && interactiveMode) return;

        const key = e.key;

        switch (key) {
            case '1':
                e.preventDefault();
                openToolPanel('team');
                break;
            case '2':
                e.preventDefault();
                switchDesktopFocus('log');
                break;
            case '3':
                e.preventDefault();
                if (document.querySelector('.app')?.classList.contains('desktop-terminal-open')) {
                    closeDesktopTerminal();
                } else {
                    openDesktopTerminal();
                }
                break;
            case 'j':
                e.preventDefault();
                selectNextAgent();
                break;
            case 'k':
                e.preventDefault();
                selectPrevAgent();
                break;
            case 'a':
                e.preventDefault();
                approveSelectedAgent();
                break;
            case 'd':
                e.preventDefault();
                denySelectedAgent();
                break;
            case 'Enter':
                e.preventDefault();
                openSelectedAgentTerminal();
                break;
            case 't':
                e.preventDefault();
                openToolPanel('team');
                break;
            case '/':
                e.preventDefault();
                focusSearchInput();
                break;
            case 'Escape':
                if (document.querySelector('.app')?.classList.contains('desktop-terminal-open') && desktopFocusedPane === 'terminal') {
                    e.preventDefault();
                    closeDesktopTerminal();
                } else if (document.querySelector('.shortcut-help-modal.visible')) {
                    document.querySelector('.shortcut-help-modal.visible').classList.remove('visible');
                }
                break;
            case '?':
                if (e.shiftKey) {
                    e.preventDefault();
                    toggleShortcutHelp();
                }
                break;
        }
    });
}


/**
 * Shortcut help modal
 */
function toggleShortcutHelp() {
    let modal = document.querySelector('.shortcut-help-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.className = 'shortcut-help-modal';
        modal.innerHTML = `
            <div class="shortcut-help-content">
                <div class="shortcut-help-title">
                    <span>Keyboard Shortcuts</span>
                    <button class="shortcut-help-close">&times;</button>
                </div>
                <div class="shortcut-group">
                    <div class="shortcut-group-title">Navigation</div>
                    <div class="shortcut-row"><span>Focus team</span><span class="shortcut-key">1</span></div>
                    <div class="shortcut-row"><span>Focus log</span><span class="shortcut-key">2</span></div>
                    <div class="shortcut-row"><span>Toggle terminal</span><span class="shortcut-key">3</span></div>
                    <div class="shortcut-row"><span>Search agents</span><span class="shortcut-key">/</span></div>
                </div>
                <div class="shortcut-group">
                    <div class="shortcut-group-title">Agent Selection</div>
                    <div class="shortcut-row"><span>Next agent</span><span class="shortcut-key">j</span></div>
                    <div class="shortcut-row"><span>Previous agent</span><span class="shortcut-key">k</span></div>
                    <div class="shortcut-row"><span>Approve</span><span class="shortcut-key">a</span></div>
                    <div class="shortcut-row"><span>Deny</span><span class="shortcut-key">d</span></div>
                    <div class="shortcut-row"><span>Open terminal</span><span class="shortcut-key">Enter</span></div>
                </div>
                <div class="shortcut-group">
                    <div class="shortcut-group-title">Other</div>
                    <div class="shortcut-row"><span>Close terminal</span><span class="shortcut-key">Esc</span></div>
                    <div class="shortcut-row"><span>This help</span><span class="shortcut-key">Shift+?</span></div>
                </div>
            </div>`;
        document.body.appendChild(modal);
        modal.querySelector('.shortcut-help-close').addEventListener('click', () => {
            modal.classList.remove('visible');
        });
        modal.addEventListener('click', (e) => {
            if (e.target === modal) modal.classList.remove('visible');
        });
    }
    modal.classList.toggle('visible');
}



// Initialize
document.addEventListener('DOMContentLoaded', async () => {
    initDOMElements();

    // Restore high-contrast mode
    if (localStorage.getItem('mto_high_contrast') === '1') {
        document.documentElement.classList.add('high-contrast');
    }

    // Global Escape key → send ESC to terminal (unless a modal/compose is open)
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        // Don't intercept if compose modal or other modals are open
        const composeEl = document.getElementById('composeModal');
        if (composeEl && !composeEl.classList.contains('hidden')) return;
        const newWindowEl = document.getElementById('newWindowModal');
        if (newWindowEl && !newWindowEl.classList.contains('hidden')) return;
        // Don't intercept if focus is in an input/textarea (handled by their own keydown)
        const tag = document.activeElement?.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        // Send ESC to terminal
        e.preventDefault();
        sendTextAtomic('\x1b', false);
    });

    // Configure marked.js to not convert single newlines to <br>
    // This prevents garbled output when ctx.terminal content has hard line breaks
    if (typeof marked !== 'undefined') {
        marked.setOptions({
            breaks: false,  // Don't convert \n to <br>
            gfm: true,      // Keep GitHub Flavored Markdown for other features
        });
    }

    // Initialize ctx.terminal (but it starts hidden in ctx.terminal tab)
    initTerminal();

    // Hide control bars initially (view mode)
    controlBarsContainer.classList.add('hidden');

    setupEventListeners();
    initMcp({
        onAgentRestarted: () => { agentStartedAt = Date.now(); lastAgentHealth = null; },
    });
    initEnv();
    setupTerminalFocus();
    setupViewportHandler();
    setupClipboard();
    setupRepoDropdown();
    setupTargetSelector();  // Non-blocking - applies saved target locally, syncs in background
    setupNewWindowModal();
    setupJumpToBottom();
    setupMetricsWidget();
    document.getElementById('processesPill')?.addEventListener('click', () => openSurface('process'));
    setupTerminalSearch();
    setupCopyButton();
    setupCommandHistory();
    setupComposeMode();
    setupChallenge();
    setupViewToggle();
    setupSwipeNavigation();
    setupTailResize();
    setupViewSwitcher();
    updateViewSwitcher();  // Set initial view switcher state
    startActivityUpdates();
    initQueue();
    initBacklog('');
    initPermissions();
    document.getElementById('permissionsTestBtn')?.addEventListener('click', () => {
        handlePermissionRequest({
            id: 'test-' + Date.now(),
            tool: 'Bash',
            target: 'pytest tests/ -q',
            repo: '/home/gcbbuilder/dev/mobile-terminal-overlay',
            risk: 'low',
        });
    });
    document.getElementById('permissionsTestPromptBtn')?.addEventListener('click', () => {
        // Simulate real Claude Code TUI format (tool name in box header)
        const fakeTerminal = [
            '╭─ Bash ────────────────────────────────╮',
            '│  git commit -m "Fix bug"              │',
            '│                                       │',
            '│  Allow this action?                   │',
            '│  ❯ 1. Yes                             │',
            '│    2. Yes, and don\'t ask again        │',
            '│    3. Reject                          │',
            '╰───────────────────────────────────────╯',
        ].join('\n');
        extractPermissionPrompt(fakeTerminal);
    });
    initCollapse(logContent);
    setupSelectionBacklog(logContent);
    setupScrollTracking();
    setupLogFilterBar();
    initMarkdown(logContent);
    initToolOutput(logContent);
    initDocs();
    initPalette({
        switchToView, openSurface, executeRunnerCommand,
        respawnProcess, showLaunchTeamModal,
        sendInterrupt: () => sendStopInterrupt(),
    });
    initActivity();
    initHistory({ captureSnapshot, enterPreviewMode });
    initTeam({ selectTarget, switchToView, fetchWithTimeout, updateActionBar });
    setupPreviewHandlers();
    setupRunnerHandlers();
    setupDevPreview();
    setupPermissionBanner();
    setupDesktopLayout();

    // Command palette: Ctrl+K / Cmd+K (works in all UI modes)
    document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
            e.preventDefault();
            const paletteEl = document.getElementById('paletteOverlay');
            if (paletteEl && !paletteEl.classList.contains('hidden')) {
                closePalette();
            } else {
                openPalette();
            }
        }
    });

    // Palette button (desktop header)
    document.getElementById('paletteBtn')?.addEventListener('click', () => openPalette());

    // Reconnect/refresh button click handlers
    const reconnectBadge = document.getElementById('reconnectBadge');
    if (reconnectBadge) {
        reconnectBadge.addEventListener('click', () => {
            if (typeof manualReconnect === 'function') manualReconnect();
        });
    }
    const reconnectBtn = document.getElementById('reconnectBtn');
    reconnectBtn?.addEventListener('click', manualReconnect);
    const hardRefreshBtn = document.getElementById('hardRefreshBtn');
    hardRefreshBtn?.addEventListener('click', hardRefresh);

    // Dispatch bar — initialized via initTeam() in DOMContentLoaded

    // Context banner: tap header to expand, × to dismiss
    const ctxHeader = document.getElementById('contextBannerHeader');
    const ctxDismiss = document.getElementById('contextBannerDismiss');
    if (ctxHeader) {
        ctxHeader.addEventListener('click', () => {
            const body = document.getElementById('contextBannerBody');
            if (body) body.classList.toggle('hidden');
        });
    }
    if (ctxDismiss) {
        ctxDismiss.addEventListener('click', (e) => {
            e.stopPropagation();
            const banner = document.getElementById('contextBanner');
            if (banner) banner.classList.add('hidden');
            const dismissKey = `mto_context_dismissed_${ctx.currentSession || ''}`;
            sessionStorage.setItem(dismissKey, '1');
        });
    }

    // Scroll input bar to the right so Enter button is visible
    if (inputBar) {
        inputBar.scrollLeft = inputBar.scrollWidth;
    }

    // CRITICAL: Connect IMMEDIATELY - don't block on any API calls
    // WebSocket connection is independent of ctx.config/session/queue
    // SSE fallback: if previous session found WS broken, start with SSE
    if (_preferSSE) { connectSSE(); } else { connect(); }

    // Start with log view as primary (desktop layout already handled in setupDesktopLayout)
    if (ctx.uiMode !== 'desktop-multipane') {
        switchToLogView();
    }

    // Background init: Load session, ctx.config, queue in parallel (non-blocking)
    // These enhance the UI but are not required for basic ctx.terminal operation
    Promise.all([
        loadCurrentSession().catch(e => console.warn('loadCurrentSession failed:', e)),
        loadConfig().catch(e => console.warn('loadConfig failed:', e)),
    ]).then(() => {
        // Reconcile queue after session is known (needs ctx.currentSession)
        reconcileQueue().catch(e => console.warn('reconcileQueue failed:', e));
        // Fire-and-forget: context banner load must never block
        loadContextBanner().catch(() => {});
        // Fire-and-forget: push notification setup
        setupPushNotifications().catch(() => {});

        // Handle URL action params (from push notification deep links)
        const urlParams = new URLSearchParams(window.location.search);
        const deepAction = urlParams.get('action');
        if (deepAction === 'respawn') {
            // Delay respawn until connection is established
            setTimeout(() => {
                respawnAgent();
                const cleanUrl = window.location.pathname + (ctx.token ? `` : '');
                window.history.replaceState({}, '', cleanUrl);
            }, 2000);
        } else if (urlParams.get('share')) {
            // Web Share Target: retrieve shared content and open compose
            const shareId = urlParams.get('share');
            setTimeout(async () => {
                try {
                    const resp = await apiFetch('/api/share/pending?share_id=' + encodeURIComponent(shareId) + '&token=' + ctx.token);
                    const data = await resp.json();
                    if (data.found && data.text) {
                        prefillCompose(data.text);
                        showToast('Shared content loaded', 'success');
                    }
                } catch (_) {}
                const cleanUrl = window.location.pathname + (ctx.token ? '' : '');
                window.history.replaceState({}, '', cleanUrl);
            }, 1000);
        } else if (deepAction === 'allow' || deepAction === 'deny') {
            // Permission response from push notification when no client was open
            const choice = deepAction === 'allow' ? 'y' : 'n';
            const paneId = urlParams.get('pane_id') || '';
            // Delay until WebSocket connection is established
            setTimeout(() => {
                if (ctx.socket && ctx.socket.readyState === WebSocket.OPEN) {
                    sendTextAtomic(choice, true);
                    showToast(`Sent ${deepAction} to ${paneId || 'agent'}`, 'success');
                } else {
                    showToast(`Could not send ${deepAction} — not connected`, 'error');
                }
                const cleanUrl = window.location.pathname + (ctx.token ? `` : '');
                window.history.replaceState({}, '', cleanUrl);
            }, 2000);
        }
    });
});

// Register service worker for PWA standalone mode
if ('serviceWorker' in navigator) {
    const _bp = window.__BASE_PATH || '';
    const correctScope = _bp + '/';

    // Unregister stale SWs from wrong scopes (e.g. old '/' after switching to '/terminal')
    navigator.serviceWorker.getRegistrations().then(regs => {
        for (const reg of regs) {
            if (new URL(reg.scope).pathname !== correctScope) {
                console.log('Unregistering stale SW at scope:', reg.scope);
                reg.unregister();
            }
        }
    });

    navigator.serviceWorker.register(_bp + '/sw.js?v=348', { scope: correctScope })
        .catch(err => console.log('SW registration failed:', err));
}
