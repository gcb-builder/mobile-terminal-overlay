/**
 * Pure utility functions — no global state, no side effects.
 * Extracted from terminal.js for module boundaries.
 */

/**
 * Cancellable sleep using AbortController signal.
 */
export function abortableSleep(ms, signal) {
    return new Promise((resolve, reject) => {
        if (signal?.aborted) {
            reject(new DOMException('Aborted', 'AbortError'));
            return;
        }
        const timeout = setTimeout(resolve, ms);
        signal?.addEventListener('abort', () => {
            clearTimeout(timeout);
            reject(new DOMException('Aborted', 'AbortError'));
        }, { once: true });
    });
}

/**
 * Find a safe UTF-8/ANSI boundary for splitting binary data.
 * Returns the safe cut position <= maxPos.
 */
export function findSafeBoundary(data, maxPos) {
    if (maxPos >= data.length) return data.length;

    let pos = maxPos;

    // Scan backwards to find a safe position (max 64 bytes back)
    // Safe position: not inside an ANSI sequence and not inside UTF-8
    const scanLimit = Math.max(0, pos - 64);

    while (pos > scanLimit) {
        const byte = data[pos];

        // UTF-8 continuation byte (10xxxxxx) - not safe
        if ((byte & 0xC0) === 0x80) {
            pos--;
            continue;
        }

        // Check if we're inside an ANSI escape sequence
        // Scan backwards for ESC (0x1B) and see if sequence is incomplete
        let foundEsc = false;
        let escPos = pos - 1;
        const escLimit = Math.max(0, pos - 32);  // ANSI sequences rarely > 32 bytes

        while (escPos >= escLimit) {
            if (data[escPos] === 0x1B) {
                foundEsc = true;
                break;
            }
            // Stop if we hit a sequence terminator (letter 0x40-0x7E)
            const b = data[escPos];
            if (b >= 0x40 && b <= 0x7E) break;
            escPos--;
        }

        if (foundEsc) {
            // Check if sequence is complete before pos
            // Sequence ends with letter (0x40-0x7E)
            let seqComplete = false;
            for (let i = escPos + 1; i < pos; i++) {
                const b = data[i];
                // '[' starts CSI sequence, continue looking
                if (b === 0x5B && i === escPos + 1) continue;
                // Terminator found before pos - sequence complete
                if (b >= 0x40 && b <= 0x7E) {
                    seqComplete = true;
                    break;
                }
            }

            if (!seqComplete) {
                // We're inside an incomplete ANSI sequence - move before it
                pos = escPos;
                continue;
            }
        }

        // Position is safe
        return pos;
    }

    // Couldn't find safe position - fall back to maxPos
    return maxPos;
}

/**
 * Format byte count as human-readable size.
 */
export function formatFileSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Remove spinner, progress bar, and status lines from terminal output.
 */
export function cleanTerminalOutput(text) {
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

        // Skip Claude Code promotional/UI chrome lines
        if (/^Tip:\s/i.test(trimmed) || /\/passes\s*$/.test(trimmed)) {
            continue;
        }
        if (/Resume this session with:\s*claude\s+--resume/i.test(trimmed)) {
            continue;
        }
        // Skip status-bar fragments: "▪▪▪", "esc to interrupt", isolated dots
        if (/^[▪•·\s─]+$/.test(trimmed) || /^esc to interrupt$/i.test(trimmed)) {
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

/**
 * Strip ANSI escape codes from text.
 */
export function stripAnsi(text) {
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

/**
 * Escape regex special characters.
 */
export function escapeRegExp(string) {
    return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Classify a log entry by type (permission, error, output, system).
 */
export function classifyLogEntry(msg) {
    const allText = msg.blocks.map(b => b.text).join('\n').toLowerCase();

    // Permission-related entries
    if (allText.includes('permission') || allText.includes('allow') ||
        allText.includes('deny') || allText.includes('\u{1F512}')) {
        return 'permission';
    }

    // Error entries
    if (allText.includes('error') || allText.includes('failed') ||
        allText.includes('exception') || allText.includes('traceback') ||
        allText.includes('fatal')) {
        return 'error';
    }

    // Tool output
    if (msg.blocks.some(b => b.role === 'tool')) {
        return 'output';
    }

    return 'system';
}

/**
 * Yield to main thread — allows browser to handle events/paint.
 */
export function yieldToMain() {
    return new Promise(resolve => {
        if ('requestIdleCallback' in window) {
            requestIdleCallback(resolve, { timeout: 50 });
        } else {
            setTimeout(resolve, 0);
        }
    });
}

/**
 * Escape HTML entities for safe innerHTML insertion.
 */
export function escapeHtml(text) {
    if (!text) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/**
 * Split a string into args respecting single and double quotes.
 */
export function shellSplit(str) {
    const args = [];
    let current = '';
    let inSingle = false, inDouble = false;
    for (const ch of str) {
        if (ch === "'" && !inDouble) { inSingle = !inSingle; continue; }
        if (ch === '"' && !inSingle) { inDouble = !inDouble; continue; }
        if (ch === ' ' && !inSingle && !inDouble) {
            if (current) args.push(current);
            current = '';
            continue;
        }
        current += ch;
    }
    if (current) args.push(current);
    return args;
}

/**
 * Format a timestamp as a relative time string (e.g. "5m", "2h").
 */
export function formatTimeAgo(timestamp) {
    const now = Date.now();
    const diff = now - timestamp;
    const seconds = Math.floor(diff / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);

    if (days > 0) return `${days}d`;
    if (hours > 0) return `${hours}h`;
    if (minutes > 0) return `${minutes}m`;
    return `${seconds}s`;
}
