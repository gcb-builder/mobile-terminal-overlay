# Touch Summary Log

Append-only log of implementation batches.

---

## 2026-01-10: Initial Scaffold

**Goal:** Create initial project structure for mobile-terminal-overlay

**Files Created:**
- `pyproject.toml` - pip package configuration
- `mobile_terminal/__init__.py` - package init
- `mobile_terminal/config.py` - Config dataclass with YAML loading
- `mobile_terminal/discovery.py` - Auto-discovery from CLAUDE.md
- `mobile_terminal/server.py` - FastAPI + WebSocket tmux relay
- `mobile_terminal/cli.py` - CLI entrypoint
- `mobile_terminal/static/index.html` - Main UI with xterm.js
- `mobile_terminal/static/styles.css` - Mobile-first CSS
- `mobile_terminal/static/terminal.js` - WebSocket + xterm.js client
- `README.md` - Documentation
- `.gitignore` - Git exclusions
- `CLAUDE.md` - Claude Code guidelines
- `.claude/CONTEXT.md` - Session context
- `.claude/touch-summary.md` - This file

**Risks/Follow-ups:**
- Needs testing with actual tmux session
- Needs unit tests
- Role prefix regex may need tuning for different CLAUDE.md formats

---

## 2026-01-15: Copy/Select Fix + PWA Standalone

**Goal:** Fix copy functionality breaking terminal input, add PWA standalone support

**Files Changed:**
- `mobile_terminal/server.py` - Robust message handling, continue on errors instead of disconnecting
- `mobile_terminal/static/terminal.js` - Focus restoration after copy, isComposing reset, rewritten copy handler
- `mobile_terminal/static/index.html` - Added ^B button, service worker registration
- `mobile_terminal/static/manifest.json` - Added scope and display_override for standalone
- `mobile_terminal/static/styles.css` - Various UX improvements

**Files Created:**
- `mobile_terminal/static/sw.js` - Service worker for PWA installation
- `mobile-terminal.service` - systemd unit file for auto-start

**Commits:**
- `da6e940` Fix copy/select bug and add PWA standalone support
- `3d25636` Add Ctrl+B (tmux prefix) to control bar

**Risks/Follow-ups:**
- PWA needs reinstall after service worker changes
- systemd service needs manual setup (sudo required)

---

## 2026-01-18: Input Box Sync with tmux Terminal

**Goal:** Implement tighter alignment between mobile overlay input box and tmux terminal command line

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Added extractPromptContent(), syncPromptToInput(), sendKeyWithSync(), sendKeyDebounced(); atomic send in sendLogCommand(); modified control bar handlers for Up/Down/Tab sync
- `mobile_terminal/static/index.html` - Added sync button (↻) to terminal-block-input, version bumps
- `mobile_terminal/static/styles.css` - Added .terminal-sync-btn styling
- `mobile_terminal/static/sw.js` - Version bump to v31

**New Functions (terminal.js):**
- `extractPromptContent(content)` - Multi-pattern prompt detection (Claude ❯, bash $, Python >>>, Node >)
- `syncPromptToInput()` - Captures terminal and syncs prompt content to input box
- `sendKeyWithSync(key, delay)` - Sends key and syncs result back to input
- `sendKeyDebounced(key, force)` - 150ms debounce, Ctrl+C bypasses

**Commits:**
- `9bd1ea9` Add input box sync with tmux terminal command line

**Risks/Follow-ups:**
- Sync delay (100-200ms) may feel slow on laggy connections
- Prompt patterns may need expansion for other shells (fish, nushell)

---

## 2026-01-18: UI Simplification (continued)

**Goal:** Remove lock button, simplify controls, fix tail viewport filtering

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Removed toggleControl(), controlBtn; set isControlUnlocked=true; empty Enter sends '\r'; fixed ⏵⏵ filter to not hide Claude options
- `mobile_terminal/static/index.html` - Removed lock button, moved collapse toggle to tab indicator
- `mobile_terminal/static/styles.css` - Resized collapse toggle (32x24px pill) for tab bar

**Commits:**
- `eb4e898` Fix tail viewport filtering and empty Enter behavior
- `298ea42` Remove lock button, move collapse toggle to tab indicator

**Changes:**
- Controls always enabled (no lock/unlock)
- Collapse toggle in tab indicator section (next to dots)
- Empty input Enter confirms prompts (like control bar Enter)
- ⏵⏵ filter now only hides status hints, not interactive options

---

## 2026-01-18: Header Simplification and Unified Collapse

**Goal:** Streamline header, remove redundant indicators, unify collapse behavior

**Files Changed:**
- `mobile_terminal/static/index.html` - Removed "Log" title and log-view-header; removed sync button from input box; added refresh button to header; removed working indicator
- `mobile_terminal/static/styles.css` - Removed .log-view-header, .terminal-sync-btn, .header-thinking styles; added .view-bar.collapsed
- `mobile_terminal/static/terminal.js` - Removed updateWorkingIndicator(), showThinking(), hideThinking(), maybeHideThinking(); updated toggleControlBarsCollapse() to also collapse viewBar; refresh button now calls loadLogContent() + syncPromptToInput()
- `mobile_terminal/static/sw.js` - Version bump to v32

**Commits:**
- `45ddc35` Simplify header and unify collapse behavior

**Changes:**
- Removed "Log" title from log view
- Refresh button moved to header (refreshes log + syncs input)
- Removed sync button from input box
- Removed "working..." indicator (redundant with activity timestamp)
- View bar (Select/Stop/Challenge/Compose) collapses with control bars
- Control bar Enter kept as stateless confirm (ignores input box state)

---

## 2026-01-18: Non-Blocking Tool Collapse for Log View

**Goal:** Collapse consecutive duplicate tool `<details>` blocks into single block with "×N" badge

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Added data-tool attributes in renderLogEntries(), scheduleCollapse(), collapseRepeatedTools(), setupCollapseHandler()
- `mobile_terminal/static/styles.css` - Added .collapsed-duplicate, .collapse-count styles
- `mobile_terminal/static/index.html` - Version bumps (v120, v151, v34)
- `mobile_terminal/static/sw.js` - Version bump to v34

**New Functions (terminal.js):**
- `scheduleCollapse()` - Hash-based scheduling via requestIdleCallback
- `collapseRepeatedTools(hash)` - Single-pass DOM collapse with badge insertion
- `setupCollapseHandler()` - Event delegation for expand/collapse toggle

**Key Design Decisions:**
- Post-render collapse via requestIdleCallback (fallback: setTimeout 100ms)
- Hash check: `${toolCount}:${lastToolKey}:${htmlLength}` to skip unchanged content
- Stable group keys: `${toolName}:${summaryText.slice(0,40)}` (not index-based)
- Single event delegation handler (no per-node listeners)
- Graceful degradation: try/catch wrapper, base render unaffected

**Risks/Follow-ups:**
- Previous attempts caused lag due to work in render loop; this uses idle callback
- Safari <16.4 uses setTimeout fallback
- Hash verification at end of collapse to detect race conditions

---

## 2026-01-19: Smart Auto-Scroll and Plan File Previews

**Goal:** Improve log view UX with conditional auto-scroll and inline plan file previews

**Problem 2: Auto-scroll Interrupts Reading**
- Track if user is at bottom of log via scroll event listener
- Only auto-scroll if `userAtBottom` is true
- Show floating "↓ New content" indicator when new content arrives while reading
- Click indicator to jump to bottom

**Problem 3: Plan Files Not Visible**
- Added `/api/plan` endpoint to read plan files from `~/.claude/plans/`
- Detect plan file paths in log text via regex
- Replace with clickable elements showing filename
- On tap, fetch 10-line preview and show inline
- Tap again to collapse

**Files Changed:**
- `mobile_terminal/server.py` - Added `/api/plan` endpoint with filename sanitization
- `mobile_terminal/static/terminal.js` - Added scroll tracking, new content indicator, plan file detection
- `mobile_terminal/static/styles.css` - Added .new-content-indicator, .plan-file-ref, .plan-preview styles
- `mobile_terminal/static/index.html` - Version bumps (v121, v152, v35)
- `mobile_terminal/static/sw.js` - Version bump to v35

**New Functions (terminal.js):**
- `setupScrollTracking()` - Scroll event listener to track userAtBottom
- `showNewContentIndicator()` / `hideNewContentIndicator()` - Floating indicator management
- `schedulePlanPreviews()` - Schedule plan detection for idle time
- `detectAndReplacePlanRefs()` - TreeWalker to find and replace plan paths
- `setupPlanPreviewHandler()` - Event delegation for plan preview clicks

**Key Design Decisions:**
- Both features use requestIdleCallback for non-blocking execution
- New content indicator positioned absolute, slides up with animation
- Plan file regex: `/(?:~|\/home\/\w+)\/\.claude\/plans\/([\w\-\.]+\.md)/g`
- API sanitizes filenames to prevent path traversal

---

## 2026-01-19: Super-Collapse for Tool Runs

**Goal:** Collapse runs of many consecutive tool calls into single summary row

**Problem:** Even with same-tool collapsing (×N badges), interleaved tool types (Read, Edit, TodoWrite, Grep) still create long lists that overwhelm the log view.

**Solution:** "Super-collapse" - when 6+ consecutive tool blocks appear, collapse them into:
```
🔧 23 tool operations ▶
```
Click to expand and see individual tools (which can still have ×N badges).

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Added scheduleSuperCollapse(), applySuperCollapse(), createSuperGroup(), setupSuperCollapseHandler()
- `mobile_terminal/static/styles.css` - Added .super-collapsed, .tool-supergroup, .tool-supergroup-toggle styles
- `mobile_terminal/static/index.html` - Version bumps (v122, v153, v36)
- `mobile_terminal/static/sw.js` - Version bump to v36

**New State Variables:**
- `SUPER_COLLAPSE_THRESHOLD = 6` - Minimum tools to trigger super-collapse
- `lastSuperCollapseHash` - Skip work if unchanged
- `expandedSuperGroups` - Set of expanded group keys

**New Functions (terminal.js):**
- `scheduleSuperCollapse()` - Waits 150ms for regular collapse, then schedules idle pass
- `applySuperCollapse(hash)` - Finds runs of consecutive .log-tool within each .log-card-body
- `createSuperGroup(container, tools, insertIndex)` - Creates header element, hides tools
- `setupSuperCollapseHandler()` - Event delegation for toggle clicks

**Key Design Decisions:**
- Runs after 150ms delay to let regular collapse (×N badges) complete first
- Uses requestIdleCallback with 700ms timeout for non-blocking execution
- Stable group key: `supergroup:${firstToolKey}:${toolCount}`
- Super-groups are per-.log-card-body (within a Claude turn), not global
- CSS uses `!important` on .super-collapsed to ensure tools are hidden

---

## 2026-01-19: Terminal-Native UI Design (CSS Only)

**Goal:** Make UI feel like one coherent terminal with layers, not three separate widgets

**Problem:** UI felt like chat app (YOU/CLAUDE badges, rounded cards) mixed with terminal, causing mental model switching.

**Solution:** Terminal-first aesthetic throughout:

**Log cards → Terminal style:**
- Removed rounded corners, chat-style backgrounds
- Hidden YOU/CLAUDE role badges
- Monospace font for all text
- User messages prefixed with `$ ` in accent color
- Left border indicator only (2px, not full card)

**Log → Terminal handoff:**
- Added gradient fade (::before pseudo-element)
- "── live ──" label at top of terminal block
- Visual continuity from history to active

**Input → stdin feel:**
- Darker background matching terminal aesthetic
- Subtle caret color (accent)
- Placeholder opacity reduced

**Control strip → Keyboard extension:**
- Darker background (--bg-darker)
- Smaller, tighter buttons (36px)
- Monospace font on quick-btn
- Reduced gap and padding

**Files Changed:**
- `mobile_terminal/static/styles.css` - Major CSS refactor (~100 lines changed)
- `mobile_terminal/static/index.html` - Version bump (v123)
- `mobile_terminal/static/sw.js` - Version bump (v37)

**Key CSS Changes:**
- `.log-card` - border-radius: 0, no background
- `.log-card-header` - display: none (hides role badges)
- `.log-text` - monospace font
- `.log-text.user-text::before` - content: '$ '
- `.terminal-block::before` - gradient fade overlay
- `.terminal-block::after` - "live" label
- `.quick-btn` - monospace, smaller, darker

---

## 2026-01-19: Collapse Toggle Fix

**Goal:** Fix collapse button not properly expanding control bars

**Problem:** When the collapse button was tapped to collapse the control bars, tapping again to expand might not work if the `hidden` class was present (e.g., from a view switch while collapsed).

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Fixed toggleControlBarsCollapse() to also remove 'hidden' class when expanding in log/terminal views
- `mobile_terminal/static/index.html` - Version bump (v154, v38)
- `mobile_terminal/static/sw.js` - Version bump to v38

**Fix Details:**
- Modified `toggleControlBarsCollapse()` to remove `hidden` class when expanding (isCollapsed=false)
- Only applies in log and terminal views where bars should be visible
- Ensures bars become visible even if `hidden` was re-added during view switches

**Commits:**
- (pending)
