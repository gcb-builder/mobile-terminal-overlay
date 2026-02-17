# Plan: New Session in Different Repo

## Goal
Add the ability to create a new tmux window in a configured repo from the mobile terminal UI.

## Security Constraints
- **No arbitrary paths**: Only allow repos explicitly configured in `.mobile-terminal.yaml`
- **Window name sanitization**: SERVER-SIDE only - strip all non-alphanumeric characters except `-`, `_`, `.`
- **Subprocess safety**: Use list arguments (no shell=True, no string interpolation)

---

## Changes

### 1. Server: Add `POST /api/session/new` endpoint

**File**: `mobile_terminal/server.py` (after `/api/target/select` ~line 1157)

```python
@app.post("/api/session/new")
async def create_session_window(
    repo_label: str = Query(...),
    window_name: Optional[str] = Query(None),
    auto_start_claude: bool = Query(False),
    token: Optional[str] = Query(None),
):
```

**Server-side logic** (all validation/sanitization here, not frontend):

1. Auth check (existing pattern)
2. Look up `repo_label` in `config.repos` - reject if not found (404)
3. Verify `repo.path` exists on disk (400 if not)
4. **Sanitize window name server-side**: `re.sub(r'[^a-zA-Z0-9_\-.]', '', name)[:50]`
5. **Handle duplicate names**: Append timestamp suffix if name exists
   ```python
   # Check if window name exists
   check = subprocess.run(["tmux", "list-windows", "-t", session, "-F", "#{window_name}"], ...)
   if window_label in check.stdout:
       window_label = f"{window_label}-{int(time.time()) % 10000}"
   ```
6. Run: `tmux new-window -t {session} -n {name} -c {path} -P -F "#{window_index}:#{pane_index}|#{pane_id}"`
   - Uses subprocess list args (no shell injection possible)
7. Parse output to get `target_id` (format: "window:pane" like "3:0")
8. If `auto_start_claude`: run `tmux send-keys -t {session}:{target} "claude" Enter`
9. Return `{success, target_id, window_name, path}`

**Why target-to-repo mapping works**: The new window's `cwd` is set to `repo.path` via `-c` flag. When `/api/targets` calls `tmux list-panes`, it returns each pane's `#{pane_current_path}`. So the new target automatically has the correct repo path without additional mapping.

### 2. Client: Add "New Window" button to target dropdown

**File**: `mobile_terminal/static/terminal.js`

**Add to target dropdown** (after listing targets):
- Divider line
- "+ New window in repo..." button
- Opens modal with repo selector

**New functions**:
```javascript
async function createNewRepoWindow(repoLabel, windowName, autoStartClaude)
function showNewWindowModal()  // Lists repos from config
```

**After creation**:
- Call `loadTargets()` to refresh list
- Auto-select the new `target_id` (call existing `selectTarget()`)
- Show success toast

### 3. HTML: Add new window modal

**File**: `mobile_terminal/static/index.html`

Add modal structure (similar to existing modals):
- Radio list of configured repos (label + path preview)
- Optional: custom window name input
- Checkbox: "Start Claude automatically"
- Create / Cancel buttons

### 4. CSS: Style additions

**File**: `mobile_terminal/static/styles.css`

- `.target-dropdown-divider`
- `.new-window-btn`
- Modal styling (reuse existing `.challenge-modal` pattern)

---

## Files to Modify

| File | Changes |
|------|---------|
| `mobile_terminal/server.py` | Add `/api/session/new` endpoint, sanitize helper, duplicate name handling |
| `mobile_terminal/static/terminal.js` | Add `createNewRepoWindow()`, `showNewWindowModal()`, modify target dropdown |
| `mobile_terminal/static/index.html` | Add new window modal HTML |
| `mobile_terminal/static/styles.css` | Add modal + button styles |
| `.claude/CONTEXT.md` | Document new feature |

---

## Error Handling (all server-side)

| Scenario | Status | Response |
|----------|--------|----------|
| Invalid/missing token | 401 | `{"error": "Unauthorized"}` |
| Unknown repo label | 404 | `{"error": "Unknown repo", "available": [...]}` |
| Repo path doesn't exist | 400 | `{"error": "Repo path does not exist: /path/..."}` |
| Window name invalid after sanitization | 400 | `{"error": "Invalid window name"}` |
| tmux command failed | 500 | `{"error": "Failed to create window", "detail": "stderr..."}` |

---

## Verification

1. **Config prerequisite**: Ensure `.mobile-terminal.yaml` has multiple repos:
   ```yaml
   repos:
     - label: "project-a"
       path: "/path/to/project-a"
       session: "claude"
     - label: "project-b"
       path: "/path/to/project-b"
       session: "claude"
   ```

2. **Test flow**:
   - Open target dropdown
   - Click "+ New window in repo..."
   - Select a repo from list
   - Click Create
   - Verify new target appears in dropdown with correct cwd
   - Verify target auto-selected
   - Verify `/api/targets` shows new window with correct `cwd` field
   - Do a file operation (git status) - verify it runs in new repo

3. **Security test**:
   - Call `/api/session/new?repo_label=nonexistent` -> should 404
   - Call with `window_name=; rm -rf /` -> server sanitizes to empty, uses fallback

4. **Duplicate name test**:
   - Create window named "test"
   - Create another with same name -> should get "test-XXXX" suffix
   - Both windows appear in target list

5. **Path validation test**:
   - Add repo with non-existent path to config
   - Try to create window -> should 400
