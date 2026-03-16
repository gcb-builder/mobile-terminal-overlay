# Mobile Terminal Overlay (MTO) — Internal Architecture Note v0.1

## Purpose

This document captures the current architectural reality of MTO, the intentional
constraints guiding design, and the next refactor boundary.

The goal is not to describe a future platform but to clarify the system as it
exists today so that future decisions remain consistent with its core principles.

---

## 1. System Identity

Mobile Terminal Overlay (MTO) is a **mobile-first control surface** for
coordinating multiple AI agents running in terminal sessions on a local machine.

It provides:

- Visibility into agent activity (phase, tools, permission waits)
- Lightweight orchestration of agent teams (launch, role injection, dispatch)
- Structured summaries of tool activity (activity timeline)
- Mobile-friendly control of terminal-based workflows

The system is intentionally **local-first, ephemeral, and zero-setup**.

---

## 2. Core Architectural Structure

The system separates into three functional planes.

### 2.1 Control Plane

Responsible for orchestration and coordination.

**Components:**
- FastAPI server (`server.py`)
- 17 modular routers (`routers/`) — team, team_launcher, logs, process, queue,
  runner, preview, challenge, context, env, files, git, mcp, push, scratch,
  snapshots, terminal_io
- Agent drivers (`drivers/`)
- Team launcher (8-phase pipeline)
- Status and activity endpoints

**Responsibilities:**
- Starting agents via drivers
- Sending commands to terminal sessions
- Monitoring readiness via driver polling
- Aggregating activity data for UI consumption

### 2.2 Agent Runtime

Responsible for executing the AI agents themselves.

**Current implementation:**
- tmux sessions (process isolation, window management)
- PTY interfaces (direct terminal I/O)
- Local CLI agents

**Agents currently supported:**
- Claude (structured JSONL, rich observability)
- Codex (session JSONL, approval events)
- Gemini (pane title signals, minimal logs)
- Generic CLI agents (heuristic fallback)

The runtime is local-only and intentionally simple. tmux currently acts as
process scheduler, terminal multiplexer, and interaction surface.

### 2.3 Observability Plane

Responsible for turning raw agent output into structured information.

**Sources:**
- JSONL logs (Claude: project logs, Codex: session logs)
- Pane title signals (Gemini: Unicode state icons, Claude: permission strings)
- Tool invocation summaries (extracted from assistant messages)
- Process detection (pgrep, log recency)

**Output — Activity Events (10 fields):**
```
{id, timestamp, ts_epoch, category, icon, title, detail, status, status_badge, tool_use_id}
```

**Categories:** tools, files, tests, git, errors

**Example transformation:**

Raw JSONL tool_use blocks:
```
Edit server.py  →  Bash: pytest
```

Structured timeline:
```
[files] Edit: server.py           OK
[tests] pytest                    ERR: 2 failed
[errors] Test failure detected    ERR
```

---

## 3. Key Architectural Constraints

These constraints are intentional and should guide all design decisions.

### 3.1 Mobile-First Interface

The UI must remain usable on a phone.

**Implications:**
- Large touch targets (>=44px)
- Shallow navigation (drawer tabs, not nested pages)
- Minimal simultaneous panels
- Summarized system state (badges, status pills)
- Progressive disclosure of detail

> Backend complexity must collapse into mobile-safe interaction primitives.

**Key UI patterns:** drawer tab system, command palette, floating action menu,
activity timeline, team cards.

### 3.2 Ephemeral System State

MTO intentionally has:
- No database
- No persistent state service
- No background infrastructure

State exists only in:
- Running processes (tmux sessions, agent PIDs)
- Filesystem (JSONL logs, config files, team role files)

**Benefits:** zero setup, no migrations, restart = recovery.

> The control plane is disposable. Avoid introducing components that must be
> "kept healthy" for MTO to function.

### 3.3 Heterogeneous Agent Observability

Different agents expose different levels of introspection:

| Agent   | JSONL Logs | Permission Signal | Phase Detection | Pane Title |
|---------|-----------|-------------------|-----------------|------------|
| Claude  | Yes       | Yes (pane title)  | Yes (JSONL)     | Yes        |
| Codex   | Yes       | Yes (JSONL event) | Yes (JSONL)     | No         |
| Gemini  | No        | Yes (regex/title) | Yes (title icons)| Yes       |
| Generic | No        | Heuristic only    | No              | No         |

The observability plane must normalize these uneven signals. Agent drivers
therefore define **both** launch behavior and observation strategy.

---

## 4. Agent Driver Architecture

Drivers are the central abstraction. The `AgentDriver` Protocol
(`drivers/base.py`) defines 9 methods grouped into three sub-contracts:

```python
class AgentDriver(Protocol):
    # --- Identity ---
    def id(self) -> str: ...
    def display_name(self) -> str: ...
    def config_dir_name(self) -> str: ...

    # --- Launch ---
    def start_command(self, startup_command=None) -> list[str]: ...
    async def is_ready(self, session, target, timeout, interval) -> bool: ...
    def ready_patterns(self) -> list[str]: ...

    # --- Observation ---
    def observe(self, ctx: ObserveContext) -> Observation: ...
    def capabilities(self) -> DriverCapabilities: ...
    def find_log_file(self, repo_path: Path) -> Optional[Path]: ...
```

**`observe()` is the heaviest method.** Each driver implements agent-specific
detection logic:

- **ClaudeDriver:** tail JSONL → parse tool_use blocks → detect phases
  (idle, planning, running_task, working, waiting) + pane title permission check
- **CodexDriver:** parse JSONL events (turn.started, item.started,
  approval-requested) → phases + token usage
- **GeminiDriver:** parse pane title Unicode icons
  (diamond=idle, sparkle/hourglass=working, hand=waiting) + regex fallback

**`capabilities()`** returns a frozen `DriverCapabilities` dataclass:
```python
@dataclass(frozen=True)
class DriverCapabilities:
    structured_logs: bool = False       # JSONL log support
    permission_detection: bool = False  # Can surface permission-wait state
    phase_detection: bool = False       # Idle/working/waiting phases
    pane_title_signal: bool = False     # tmux pane title signals
```

This allows the UI to degrade gracefully per agent. Capabilities are
immutable — they describe static driver traits, not runtime state.

| Agent   | structured_logs | permission_detection | phase_detection | pane_title_signal |
|---------|----------------|---------------------|-----------------|-------------------|
| Claude  | Yes            | Yes                 | Yes             | Yes               |
| Codex   | Yes            | Yes                 | Yes             | No                |
| Gemini  | No             | Yes                 | Yes             | Yes               |
| Generic | No             | No                  | No              | No                |

**Drivers:** ClaudeDriver, CodexDriver, GeminiDriver, GenericDriver.

---

## 5. Deliberate Non-Goals

### 5.1 Distributed Agent Infrastructure
MTO is a single-machine tool. No remote execution, cluster orchestration, or
distributed agent networks.

### 5.2 Persistent Backend Services
No Redis, message queues, relational databases, or background workers. These
would change the operational model and introduce maintenance overhead.

### 5.3 Platform-Level Agent Runtime
MTO is not an "agent operating system." It is a control interface for existing
CLI agents. Agent implementation and behavior remain external to MTO.

---

## 6. Current Fragility Points

### 6.1 Log Format Dependency
The observability system depends on JSONL log structure remaining stable.
Inconsistent logging across agents (and across agent versions) is the main
integration challenge.

### 6.2 Process Interaction Scatter (Resolved)

~~Process communication was scattered across 6+ modules.~~

**Resolved** by the `ProcessRuntime` refactor (commit `c316b64`). All PTY
lifecycle, I/O, and tmux command interactions now go through `TmuxRuntime`
(`runtime.py`). See section 7 for details.

**Remaining tmux calls outside runtime:** sync startup helpers in `helpers.py`
(run before the async event loop), read-only observation queries in drivers
and `team.py`, and `logs.py` capture-pane (uses `-J` flag). These are
intentionally out of scope — they're either pre-loop or read-only.

---

## 7. ProcessRuntime (Implemented)

`ProcessRuntime` Protocol + `TmuxRuntime` implementation in `runtime.py`.

**Interface (25 methods):**
- **State:** `master_fd`, `child_pid`, `session_name`, `has_fd`
- **PTY lifecycle:** `spawn()`, `terminate()`, `close_fd()`
- **Direct PTY I/O:** `pty_write()` (never modifies bytes), `pty_read()`,
  `write_command()` (only method that appends `\r`)
- **tmux send-keys:** `send_keys(target, *keys, literal=False)`
- **Window/pane management:** `new_window()`, `kill_window()`,
  `select_window()`, `select_pane()`
- **tmux queries:** `capture_pane()`, `display_message()`, `list_panes()`,
  `pipe_pane()`
- **Terminal size:** `set_size()`

**Key design decisions:**
- `@runtime_checkable` Protocol — no base class inheritance
- PTY vs tmux operations kept distinct (reflects real architecture)
- `has_fd` property hides implementation detail
- tmux target is a plain string (format: `session`, `session:window`,
  `session:window.pane`)
- Single concrete implementation (`TmuxRuntime`)

**Files migrated:** server.py, models.py, terminal_io.py, process.py,
runner.py, preview.py, team_launcher.py (net -216 lines).

### Next Refactor Boundary

The next pressure point is **observation normalization** — producing a
stable activity model from uneven agent outputs. This seam is already partially
addressed by the driver `observe()` + `capabilities()` pattern. The question
is whether a separate `ObservationAdapter` layer is needed or whether keeping
it driver-owned is sufficient.

---

## 8. Architectural Principles

1. **Mobile-first design** — all features must compress to phone-sized interaction
2. **Local-first execution** — agents run on the same machine as the control server
3. **Ephemeral state** — avoid persistent infrastructure; filesystem is truth
4. **Driver-based agent integration** — each agent defines its own launch and
   observation behavior
5. **Normalize only at the UI boundary** — internal agent differences are
   acceptable until presented to users
6. **Abstract only where duplication hurts** — build seams where code is already
   scattered or fragile, not speculatively

---

## 9. Summary

MTO is best understood as:

> A mobile-first control surface for coordinating terminal-based AI agents
> on a local machine.

Its design emphasizes simplicity, recoverability, observability, and low
operational overhead. Future evolution should remain grounded in these
constraints rather than expanding toward unnecessary platform complexity.
