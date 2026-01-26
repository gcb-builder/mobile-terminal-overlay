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
- [ ] Replace `repoBtn`/`repoLabel`/`repoDropdown` + `targetBtn`/`targetLabel`/`targetDropdown` with:
  - `navBtn` with `navLabel` (shows "repo • pane")
  - `navDropdown` (single dropdown)
- [ ] Remove target lock button (merge lock logic or simplify)
- [ ] Bump CSS/JS versions

### 2. CSS Changes (styles.css)
- [ ] Add `.nav-section-header` - muted section title (CURRENT SESSION, etc.)
- [ ] Add `.nav-section-divider` - subtle horizontal line
- [ ] Add `.reconnect-pill` - small badge saying "Reconnect"
- [ ] Reuse `.target-option` for pane rows, `.repo-option` patterns
- [ ] Add `.nav-check` for checkmark on current pane

### 3. JS Changes (terminal.js)
- [ ] Update DOM refs: `navBtn`, `navLabel`, `navDropdown`
- [ ] Create `updateNavLabel()` - sets button to "repoLabel • window:pane"
- [ ] Create `renderNavDropdown()`:
  - Section 1: Current session panes (from `targets`)
  - Section 2: Actions (+ New Window if repos configured)
  - Section 3: Other sessions (from `config.repos` where session !== currentSession)
- [ ] Rename `switchRepo()` to `switchSession()` for clarity
- [ ] Keep `selectTarget()` as-is (already lightweight, no reconnect)
- [ ] Remove old `populateRepoDropdown()`, `renderTargetDropdown()`
- [ ] Wire up dropdown toggle on navBtn click
- [ ] Auto-close on outside click
- [ ] After new window creation: refresh targets, auto-select new pane

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
