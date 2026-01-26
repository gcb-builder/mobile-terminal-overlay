# Unified Navigation Dropdown Implementation Plan

## Goal
Replace separate repo dropdown + target dropdown with a single unified navigation button.

## Current State
- `repoBtn` + `repoDropdown`: switches sessions (reconnects WebSocket)
- `targetBtn` + `targetDropdown`: switches panes within session (no reconnect)

## New State
- Single `navBtn` + `navDropdown` with sections:
  1. **CURRENT SESSION** - panes (selectTarget, no reconnect)
  2. **ACTIONS** - "+ New Window..."
  3. **OTHER SESSIONS** - repos (switchSession, reconnect)

## Implementation Checklist

### 1. HTML Changes (index.html)
- [x] Keep existing element IDs (safer approach - no breaking DOM changes)
- [x] Bump CSS/JS versions (v144, v202)

### 2. CSS Changes (styles.css)
- [x] Add `.nav-section-header` - muted section title
- [x] Add `.nav-section-divider` - subtle horizontal line
- [x] Add `.nav-pane-option` - pane rows with checkmark support
- [x] Add `.nav-session-option` - session rows with Switch pill
- [x] Add `.nav-action-option` - action rows (blue)
- [x] Add `.reconnect-pill` - small badge
- [x] Hide `.target-btn`, `.target-lock-btn`, `.target-dropdown` via CSS

### 3. JS Changes (terminal.js)
- [x] Add `updateNavLabel()` - sets button to "repo • pane" format
- [x] Rewrite `populateRepoDropdown()` to render 3 sections
- [x] Make `updateTargetLabel()` redirect to `updateNavLabel()`
- [x] Make `renderTargetDropdown()` a no-op
- [x] Make `updateLockUI()` a no-op
- [x] Update `selectTarget()` to close repoDropdown
- [x] Update `loadTargets()` to call `updateNavLabel()`
- [x] Update `switchRepo()` to call `updateNavLabel()`
- [x] Update `setupTargetSelector()` to remove old event handlers

### 4. Server Changes
- None needed - existing `/api/targets` and `/api/repos` suffice

## Key Constraints
- `selectTarget()` must NEVER trigger WebSocket reconnect
- `switchSession()` MUST trigger reconnect
- Button label updates instantly (not waiting for API)
- No heavy DOM work in refresh loop (dropdown renders on open only)
- pane_id is canonical; "window:pane" is display only

## Commits (incremental)
1. HTML + CSS scaffold for unified dropdown
2. JS: Add renderNavDropdown() and updateNavLabel()
3. JS: Wire up events, remove old functions
4. Test and polish
