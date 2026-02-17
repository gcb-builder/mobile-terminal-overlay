# Implementation Plan: Always-on PTY Drain + Dual Output Modes

## Problem
Terminal remains unresponsive even after reducing initial history. The real issue is **continuous PTY streaming** that floods xterm.js with data, blocking the main thread.

## Solution Architecture
Decouple terminal I/O from xterm.js rendering via dual output modes:

1. **mode=tail** (default): Server sends rate-limited plain text snapshots (~200ms interval)
2. **mode=full**: Server sends raw PTY bytes (only when Terminal tab is visible)

## CRITICAL: PTY MUST ALWAYS DRAIN

**The PTY read loop runs continuously regardless of mode.**

- PTY → always read into ring buffer (never pause)
- Ring buffer → stripped for tail mode OR forwarded raw for full mode

Pausing PTY causes:
- tmux buffer buildup
- Giant burst on resume
- Worse freezes later

## Existing Infrastructure
- `term_subscribe`/`term_unsubscribe` messages exist (server.py:5680-5685) but are no-ops
- `startTailViewport()`/`stopTailViewport()` exist (terminal.js:3395-3396)
- View switching functions: `switchToLogView()` (3534), `switchToTerminalView()` (3559)

## Files to Modify

### Server: `mobile_terminal/server.py`

**Changes to WebSocket handler (line ~5580):**

1. Add per-client mode state:
```python
client_mode = "tail"  # "tail" or "full"
```

2. Add tail buffer (ring buffer of last ~50 lines, ANSI-stripped):
```python
tail_lines = []  # Last 50 lines of plain text
tail_seq = 0
last_tail_send = 0
TAIL_INTERVAL = 0.2  # 200ms max rate
```

3. Modify `read_from_terminal()` function (line ~5582):
   - **ALWAYS drain PTY** - never pause or slow down reads
   - Always store in ring buffer (for tail extraction + mode switch catchup)
   - If `client_mode == "full"`: forward raw bytes to WebSocket
   - If `client_mode == "tail"`: skip WebSocket send (save bandwidth)
   - Separate async task: send `{type:"tail", text:"...", seq:n}` every 200ms

4. Handle `set_mode` message in `write_to_terminal()` (line ~5680):
```python
elif msg_type == "set_mode":
    client_mode = data.get("mode", "tail")
    logger.info(f"Client mode changed to: {client_mode}")
    if client_mode == "full":
        # Send recent buffer to catch up
        await websocket.send_bytes(bytes(recent_buffer))
```

### Client: `mobile_terminal/static/terminal.js`

**Changes to WebSocket message handling (line ~918):**

1. Add mode state (near line 14):
```javascript
let outputMode = 'tail';  // 'tail' or 'full'
```

2. Add mode switching in view functions:
   - `switchToTerminalView()` (line 3559): send `{type:"set_mode", mode:"full"}`
   - `switchToLogView()` (line 3534): send `{type:"set_mode", mode:"tail"}`
   - `switchToTranscriptView()`: send `{type:"set_mode", mode:"tail"}`

3. Handle new message types in `socket.onmessage`:
```javascript
if (msg.type === 'tail') {
    // Update tail strip in Log view (cheap DOM update)
    updateTailStrip(msg.text);
    return;
}
if (msg.type === 'pty') {
    // Only process if in Terminal view
    if (outputMode === 'full') {
        terminal.write(new Uint8Array(msg.data));
    }
    return;
}
```

4. **Buffered xterm writes** (even in full mode):
```javascript
let writeQueue = [];
let draining = false;

function queuedWrite(data) {
    writeQueue.push(data);
    if (!draining) {
        draining = true;
        requestAnimationFrame(drainQueue);
    }
}

function drainQueue() {
    const start = performance.now();
    while (writeQueue.length && performance.now() - start < 8) {
        terminal.write(writeQueue.shift());
    }
    if (writeQueue.length) {
        requestAnimationFrame(drainQueue);
    } else {
        draining = false;
    }
}
```

### Client: `mobile_terminal/static/index.html`

1. Add tail strip element in Log view (optional, for live preview)
2. Bump version

## Implementation Order

1. **Server: Add mode state and set_mode handler** - wire up the protocol
2. **Server: Modify read_from_terminal()** - conditional forwarding based on mode
3. **Server: Add ANSI stripping + tail buffer** - for tail mode output
4. **Client: Add mode switching on view change** - send set_mode messages
5. **Client: Handle tail message type** - cheap DOM update
6. **Client: Add buffered writes for full mode** - requestAnimationFrame drain

## Verification

1. Start server: `source venv/bin/activate && mobile-terminal --verbose`
2. Connect from mobile browser
3. Verify Log view is responsive immediately
4. Switch to Terminal view - should see full ANSI output
5. Switch back to Log view - should remain responsive
6. Check server logs for mode change messages

## Risks/Mitigations

- **Risk**: ANSI stripping is slow
  - **Mitigation**: Use simple regex, only strip on tail sends (200ms interval)

- **Risk**: Mode switch race conditions
  - **Mitigation**: Track sequence numbers, ignore stale messages

- **Risk**: Terminal buffer desync after mode switch
  - **Mitigation**: Send capture-pane snapshot when switching to full mode
