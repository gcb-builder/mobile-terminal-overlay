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

// DOM elements
const terminalContainer = document.getElementById('terminal-container');
const controlBtn = document.getElementById('controlBtn');
const controlIndicator = document.getElementById('controlIndicator');
const controlBar = document.getElementById('controlBar');
const roleBar = document.getElementById('roleBar');
const quickBar = document.getElementById('quickBar');
const statusOverlay = document.getElementById('statusOverlay');
const statusText = document.getElementById('statusText');
const repoBtn = document.getElementById('repoBtn');
const repoLabel = document.getElementById('repoLabel');
const repoDropdown = document.getElementById('repoDropdown');
const searchBtn = document.getElementById('searchBtn');
const searchModal = document.getElementById('searchModal');
const searchInput = document.getElementById('searchInput');
const searchClose = document.getElementById('searchClose');
const searchResults = document.getElementById('searchResults');

/**
 * Initialize the terminal
 * Uses fixed size to prevent resize-triggered redraws from Claude Code
 */
function initTerminal() {
    // Fixed terminal size - prevents resize events that cause duplications
    const FIXED_COLS = 80;
    const FIXED_ROWS = 30;

    terminal = new Terminal({
        cursorBlink: false,
        cursorStyle: 'bar',
        cursorInactiveStyle: 'none',
        fontSize: 13,
        fontFamily: 'Menlo, Monaco, "Courier New", monospace',
        cols: FIXED_COLS,
        rows: FIXED_ROWS,
        scrollback: 5000,
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

    // Web links addon for clickable URLs
    const webLinksAddon = new WebLinksAddon.WebLinksAddon();
    terminal.loadAddon(webLinksAddon);

    terminal.open(terminalContainer);

    // Handle terminal input (only when unlocked)
    // Send as binary for faster processing (bypasses JSON parsing on server)
    const encoder = new TextEncoder();

    // Track composition to avoid double-sending
    let compositionText = '';
    let isComposing = false;

    // Send characters immediately during IME composition (mobile keyboards)
    terminal.textarea.addEventListener('compositionstart', () => {
        isComposing = true;
        compositionText = '';
    });

    terminal.textarea.addEventListener('compositionupdate', (e) => {
        if (!isControlUnlocked || !socket || socket.readyState !== WebSocket.OPEN) return;

        // Send only new characters added to composition
        const newText = e.data || '';
        if (newText.length > compositionText.length) {
            const newChars = newText.slice(compositionText.length);
            socket.send(encoder.encode(newChars));
        }
        compositionText = newText;
    });

    terminal.textarea.addEventListener('compositionend', () => {
        isComposing = false;
        // Small delay before clearing to let onData check
        setTimeout(() => { compositionText = ''; }, 50);
    });

    terminal.onData((data) => {
        if (isControlUnlocked && socket && socket.readyState === WebSocket.OPEN) {
            // Skip if this text was already sent during composition
            if (compositionText && data === compositionText) {
                return;
            }
            // Skip during active composition (compositionupdate handles it)
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
 * Connect to WebSocket
 */
function connect() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/terminal?token=${token}`;

    statusText.textContent = 'Connecting...';
    statusOverlay.classList.remove('hidden');

    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
        console.log('WebSocket connected');
        statusOverlay.classList.add('hidden');
        sendResize();
    };

    socket.onmessage = (event) => {
        if (event.data instanceof Blob) {
            event.data.arrayBuffer().then((buffer) => {
                terminal.write(new Uint8Array(buffer));
            });
        } else {
            terminal.write(event.data);
        }
    };

    socket.onclose = (event) => {
        console.log('WebSocket closed:', event.code, event.reason);
        statusText.textContent = 'Disconnected. Reconnecting...';
        statusOverlay.classList.remove('hidden');

        // Reconnect after delay
        setTimeout(connect, 2000);
    };

    socket.onerror = (error) => {
        console.error('WebSocket error:', error);
        statusText.textContent = 'Connection error';
    };
}

/**
 * Send terminal resize
 */
function sendResize() {
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

        controlIndicator.classList.remove('locked');
        controlIndicator.classList.add('unlocked');
        controlIndicator.textContent = 'Tap terminal';

        controlBar.classList.remove('hidden');
        roleBar.classList.remove('hidden');
        quickBar.classList.remove('hidden');

        // Focus terminal for direct input
        terminal.focus();
        terminalContainer.classList.add('focusable');
    } else {
        controlBtn.classList.remove('unlocked');
        controlBtn.classList.add('locked');
        controlBtn.querySelector('.lock-icon').innerHTML = '&#x1F512;';

        controlIndicator.classList.remove('unlocked');
        controlIndicator.classList.add('locked');
        controlIndicator.textContent = 'View';

        controlBar.classList.add('hidden');
        roleBar.classList.add('hidden');
        quickBar.classList.add('hidden');
        terminalContainer.classList.remove('focusable');
    }
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
            controlIndicator.textContent = 'Typing';
        }
    });

    // Update indicator when terminal loses focus
    terminal.textarea.addEventListener('blur', () => {
        if (isControlUnlocked) {
            controlIndicator.textContent = 'Tap terminal';
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

        // Reconnect WebSocket
        if (socket) {
            socket.close();
        }
        setTimeout(connect, 500);

    } catch (error) {
        console.error('Error switching repo:', error);
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

    // Key mapping for control and quick buttons
    const keyMap = {
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

    // Quick buttons (numbers, arrows, y/n/enter) - use pointerup for better mobile support
    quickBar.querySelectorAll('.quick-btn').forEach((btn) => {
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

    // Prevent zoom on double-tap (but not on buttons)
    document.addEventListener('touchend', (e) => {
        // Don't interfere with button taps
        if (e.target.closest('button')) return;

        const now = Date.now();
        if (now - lastTouchEnd <= 300) {
            e.preventDefault();
        }
        lastTouchEnd = now;
    }, { passive: false });
}

let lastTouchEnd = 0;

// Setup viewport - no resize events, just back button handling
function setupViewportHandler() {
    // Disable Android back button navigation
    history.pushState(null, '', window.location.href);
    window.addEventListener('popstate', (e) => {
        history.pushState(null, '', window.location.href);
    });

    // Scroll terminal into view when keyboard opens (don't resize)
    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', () => {
            // Just scroll to keep cursor visible, don't resize terminal
            terminal.scrollToBottom();
        });
    }
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

// Initialize
document.addEventListener('DOMContentLoaded', async () => {
    initTerminal();
    setupEventListeners();
    setupTerminalFocus();
    setupViewportHandler();
    setupClipboard();
    setupRepoDropdown();
    setupFileSearch();

    // Load current session first, then config
    await loadCurrentSession();
    await loadConfig();

    connect();
});
