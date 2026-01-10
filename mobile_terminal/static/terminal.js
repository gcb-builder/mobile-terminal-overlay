/**
 * Mobile Terminal Overlay - Terminal Client
 *
 * Connects xterm.js to the WebSocket backend for tmux relay.
 */

// Get token from URL
const urlParams = new URLSearchParams(window.location.search);
const token = urlParams.get('token');

if (!token) {
    document.getElementById('statusText').textContent = 'No token provided';
    throw new Error('No token in URL');
}

// State
let terminal = null;
let fitAddon = null;
let socket = null;
let isControlUnlocked = false;
let config = null;

// DOM elements
const terminalContainer = document.getElementById('terminal-container');
const controlBtn = document.getElementById('controlBtn');
const controlBtnText = document.getElementById('controlBtnText');
const controlIndicator = document.getElementById('controlIndicator');
const controlBar = document.getElementById('controlBar');
const roleBar = document.getElementById('roleBar');
const quickBar = document.getElementById('quickBar');
const inputArea = document.getElementById('inputArea');
const inputField = document.getElementById('inputField');
const sendBtn = document.getElementById('sendBtn');
const statusOverlay = document.getElementById('statusOverlay');
const statusText = document.getElementById('statusText');

/**
 * Initialize the terminal
 */
function initTerminal() {
    terminal = new Terminal({
        cursorBlink: true,
        fontSize: 14,
        fontFamily: 'Menlo, Monaco, "Courier New", monospace',
        theme: {
            background: '#0b0f14',
            foreground: '#e6edf3',
            cursor: '#58a6ff',
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

    fitAddon = new FitAddon.FitAddon();
    terminal.loadAddon(fitAddon);

    const webLinksAddon = new WebLinksAddon.WebLinksAddon();
    terminal.loadAddon(webLinksAddon);

    terminal.open(terminalContainer);
    fitAddon.fit();

    // Handle terminal input (only when unlocked)
    terminal.onData((data) => {
        if (isControlUnlocked && socket && socket.readyState === WebSocket.OPEN) {
            socket.send(data);
        }
    });

    // Handle resize
    window.addEventListener('resize', () => {
        fitAddon.fit();
        sendResize();
    });

    // Initial resize after a short delay
    setTimeout(() => {
        fitAddon.fit();
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
        controlBtnText.textContent = 'Release Control';
        controlBtn.querySelector('.lock-icon').innerHTML = '&#x1F513;';

        controlIndicator.classList.remove('locked');
        controlIndicator.classList.add('unlocked');
        controlIndicator.textContent = 'Control';

        controlBar.classList.remove('hidden');
        roleBar.classList.remove('hidden');
        quickBar.classList.remove('hidden');
        inputArea.classList.remove('hidden');

        // Focus terminal
        terminal.focus();
    } else {
        controlBtn.classList.remove('unlocked');
        controlBtn.classList.add('locked');
        controlBtnText.textContent = 'Take Control';
        controlBtn.querySelector('.lock-icon').innerHTML = '&#x1F512;';

        controlIndicator.classList.remove('unlocked');
        controlIndicator.classList.add('locked');
        controlIndicator.textContent = 'View';

        controlBar.classList.add('hidden');
        roleBar.classList.add('hidden');
        quickBar.classList.add('hidden');
        inputArea.classList.add('hidden');
    }
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
 * Populate UI from config
 */
function populateUI() {
    if (!config) return;

    // Populate role buttons
    if (config.role_prefixes && config.role_prefixes.length > 0) {
        roleBar.innerHTML = '';
        config.role_prefixes.forEach((role) => {
            const btn = document.createElement('button');
            btn.className = 'role-btn';
            btn.textContent = role.label;
            btn.addEventListener('click', () => {
                inputField.value = role.insert + inputField.value;
                inputField.focus();
            });
            roleBar.appendChild(btn);
        });
    }

    // Populate quick commands
    if (config.quick_commands && config.quick_commands.length > 0) {
        quickBar.innerHTML = '';
        config.quick_commands.forEach((cmd) => {
            const btn = document.createElement('button');
            btn.className = 'quick-btn';
            btn.textContent = cmd.label;
            btn.addEventListener('click', () => {
                if (isControlUnlocked) {
                    sendInput(cmd.command);
                }
            });
            quickBar.appendChild(btn);
        });
    }
}

/**
 * Setup event listeners
 */
function setupEventListeners() {
    // Control toggle button
    controlBtn.addEventListener('click', toggleControl);

    // Control key buttons
    controlBar.querySelectorAll('.ctrl-key').forEach((btn) => {
        btn.addEventListener('click', () => {
            if (isControlUnlocked) {
                const key = btn.dataset.key;
                sendInput(key);
                terminal.focus();
            }
        });
    });

    // Send button
    sendBtn.addEventListener('click', () => {
        if (isControlUnlocked && inputField.value) {
            sendInput(inputField.value + '\n');
            inputField.value = '';
            terminal.focus();
        }
    });

    // Input field enter key
    inputField.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (isControlUnlocked && inputField.value) {
                sendInput(inputField.value + '\n');
                inputField.value = '';
            }
        }
    });

    // Auto-resize textarea
    inputField.addEventListener('input', () => {
        inputField.style.height = 'auto';
        inputField.style.height = Math.min(inputField.scrollHeight, 120) + 'px';
    });

    // Prevent zoom on double-tap
    document.addEventListener('touchend', (e) => {
        const now = Date.now();
        if (now - lastTouchEnd <= 300) {
            e.preventDefault();
        }
        lastTouchEnd = now;
    }, { passive: false });
}

let lastTouchEnd = 0;

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initTerminal();
    setupEventListeners();
    loadConfig();
    connect();
});
