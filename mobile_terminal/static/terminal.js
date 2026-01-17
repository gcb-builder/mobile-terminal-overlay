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
let isControlUnlocked = false;
let config = null;
let currentSession = null;

// Reconnection with exponential backoff
let reconnectDelay = 500;
const MAX_RECONNECT_DELAY = 30000;
const INITIAL_RECONNECT_DELAY = 500;  // Fast initial reconnect for mobile
const MIN_CONNECTION_INTERVAL = 500;  // Minimum ms between connection attempts
let intentionalClose = false;  // Track intentional closes to skip auto-reconnect
let isConnecting = false;  // Prevent concurrent connection attempts
let reconnectTimer = null;  // Track pending reconnect
let lastConnectionAttempt = 0;  // Timestamp of last connection attempt

// Heartbeat for connection health monitoring
const HEARTBEAT_INTERVAL = 30000;  // Send ping every 30s
const HEARTBEAT_TIMEOUT = 10000;   // Expect pong within 10s
let heartbeatTimer = null;
let heartbeatTimeoutTimer = null;
let lastPongTime = 0;

// Local command history (persisted to localStorage)
const MAX_HISTORY_SIZE = 100;
let commandHistory = JSON.parse(localStorage.getItem('terminalHistory') || '[]');
let historyIndex = -1;
let currentInput = '';

// DOM elements (initialized in DOMContentLoaded)
let terminalContainer, controlBtn, controlBarsContainer;
let collapseToggle, controlBar, roleBar, inputBar, viewBar;
let statusOverlay, statusText, repoBtn, repoLabel, repoDropdown;
let searchBtn, searchModal, searchInput, searchClose, searchResults;
let composeBtn, composeModal;
let composeInput, composeClose, composeClear, composeInsert, composeRun;
let composeCamera, composeGallery, composeCameraInput, composeGalleryInput, composeAttachments;
let copyBtn, selectModeBtn, stopBtn;
let terminalViewBtn, transcriptViewBtn, transcriptContainer, transcriptContent, transcriptSearch, transcriptSearchCount;
let contextViewBtn, touchViewBtn, contextContainer, contextContent, touchContainer, touchContent;

// Attachments state for compose modal
let pendingAttachments = [];

function initDOMElements() {
    terminalContainer = document.getElementById('terminal-container');
    controlBtn = document.getElementById('controlBtn');
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
    composeInsert = document.getElementById('composeInsert');
    composeRun = document.getElementById('composeRun');
    composeCamera = document.getElementById('composeCamera');
    composeGallery = document.getElementById('composeGallery');
    composeCameraInput = document.getElementById('composeCameraInput');
    composeGalleryInput = document.getElementById('composeGalleryInput');
    composeAttachments = document.getElementById('composeAttachments');
    copyBtn = document.getElementById('copyBtn');
    selectModeBtn = document.getElementById('selectModeBtn');
    stopBtn = document.getElementById('stopBtn');
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
}

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
        if (isControlUnlocked && socket && socket.readyState === WebSocket.OPEN) {
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
}

/**
 * Handle pong response from server
 */
function handlePong() {
    lastPongTime = Date.now();
    if (heartbeatTimeoutTimer) {
        clearTimeout(heartbeatTimeoutTimer);
        heartbeatTimeoutTimer = null;
    }
    updateConnectionIndicator('connected');
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
        statusOverlay.classList.add('hidden');
        // Reset reconnect delay on successful connection
        reconnectDelay = INITIAL_RECONNECT_DELAY;

        // Fit terminal to container (don't clear buffer - server will replay history)
        if (terminal && fitAddon) {
            fitAddon.fit();
        }

        sendResize();
        startHeartbeat();
        updateConnectionIndicator('connected');
    };

    socket.onmessage = (event) => {
        if (event.data instanceof Blob) {
            event.data.arrayBuffer().then((buffer) => {
                terminal.write(new Uint8Array(buffer));
            });
        } else {
            // Check for JSON messages (pong, etc.)
            if (event.data.startsWith('{')) {
                try {
                    const msg = JSON.parse(event.data);
                    if (msg.type === 'pong') {
                        handlePong();
                        return;
                    }
                } catch (e) {
                    // Not JSON, treat as terminal data
                }
            }
            terminal.write(event.data);
        }
    };

    socket.onclose = (event) => {
        console.log('WebSocket closed:', event.code, event.reason);
        isConnecting = false;
        stopHeartbeat();
        updateConnectionIndicator('disconnected');

        // Skip auto-reconnect for:
        // - intentionalClose flag (client-initiated)
        // - code 4002 (replaced by another connection)
        // - code 4003 (repo switch in progress)
        if (intentionalClose || event.code === 4002 || event.code === 4003) {
            intentionalClose = false;
            return;
        }

        // Rate limited (4004) - wait longer before retry
        if (event.code === 4004) {
            console.log('Rate limited by server, waiting before retry');
            reconnectDelay = Math.max(reconnectDelay, 2000);
        }

        statusText.textContent = `Disconnected. Reconnecting in ${reconnectDelay / 1000}s...`;
        statusOverlay.classList.remove('hidden');

        // Show reconnect button
        const reconnectBtn = document.getElementById('reconnectBtn');
        if (reconnectBtn) reconnectBtn.classList.remove('hidden');

        // Reconnect with exponential backoff
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
 * Send input to terminal
 */
function sendInput(data) {
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            type: 'input',
            data: data,
        }));
    }
}

/**
 * Toggle control lock
 */
function toggleControl() {
    isControlUnlocked = !isControlUnlocked;

    if (isControlUnlocked) {
        controlBtn.classList.remove('locked');
        controlBtn.classList.add('unlocked');
        controlBtn.querySelector('.lock-icon').innerHTML = '&#x1F513;';

        controlBarsContainer.classList.remove('hidden');
        controlBarsContainer.classList.remove('collapsed');
        collapseToggle.classList.remove('hidden');
        collapseToggle.classList.remove('collapsed');

        // Focus terminal for direct input
        terminal.focus();
        terminalContainer.classList.add('focusable');

        // Don't resize - keeps terminal stable, prevents tmux reflow/corruption
    } else {
        controlBtn.classList.remove('unlocked');
        controlBtn.classList.add('locked');
        controlBtn.querySelector('.lock-icon').innerHTML = '&#x1F512;';

        controlBarsContainer.classList.add('hidden');
        collapseToggle.classList.add('hidden');
        terminalContainer.classList.remove('focusable');

        // Clear any selection when locking
        terminal.clearSelection();

        // Don't resize - keeps terminal stable, prevents tmux reflow/corruption
    }
}

/**
 * Toggle control bars collapse state
 */
function toggleControlBarsCollapse() {
    if (!controlBarsContainer || !collapseToggle) return;

    const isCollapsed = controlBarsContainer.classList.toggle('collapsed');
    // Update button icon state
    collapseToggle.classList.toggle('collapsed', isCollapsed);

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
                    sendInput(role.insert);
                    terminal.focus();
                }
            });
            roleBar.appendChild(btn);
        });
    }

    // Populate repo dropdown
    populateRepoDropdown();
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

        // Update UI
        const currentRepo = config.repos.find(r => r.session === session);
        if (currentRepo) {
            repoLabel.textContent = currentRepo.label;
        } else {
            repoLabel.textContent = session;
        }

        // Server already closed WebSocket, reconnect after cleanup delay
        setTimeout(connect, 1000);

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
    // Control toggle button
    controlBtn.addEventListener('click', toggleControl);

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
            if (isControlUnlocked) {
                const keyName = btn.dataset.key;
                const key = keyMap[keyName] || keyName;
                sendInput(key);
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
            // If disconnected, reconnect immediately instead of waiting for backoff
            if (!socket || socket.readyState !== WebSocket.OPEN) {
                console.log('Page visible, reconnecting immediately');
                if (reconnectTimer) {
                    clearTimeout(reconnectTimer);
                    reconnectTimer = null;
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
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
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

    // Auto-scroll on new output (only if already at bottom)
    const originalWrite = terminal.write.bind(terminal);
    terminal.write = (data) => {
        const wasAtBottom = isAtBottom;
        originalWrite(data);
        if (wasAtBottom) {
            terminal.scrollToBottom();
        }
    };
}

/**
 * Setup compose mode (predictive text + speech-to-text + image upload)
 */
function setupComposeMode() {
    // Open compose modal and unlock control mode
    composeBtn.addEventListener('click', () => {
        // Unlock control mode if locked (so compose can send input)
        if (!isControlUnlocked) {
            toggleControl();
        }
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
            // Send text first
            sendInput(text);
            // Then send Enter separately (as terminal expects discrete keypress)
            if (withEnter) {
                sendInput('\r');
            }
            closeComposeModal();
            terminal.focus();
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

    // Camera button - trigger camera input
    if (composeCamera) {
        composeCamera.addEventListener('click', () => {
            composeCameraInput.click();
        });
    }

    // Gallery button - trigger gallery input
    if (composeGallery) {
        composeGallery.addEventListener('click', () => {
            composeGalleryInput.click();
        });
    }

    // Handle camera file selection
    if (composeCameraInput) {
        composeCameraInput.addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            composeCameraInput.value = '';
            await uploadAttachment(file, composeCamera);
        });
    }

    // Handle gallery file selection
    if (composeGalleryInput) {
        composeGalleryInput.addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            composeGalleryInput.value = '';
            await uploadAttachment(file, composeGallery);
        });
    }

    // Handle paste - detect images and auto-upload
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
        // Text paste proceeds normally
    });
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
    // Toggle select mode
    const toggleSelectMode = (e) => {
        e.preventDefault();

        isSelectMode = !isSelectMode;
        selectStart = null;

        if (isSelectMode) {
            selectModeBtn.classList.add('active');
            selectModeBtn.textContent = 'Tap start';
            terminal.clearSelection();
        } else {
            selectModeBtn.classList.remove('active');
            selectModeBtn.textContent = 'Select';
            setTimeout(() => terminal.focus(), 100);
        }
    };

    if (selectModeBtn) {
        selectModeBtn.addEventListener('click', toggleSelectMode);
    }

    // Handle taps on terminal for selection - use click only to avoid double-firing
    // Note: Works in both View and Control mode (viewBar is always visible)
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
                if (selectModeBtn) selectModeBtn.textContent = 'Tap end';
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

                // Exit select mode but keep selection visible for copy
                isSelectMode = false;
                selectStart = null;
                if (selectModeBtn) {
                    selectModeBtn.classList.remove('active');
                    selectModeBtn.textContent = 'Select';
                }
                // Restore focus so user can type or tap Copy
                setTimeout(() => terminal.focus(), 100);
            }
        } catch (err) {
            console.error('Selection error:', err);
            isSelectMode = false;
            selectStart = null;
            if (selectModeBtn) {
                selectModeBtn.classList.remove('active');
                selectModeBtn.textContent = 'Select';
            }
            // Restore focus on error too
            setTimeout(() => terminal.focus(), 100);
        }
    });

    // Copy button
    if (copyBtn) {
        const resetCopyState = () => {
            isSelectMode = false;
            selectStart = null;
            if (selectModeBtn) {
                selectModeBtn.classList.remove('active');
                selectModeBtn.textContent = 'Select';
            }
            setTimeout(() => {
                terminal.focus();
                if (document.activeElement !== terminal.textarea) {
                    terminal.textarea.focus();
                }
            }, 50);
        };

        const handleCopy = () => {
            const selection = terminal.getSelection();

            if (!selection) {
                copyBtn.textContent = 'Select first';
                setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
                resetCopyState();
                return;
            }

            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(selection).then(() => {
                    copyBtn.textContent = 'Copied!';
                    setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
                }).catch(() => {
                    fallbackCopy(selection);
                }).finally(() => {
                    terminal.clearSelection();
                    resetCopyState();
                });
            } else {
                fallbackCopy(selection);
                terminal.clearSelection();
                resetCopyState();
            }
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
                copyBtn.textContent = success ? 'Copied!' : 'Failed';
            } catch (e) {
                copyBtn.textContent = 'Failed';
            }
            setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
        };

        copyBtn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            handleCopy();
        });
    }

    // Stop button (Ctrl+C)
    if (stopBtn) {
        stopBtn.addEventListener('click', () => {
            if (socket && socket.readyState === WebSocket.OPEN) {
                sendInput('\x03');  // Ctrl+C
                terminal.focus();
            }
        });
    }
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
 * View toggle: Terminal | Log | Context | Touch
 */
let currentView = 'terminal';  // 'terminal', 'transcript', 'context', 'touch'
let transcriptText = '';  // Cached transcript text

function setupViewToggle() {
    terminalViewBtn.addEventListener('click', () => {
        if (currentView !== 'terminal') {
            switchToTerminalView();
        }
    });

    transcriptViewBtn.addEventListener('click', () => {
        if (currentView !== 'transcript') {
            switchToTranscriptView();
        }
    });

    contextViewBtn.addEventListener('click', () => {
        if (currentView !== 'context') {
            switchToContextView();
        }
    });

    touchViewBtn.addEventListener('click', () => {
        if (currentView !== 'touch') {
            switchToTouchView();
        }
    });

    // Refresh buttons
    const contextRefresh = document.getElementById('contextRefresh');
    const touchRefresh = document.getElementById('touchRefresh');
    if (contextRefresh) {
        contextRefresh.addEventListener('click', fetchContext);
    }
    if (touchRefresh) {
        touchRefresh.addEventListener('click', fetchTouch);
    }
}

function clearAllTabActive() {
    terminalViewBtn.classList.remove('active');
    transcriptViewBtn.classList.remove('active');
    contextViewBtn.classList.remove('active');
    touchViewBtn.classList.remove('active');
}

function hideAllContainers() {
    terminalContainer.classList.add('hidden');
    transcriptContainer.classList.add('hidden');
    contextContainer.classList.add('hidden');
    touchContainer.classList.add('hidden');
}

function switchToTerminalView() {
    currentView = 'terminal';
    clearAllTabActive();
    terminalViewBtn.classList.add('active');
    hideAllContainers();
    terminalContainer.classList.remove('hidden');
    viewBar.classList.remove('hidden');  // Show action bar in terminal view
    // Only show control bars if unlocked
    if (isControlUnlocked) {
        controlBarsContainer.classList.remove('hidden');
    }
}

async function switchToTranscriptView() {
    currentView = 'transcript';
    clearAllTabActive();
    transcriptViewBtn.classList.add('active');
    hideAllContainers();
    transcriptContainer.classList.remove('hidden');
    viewBar.classList.add('hidden');  // Hide action bar in non-terminal views
    controlBarsContainer.classList.add('hidden');

    // Fetch transcript and scroll to bottom
    await fetchTranscript();
    transcriptContent.scrollTop = transcriptContent.scrollHeight;
}

async function switchToContextView() {
    currentView = 'context';
    clearAllTabActive();
    contextViewBtn.classList.add('active');
    hideAllContainers();
    contextContainer.classList.remove('hidden');
    viewBar.classList.add('hidden');
    controlBarsContainer.classList.add('hidden');

    await fetchContext();
}

async function switchToTouchView() {
    currentView = 'touch';
    clearAllTabActive();
    touchViewBtn.classList.add('active');
    hideAllContainers();
    touchContainer.classList.remove('hidden');
    viewBar.classList.add('hidden');
    controlBarsContainer.classList.add('hidden');

    await fetchTouch();
}

async function fetchContext() {
    contextContent.textContent = 'Loading CONTEXT.md...';

    try {
        const response = await fetch(`/api/context?token=${token}`);
        if (!response.ok) {
            throw new Error('Failed to fetch context');
        }
        const data = await response.json();

        if (!data.exists) {
            contextContent.textContent = 'No CONTEXT.md file found in .claude/ directory.';
            return;
        }

        contextContent.textContent = data.content;
    } catch (error) {
        console.error('Context error:', error);
        contextContent.textContent = 'Error loading context: ' + error.message;
    }
}

async function fetchTouch() {
    touchContent.textContent = 'Loading touch-summary.md...';

    try {
        const response = await fetch(`/api/touch?token=${token}`);
        if (!response.ok) {
            throw new Error('Failed to fetch touch summary');
        }
        const data = await response.json();

        if (!data.exists) {
            touchContent.textContent = 'No touch-summary.md file found in .claude/ directory.';
            return;
        }

        touchContent.textContent = data.content;
    } catch (error) {
        console.error('Touch error:', error);
        touchContent.textContent = 'Error loading touch summary: ' + error.message;
    }
}

let transcriptSource = '';  // 'log' or 'capture'

async function fetchTranscript() {
    transcriptContent.textContent = 'Loading transcript...';
    transcriptSearchCount.textContent = '';

    try {
        // Use capture-pane for cleaner output (pipe-pane log has screen redraws)
        const response = await fetch(`/api/transcript?token=${token}&source=capture`);
        if (!response.ok) {
            throw new Error('Failed to fetch transcript');
        }
        const data = await response.json();
        transcriptText = data.text || '';
        transcriptSource = data.source || 'capture';

        // Show source indicator
        const sourceLabel = transcriptSource === 'log' ? 'Live Log' : 'Snapshot';
        transcriptSearchCount.textContent = sourceLabel;

        renderTranscript(transcriptText);
    } catch (error) {
        console.error('Transcript error:', error);
        transcriptContent.textContent = 'Error loading transcript: ' + error.message;
    }
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


// Initialize
document.addEventListener('DOMContentLoaded', async () => {
    initDOMElements();

    // IMPORTANT: Size terminal with ALL bars visible to get the smallest size
    // This ensures tmux gets a consistent size regardless of View/Control mode
    controlBarsContainer.classList.remove('hidden');
    // viewBar is always visible (no toggle needed)

    initTerminal();  // Fits terminal to container (with all bars taking space)

    // Switch to View mode layout - start in View mode
    controlBarsContainer.classList.add('hidden');

    setupEventListeners();
    setupTerminalFocus();
    setupViewportHandler();
    setupClipboard();
    setupRepoDropdown();
    setupFileSearch();
    setupJumpToBottom();
    setupCopyButton();
    setupCommandHistory();
    setupComposeMode();
    setupViewToggle();
    setupTranscriptSearch();

    // Load current session first, then config
    await loadCurrentSession();
    await loadConfig();

    connect();
});
