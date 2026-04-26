# LLM On-Switching — GPU Orchestration Plan

RTX 5090 — 32 GB VRAM, single GPU. All models compete for the same card.

---

## 1. The Problem

Two projects (SecondBrain, geo-cv) share one GPU with 9 models that cannot coexist. Today's state:

- SecondBrain's `model_manager.py` handles swapping, but it's embedded in SecondBrain's API — geo-cv can't use it.
- Models started via raw `systemctl` get killed by SecondBrain's idle reaper within 60s (no idle tracker record).
- Orphaned vLLM EngineCore processes survive parent crashes and silently hold VRAM (fixed via `KillMode=control-group`).
- Service file copies in `~/.config/systemd/user/` drift from repo sources — fixes don't propagate until manual re-install.
- The Qwen3.5-9B Mamba model degrades after ~40 requests and needs periodic restarts — no orchestrator support for this.

## 2. Model Inventory

| Model | Size | VRAM | Used By | Port | Mode | Frequency |
|---|---|---|---|---|---|---|
| Qwen3.5-27B-AWQ | 27B (AWQ) | ~30 GB | SecondBrain chat/tools/finance | 8001 | LLM | Primary, always needed |
| Qwen3.5-35B-A3B-AWQ | 35B MoE (AWQ) | ~29 GB | SecondBrain alt fast | 8001 | LLM | Inactive |
| **Qwen3.5-9B** | **9B (bf16, Mamba)** | **~22 GB** | **geo-cv listing→GSV VLM** | **8002** | **LLM** | **Batch + production** |
| DeepSeek-R1-Qwen3-8B | 8B (FP8) | ~26 GB | SecondBrain JSON fallback | 8002 | LLM | Rare |
| Qwen3-VL-8B-Instruct | 8B | ~26 GB | SecondBrain doc OCR | 8003 | VLM | Per-sweep (~5 min) |
| Qwen3-VL-30B-A3B-AWQ | 30B MoE (AWQ) | ~29 GB | geo-cv verification (legacy) | 8001 | VLM | Deprecated by 9B |
| Qwen3-VL-Embedding-8B | 8B | ~28 GB | geo-cv vision embeddings | 8001 | Embed | Batch precompute |
| Qwen2.5-VL-7B-Instruct | 7B | ~14 GB (0.85) | geo-cv Qwen embeddings | 8001 | Pooling | Batch precompute |
| Qwen3-Embedding-0.6B | 0.6B | CPU | Both (text embed) | 8020 | Embed | Always-on |

### VRAM Conflict Rules

```
Cannot co-exist (VRAM):
  27B (30 GB) + 9B (22 GB) = 52 GB > 32 GB
  27B (30 GB) + 8B VL (26 GB) = 56 GB > 32 GB
  Any two GPU models exceed 32 GB except embed-cpu (0 GPU)

Can co-exist:
  Any single GPU model + Qwen3-Embedding-0.6B on CPU
  Any single GPU model + Qwen2.5-VL-7B in-process (if VRAM fits)
```

### Port Map

```
Port 8001 (mutually exclusive):
  SecondBrain:  Qwen3.5-27B-AWQ        ← primary chat
  SecondBrain:  Qwen3.5-35B-A3B        ← alt chat
  geo-cv:       Qwen3-VL-Embedding-8B  ← vision embeddings (batch)
  geo-cv:       Qwen3-VL-30B-A3B       ← legacy VLM (deprecated)

Port 8002 (conflicts with 8001 on VRAM):
  geo-cv:       Qwen3.5-9B             ← listing→GSV VLM (production)
  SecondBrain:  DeepSeek-R1-8B         ← JSON fallback (rare)

Port 8003:  Qwen3-VL-8B (SecondBrain doc OCR, conflicts on VRAM)
Port 8020:  Qwen3-Embedding-0.6B (always-on CPU, no conflicts)
```

## 3. Architecture

**Key insight**: The model manager must live in `llm-infra/`, not SecondBrain. Both repos are GPU clients — neither should own the infrastructure.

### Target Layout

```
~/dev/tools/llm-infra/
  services/                ← canonical systemd units for all vLLM instances
    gpu-orchestrator.service  ← NEW: always-on, ~20 MB footprint
  scripts/                 ← existing install/download/CUDA scripts
  orchestrator/            ← NEW: standalone GPU orchestrator
    server.py              ← FastAPI: /ensure, /status, /stop, /ping
    model_manager.py       ← extracted from SecondBrain, generalized
    config.yaml            ← slot definitions, ports, conflict groups
    requirements.txt       ← fastapi, uvicorn, httpx
```

### Orchestrator API (port 8099)

```
POST /ensure?slot=fast         → stop conflicts, start model, return when healthy
POST /stop?slot=fast           → stop a specific model
POST /ping?slot=fast           → update idle tracker (prevents reaper kill)
GET  /status                   → all slots: state, idle time, model name, VRAM
POST /config                   → update idle timeout, default slot at runtime
```

### Slot Configuration

```yaml
default_slot: fast
idle_timeout_seconds: 900       # 15 min (SecondBrain sweep needs ~6 min margin)

slots:
  fast:
    service: vllm-fast-27b
    health_url: http://127.0.0.1:8001/health
    conflict_group: gpu

  fast_alt:
    service: vllm-fast-q35
    health_url: http://127.0.0.1:8001/health
    conflict_group: gpu

  reason:
    service: vllm-reason
    health_url: http://127.0.0.1:8002/health
    conflict_group: gpu

  vision:
    service: vllm-vision
    health_url: http://127.0.0.1:8003/health
    conflict_group: gpu

  vision_embed:
    service: vllm-embed-qwen3vl
    health_url: http://127.0.0.1:8001/health
    conflict_group: gpu
    # Note: geo-cv uses Qwen2.5-VL-7B in-process (HuggingFace) for embeddings,
    # not this vLLM slot. This slot is for SecondBrain or large batch precompute only.

  geocv_vlm:
    service: vllm-geocv-9b
    health_url: http://127.0.0.1:8002/health
    conflict_group: gpu
    idle_timeout_seconds: 600     # 10 min for batch jobs
    restart_interval: 40          # auto-restart every 40 requests (Mamba degradation)

  embed:
    service: embed-cpu
    health_url: http://127.0.0.1:8020/health
    conflict_group: null          # no GPU conflict, always-on
```

## 4. What Already Exists

### `~/dev/tools/llm-infra/`

- 7 systemd service files in `services/`
- Port mapping documented in README
- Install scripts (CUDA, vLLM build, model downloads)
- `start-all.sh` (Postgres, Paperless, embed-cpu, Qwen3.5, API, web)
- `~/.local-llm.env` (shared env vars for endpoint discovery)

### `secondbrain/.../model_manager.py`

- `ensure_model(type)` — start if not running, stop GPU conflicts first
- `is_running(type)` — public method to check model state (replaces private `_models`/`_is_active` access)
- Conflict groups — `_LLM_TYPES` (fast/reason/vision) mutually exclusive
- Idle reaper — background loop every 60s, stops idle models
- External discovery — detects models started via `systemctl`
- `ModelBackend` abstraction for start/stop/is_active
- Pre-reap sweep — runs pending work before stopping fast model
- **Cleanup done (2026-03-26):** All callers migrated to public `is_running()` API — no more private `_models`/`_is_active` access. Ready for orchestrator extraction.

### Gap Analysis

| Need | Status | Gap |
|---|---|---|
| Centralized service files | Done (Phase 1) | 10 files in `llm-infra/services/`, all deployed as symlinks |
| Start/stop via systemd | Done | `model_manager.py` uses systemd backend |
| Conflict detection (GPU) | Done | `_LLM_TYPES` in model_manager |
| Idle timeout + auto-stop | Done | Reaper loop |
| Clean public API on model_manager | Done | `is_running()` public, no private access from callers |
| Auto-swap-back to default | Done (Phase 2) | Reaper auto-starts default_slot after reaping |
| HTTP endpoint for cross-repo | Done (Phase 2) | `POST http://localhost:8099/ensure?slot=` |
| geo-cv models in orchestrator | Done (Phase 2) | `geocv_vlm` slot in config.yaml |
| Orchestrator as standalone | Done (Phase 2) | `gpu-orchestrator.service` on port 8099 |
| Mamba restart_interval | Done (Phase 2) | `/ping` returns `restart_needed` at threshold |
| SecondBrain calls orchestrator | Done (Phase 3) | `OrchestratorBackend` in model_backend.py, reaper removed |
| geo-cv calls orchestrator | Done (Phase 4) | `_maybe_restart_vlm()` calls /ping, /stop, /ensure on 8099 |

## 5. Implementation Plan

### Phase 1: Service file consolidation — DONE (2026-03-30)

All 10 service files consolidated in `llm-infra/services/` as single source of truth:

- `DO_NOT_TRACK=1` on all 10 files
- `KillMode=control-group` on all 9 vLLM files (embed-cpu has no KillMode, CPU-only)
- `TimeoutStopSec=60` on all vLLM files (embed-cpu keeps 10)
- All Conflicts= lines updated to include vllm-fast-27b and vllm-vision
- All paths normalized to `llm-infra/.venv-vllm-0.17` and `llm-infra/models/` (symlinks to secondbrain)
- 3 new files: `vllm-fast-27b.service`, `vllm-vision.service`, `vllm-geocv-9b.service`
- `install-services.sh` updated (10 services, removed tuvok-api)
- `start-all.sh` no longer symlinks from secondbrain — calls `install-services.sh` instead
- `README.md` updated with full port mapping
- All 10 deployed as symlinks via `install-services.sh`
- SecondBrain `profile.json` references same service names — no changes needed
- Running processes (geo-cv eval, embed-cpu) unaffected

### Phase 2: Standalone orchestrator — DONE (2026-03-30)

Created `llm-infra/orchestrator/` with config-driven GPU model lifecycle management:

- `model_manager.py` — generalized from SecondBrain's, config-driven slots (not enum)
  - `ensure()` — stop GPU conflicts, start service, wait for health
  - `stop()` — stop a slot
  - `ping()` — update idle tracker, returns `restart_needed` when restart_interval exceeded
  - `get_status()` — all slots with state, idle time, request count
  - `update_config()` — runtime idle timeout and default slot changes
  - Idle reaper — discovers external starts, reaps idle models, auto-starts default_slot after reap
  - `restart_interval` support for Mamba degradation (geocv_vlm: every 40 requests)
- `server.py` — FastAPI on port 8099 (~60 lines): /ensure, /stop, /ping, /status, /config, /health
- `config.yaml` — 9 slots (fast, fast_alt, fast_legacy, reason, vision, vision_embed, vlm_legacy, geocv_vlm, embed)
- `requirements.txt` — fastapi, uvicorn, httpx, pyyaml
- `gpu-orchestrator.service` — always-on systemd service, Restart=always
- Added to `install-services.sh` (auto-enabled on boot)
- Verified: all endpoints responding, detects embed-cpu as running, reaper active

### Phase 3: SecondBrain integration

Replace SecondBrain's `model_manager.py` internals with a thin HTTP client that preserves the existing public API.

#### What changes

**1. New `OrchestratorBackend` replaces `SystemdBackend`**

Add a new `ModelBackend` subclass that delegates to the orchestrator. The `ModelBackend` abstraction already exists (`model_backend.py`) with `SystemdBackend` (Linux) and `ExternalBackend` (macOS/remote). The orchestrator client is a third backend:

**Slot name vs service name:** The `ModelBackend` interface passes `service` (the systemd unit name, e.g., `"vllm-fast-27b"`), but the orchestrator expects slot names (`"fast"`). Rather than add a reverse lookup to the orchestrator, change model_manager.py to pass the **slot name** (the `ModelType` value) to the backend. This is a one-line change at each call site inside model_manager:

```python
# Before (passes service name):
await self._backend.start(info.service)
# After (passes slot name):
await self._backend.start(model_type.value)  # "fast", "reason", "vision"
```

`SystemdBackend` needs the service name, `OrchestratorBackend` needs the slot name. Resolve with a slot→service mapping on the backend:

```python
class OrchestratorBackend(ModelBackend):
    """Delegate lifecycle to the standalone GPU orchestrator (port 8099)."""

    def __init__(self, base_url: str = "http://localhost:8099"):
        self._base = base_url
        self._client = httpx.AsyncClient(base_url=base_url, timeout=5.0)

    async def start(self, slot: str) -> bool:
        resp = await self._client.post("/ensure", params={"slot": slot}, timeout=120)
        return resp.status_code == 200

    async def stop(self, slot: str) -> bool:
        resp = await self._client.post("/stop", params={"slot": slot}, timeout=30)
        return resp.status_code == 200

    def is_active(self, slot: str) -> bool:
        import httpx as httpx_sync
        try:
            resp = httpx_sync.get(f"{self._base}/status", timeout=5)
            if resp.status_code != 200:
                return False
            return resp.json().get(slot, {}).get("status") == "running"
        except httpx_sync.HTTPError:
            return False

    async def assign(self, slot: str, profile: "ModelProfile") -> None:
        # Model assignment still needs local service file rewrite —
        # orchestrator doesn't own service file contents.
        from app.services.model_catalog import write_service
        write_service(slot, profile)

    async def ping(self, slot: str) -> None:
        """Update orchestrator idle tracker."""
        try:
            await self._client.post("/ping", params={"slot": slot}, timeout=5)
        except httpx.HTTPError:
            pass  # fire-and-forget

    async def close(self):
        await self._client.aclose()
```

And `SystemdBackend` wraps the slot→service lookup internally:

```python
class SystemdBackend(ModelBackend):
    def __init__(self, slot_services: dict[str, str]):
        # e.g. {"fast": "vllm-fast-27b", "reason": "vllm-reason", "vision": "vllm-vision"}
        self._services = slot_services

    async def start(self, slot: str) -> bool:
        return self._systemctl("start", self._services[slot])

    async def stop(self, slot: str) -> bool:
        return self._systemctl("stop", self._services[slot])

    def is_active(self, slot: str) -> bool:
        return self._check_active(self._services[slot])
    # ...
```

This makes the `ModelBackend` interface slot-oriented (not service-oriented), which is the right abstraction. Both backends receive `"fast"` and resolve it to their own mechanism — orchestrator HTTP or systemctl.

Key design notes:
- **Shared `httpx.AsyncClient`** with connection pooling — no per-call client creation.
- **`is_active()` reads `/status` response** — flat dict keyed by slot name, each with `"status": "running"/"stopped"/"starting"`. NOT nested under `"slots"`.
- **`assign()` stays local** — the orchestrator manages lifecycle (start/stop) but doesn't own service file contents. Model assignment (rewriting systemd units) remains a SecondBrain concern via `model_catalog.write_service()`.
- **`ping()` added** — new method not in base `ModelBackend`. Called after each LLM request to update idle tracking.

**2. Backend selection in `create_default_backend()`**

```python
def create_default_backend() -> ModelBackend:
    profile = get_profile()
    if profile.platform == "linux" and _orchestrator_reachable():
        return OrchestratorBackend()
    if profile.platform == "linux":
        return SystemdBackend()  # fallback if orchestrator not installed yet
    return ExternalBackend()
```

This allows gradual migration — `SystemdBackend` still works if orchestrator isn't running. `ExternalBackend` (macOS) is unaffected.

**3. Remove the reaper loop from model_manager**

The orchestrator owns idle tracking and reaping. Remove from `model_manager.py`:
- The 60s `_reaper_loop` background task
- `_reap_idle_models()`

**But** keep the sweep logic accessible. The `pre_reap_sweep` and `post_reap_vision_sweep` contain SecondBrain business logic (invoice extraction, categorization, matching, vision batch). These are not model management — they're application-level workflows that happen to coincide with model lifecycle.

**Move sweep orchestration to `heartbeat.py`:**

Currently `full_sweep` already lives in `heartbeat.py` and runs on a 3 AM cron. But the reaper also triggers mini-sweeps opportunistically before stopping idle models. After removing the reaper:
- The **3 AM `full_sweep`** continues unchanged — it calls `ensure_model("fast")`, runs LLM work, calls `ensure_model("vision")`, runs vision batch, then lets the orchestrator reap when idle.
- **Opportunistic mini-sweeps are lost** — the reaper's "drain pending work before stopping" behavior goes away. This is acceptable: the 3 AM sweep catches everything, and interactive work (chat, manual upload) calls `ensure_model` on demand.
- If opportunistic sweeps are needed later, add a heartbeat beat that runs every 15 min and checks for pending work. Separate concern from model lifecycle.

**4. `model_names` dict — keep reading from profile**

`model_manager.model_names[ModelType.FAST]` returns the served model name (e.g., `"Qwen/Qwen3.5-27B"`). Callers use this to construct chat completion requests. This comes from `profile.json` capabilities, not from the orchestrator. **Keep it as-is** — read from profile at init time. The orchestrator doesn't need to know or return model names.

**5. Add `/ping` calls after LLM requests**

Each LLM call site should ping the orchestrator to update idle tracking:

```python
result = await llm_call(...)
await model_manager.ping("fast")  # fire-and-forget, via OrchestratorBackend
```

Add `ping()` as a pass-through on `ModelManager`:

```python
async def ping(self, model_type: ModelType) -> None:
    info = self._models.get(model_type)
    if info and hasattr(self._backend, "ping"):
        await self._backend.ping(info.service)
```

**6. Fallback when orchestrator is down**

Decision: trust `Restart=always` on `gpu-orchestrator.service` (Q1 option a). If orchestrator is unreachable, `ensure_model()` returns `False` and callers handle it the same way they handle "model failed to start" today. No direct systemctl fallback — that reintroduces the reaper-kills-external-starts problem.

The `create_default_backend()` check at startup provides a softer fallback: if the orchestrator isn't installed/running when SecondBrain starts, it falls back to `SystemdBackend`. But once `OrchestratorBackend` is selected, it stays — no runtime fallback to systemctl.

#### What does NOT change

- **Call sites** — all 8 callers already use the public API (`ensure_model`, `is_running`). The slot mapping is internal to model_manager. No caller changes needed.
- **`assign_model()`** — stays in model_manager. Calls `model_catalog.write_service()` locally, then delegates start/stop to orchestrator backend. Used for runtime model swaps (e.g., switching fast from 27B to 35B).
- **`model_names` dict** — populated from `profile.json` at init. Not affected by backend change.
- **`model_backend.py`** — `SystemdBackend` and `ExternalBackend` remain for non-orchestrator environments. `OrchestratorBackend` is added alongside them.
- **heartbeat.py** — `full_sweep` still runs on cron. It calls `ensure_model("fast")` and `ensure_model("vision")` as before — those now go through the orchestrator.
- **`.env` config** — `FAST_MODEL_NAME`, `FAST_SERVICE` still used for LLM endpoint URL construction. The orchestrator handles service lifecycle, but SecondBrain still needs to know which port to send chat completions to.

#### Call sites (no changes needed, for reference)

| File | Uses | Model type |
|---|---|---|
| `brain_router.py` | `ensure_model("fast")` | chat, tool calling, retry |
| `invoice_extractor.py` | `ensure_model("fast")` | invoice field extraction |
| `finance_llm_categorization.py` | `ensure_model("fast")` | transaction categorization |
| `finance_llm_matching.py` | `ensure_model("fast")` | transaction matching |
| `finance_counterparty_resolution.py` | `ensure_model("fast")` | counterparty resolution |
| `entity_resolution.py` | `ensure_model("fast")` | entity dedup |
| `document_vision.py` | `ensure_model("vision")` | batch doc OCR |
| `memory_extractor.py` / `memory_consolidator.py` | `ensure_model("fast")` | memory ops |
| `heartbeat.py` (`full_sweep`) | `ensure_model("fast")`, `ensure_model("vision")` | 3 AM sweep |

#### Risks

- **Sweep atomicity (Q4)** — `full_sweep` does fast→vision→stop in sequence (~6 min). With an external orchestrator, geo-cv could steal the GPU between steps. In practice the 15-min idle timeout makes this unlikely — geo-cv batches are long-running and won't start mid-sweep by coincidence. If it becomes a problem, the lease concept (Q4 option a) can be added later.
- **Orchestrator latency** — adds ~5ms per `/ensure` call (localhost HTTP). Negligible vs model startup time (~35s).
- **Lost opportunistic sweeps** — the reaper's "drain work before stopping" is removed. The 3 AM sweep covers this, but pending work between sweeps waits longer. Acceptable tradeoff; add a periodic heartbeat beat later if needed.

#### Estimated scope

~100 lines removed (reaper loop, `_reap_idle_models`, `_pre_reap_sweep` trigger). ~80 lines added (`OrchestratorBackend`, `ping()` pass-through, backend selection). `SystemdBackend` and `ExternalBackend` stay. Medium effort, low risk if orchestrator is stable. Can be done in one session.

### Phase 4: geo-cv integration — DONE (2026-04-01)

Replaced geo-cv's subprocess-based vLLM management with orchestrator HTTP calls in `src/pipeline_v2/orchestrator.py` (lines 6205-6282).

#### What exists today

The eval pipeline (`eval_validated_listings.py` / orchestrator module) has:
- `_maybe_restart_vlm()` — kills vLLM via `pkill -f vllm`, starts via `subprocess.Popen`, waits for health
- Auto-restart every ~40 VLM calls (Mamba state corruption detection)
- Health check wait loop after restart
- Hardcoded vLLM command-line args, paths, and ports

**Problem:** `pkill` doesn't kill orphaned EngineCore processes. When the API server dies but EngineCore survives (reparented to PID 1), the restart launches a second EngineCore that fails to bind the GPU or silently shares VRAM. This caused 2 ghost VRAM failures on 2026-03-30 alone.

#### What changes

| Current (subprocess) | Phase 4 (orchestrator) | Benefit |
|---|---|---|
| `pkill -f vllm` | `POST /stop?slot=geocv_vlm` | Clean shutdown via `systemctl stop` — `KillMode=control-group` kills ALL cgroup processes including orphan EngineCores |
| `subprocess.Popen(vllm...)` | `POST /ensure?slot=geocv_vlm` | Orchestrator handles GPU conflicts (stops 27B first if running), waits for health |
| No idle tracking | `POST /ping` every ~10 listings | Prevents orchestrator reaper from killing 9B mid-batch |
| Ghost VRAM on failure | Orchestrator uses systemd | `Restart=always` + `KillMode=control-group` eliminates ghost VRAM |
| Hardcoded paths/ports | Read from orchestrator `/status` | Single source of truth for model endpoints |

#### Implementation

**1. Replace `_maybe_restart_vlm()` with orchestrator calls:**

```python
import httpx

ORCHESTRATOR = "http://localhost:8099"

def ensure_vlm() -> bool:
    """Start geo-cv VLM via orchestrator. Blocks until healthy."""
    resp = httpx.post(f"{ORCHESTRATOR}/ensure?slot=geocv_vlm", timeout=120)
    return resp.status_code == 200 and resp.json().get("ok", False)

def stop_vlm() -> bool:
    """Stop geo-cv VLM via orchestrator. Clean cgroup shutdown."""
    resp = httpx.post(f"{ORCHESTRATOR}/stop?slot=geocv_vlm", timeout=30)
    return resp.status_code == 200

def ping_vlm() -> dict:
    """Update idle tracker. Returns restart_needed if threshold exceeded."""
    resp = httpx.post(f"{ORCHESTRATOR}/ping?slot=geocv_vlm", timeout=5)
    return resp.json() if resp.status_code == 200 else {}
```

**2. Eval loop integration:**

```python
# Before scoring batch
ensure_vlm()

for i, listing in enumerate(listings):
    # Score listing via VLM on port 8002
    result = score_listing(listing)

    # Ping every 10 listings to prevent reaper kill
    if i % 10 == 0:
        ping_result = ping_vlm()

    # Mamba degradation restart every ~40 listings
    # geo-cv initiates explicitly — it knows when degradation happens (Q3 option c)
    if i > 0 and i % 40 == 0:
        stop_vlm()
        ensure_vlm()

# After batch — let orchestrator reap and swap back to default
```

**3. Fallback when orchestrator is down:**

If orchestrator is unreachable, fall back to current subprocess method. Unlike SecondBrain (where fallback reintroduces the reaper conflict), geo-cv has no reaper — direct subprocess is safe as a degraded mode. Log a warning.

```python
def ensure_vlm() -> bool:
    try:
        resp = httpx.post(f"{ORCHESTRATOR}/ensure?slot=geocv_vlm", timeout=120)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
    except httpx.HTTPError:
        logger.warning("Orchestrator unreachable, falling back to subprocess")
    return _start_vlm_subprocess()  # existing method, kept as fallback
```

**4. Remove hardcoded paths/ports:**

The VLM endpoint URL (`http://127.0.0.1:8002/v1`) can be read from orchestrator `/status` response (the `geocv_vlm` slot knows its health URL). But for simplicity, keep the port hardcoded in geo-cv config — it's stable and matches `config.yaml`.

#### Embedding precompute (batch, not production)

For large-batch embedding precompute (thousands of images via vLLM pooling mode):

```python
httpx.post("http://localhost:8099/ensure?slot=vision_embed", timeout=120)
# ... run batch ...
httpx.post("http://localhost:8099/stop?slot=vision_embed", timeout=30)
```

Per-listing production embedding uses in-process HuggingFace (no orchestrator needed).

#### Open question answers from geo-cv experience

**Q2 (concurrent /ensure):** Option (a) with timeout. geo-cv batch scoring can wait 2 minutes for the GPU. If SecondBrain is mid-sweep, geo-cv retries. Batch is not latency-sensitive.

**Q3 (Mamba restart vs in-flight):** Option (c). geo-cv initiates restarts explicitly. It knows exactly when degradation happens (every ~40 calls). The orchestrator doesn't need to track Mamba state — geo-cv calls stop + ensure when it detects the threshold.

#### Estimated scope

~50 lines removed (`_maybe_restart_vlm()` subprocess logic, hardcoded vLLM paths). ~40 lines added (orchestrator HTTP client, ping loop, fallback). Low risk — orchestrator is already running and tested. The subprocess fallback means no regression if orchestrator is down.

### Phase 5: Cleanup — DONE (2026-04-02)

- Removed `fast_legacy` slot (Qwen3-32B-AWQ) from orchestrator config — replaced by 27B
- Removed `vlm_legacy` slot (Qwen3-VL-30B-A3B) from orchestrator config — deprecated by 9B + remote APIs
- Service files (`vllm-fast.service`, `vllm-vlm-vl.service`) kept on disk but unregistered in orchestrator
- `DeepSeek-R1-8B` (`reason` slot) kept — still referenced by SecondBrain as JSON fallback; evaluate removal later based on usage logs
- Symlink deployment already done in Phase 1
- SecondBrain `start-all.sh` and `install-services.sh` already updated (done in separate session) — no longer references `infra/systemd-user/` for vLLM services
- Orchestrator restarted, 7 active slots confirmed

## 6. Open Questions

### Q1: Orchestrator availability — DECIDED: option (a)

Trust `Restart=always` — downtime is ~3s, acceptable. SecondBrain falls back to `SystemdBackend` if orchestrator is down at startup. geo-cv falls back to subprocess method if orchestrator is unreachable mid-batch.

### Q2: Concurrent `/ensure` — DECIDED: option (a) with timeout

First-come-first-served. geo-cv batch scoring can wait 2 minutes for the GPU. If SecondBrain is mid-sweep, geo-cv blocks on `/ensure` until the slot is free. Batch is not latency-sensitive. The orchestrator's per-slot `asyncio.Lock` already provides this behavior.

### Q3: Mamba restart vs in-flight requests — DECIDED: option (c)

geo-cv initiates restarts explicitly. It knows exactly when degradation happens (every ~40 VLM calls). The orchestrator doesn't need to track Mamba state — geo-cv calls `stop` + `ensure` when it detects the threshold. The `restart_interval` field in config.yaml is informational; `/ping` returns `restart_needed: true` as a hint, but the client decides when to act.

### Q4: Sweep atomicity — DECIDED: option (b), revisit if needed

SecondBrain-specific concern. The sweep (fast→vision→stop, ~6 min) is a SecondBrain workflow managed by `heartbeat.py`. The 15-min idle timeout provides sufficient margin — geo-cv batches are long-running and won't start mid-sweep by coincidence. If it becomes a problem, add a lease concept later.

### Q5: Should `embed-cpu` be orchestrator-managed? — DECIDED: option (b)

Keep in orchestrator for `/status` visibility only. `embed-cpu` is SecondBrain-specific (text embeddings for RAG), always-on, CPU-only, no GPU conflicts. The orchestrator never stops or swaps it — it just shows up in `/status` so operators can see the full picture.

### Q6: vLLM venv ownership — RESOLVED: option (c)

All service files now reference `llm-infra/.venv-vllm-0.17/` which is a symlink to `secondbrain/.venv-vllm-0.17/`. Single install point, no duplication. Applied in Phase 1.

### Q7: Service file symlinks vs copies — RESOLVED

`install-services.sh` now uses `ln -sf` (symlinks). Tested and working on WSL2. All 11 service files are symlinks from `~/.config/systemd/user/` → `llm-infra/services/`. `daemon-reload` picks up changes after editing the source. Applied in Phase 1.

## 7. Operational Reference

### SecondBrain Sweep GPU Lifecycle

Observed sequence (2026-03-26, ~6 minutes total):
1. Cold-start fast model (27B, ~35s startup + ~33s JIT)
2. Invoice extraction (~30s)
3. LLM matching (~5s)
4. Categorization drain (up to 20 rounds x ~5s = ~100s)
5. Auto-triage (~1s)
6. Stop fast model
7. Start vision model (8B-VL, ~20s startup)
8. Vision batch (16 docs x ~20s = ~320s)
9. Stop vision model

The orchestrator must handle stop-fast→start-vision→stop-vision atomically without the reaper interfering mid-swap.

### geo-cv Batch Scoring Lifecycle

Full eval run (648 listings, ~4-5 hours):
- ~130-180 listings/hr with CUDA graphs (0.65s/call effective)
- ~30-70 VLM calls per listing (3 images x 2 headings x up to 10 candidates)
- Early exit at score >= 85, evidence skip at >= 90
- Auto-restart every ~40 listings (Mamba state corruption)
- Cannot co-exist with SecondBrain 27B on same GPU

### Qwen3.5-9B Mamba Operational Notes

- `--max-cudagraph-capture-size 8` required for Mamba CUDA graphs
- `--mamba-cache-mode none` **hangs on inference** — DO NOT USE
- Server degrades after ~40-50 requests (Mamba state corruption)
- WSL2: orphan EngineCore processes leak VRAM; `KillMode=control-group` required

### geo-cv Production Runtime Flow

geo-cv needs **two models sequentially** per listing (cannot co-exist on 32GB):

1. **Qwen2.5-VL-7B** (~14GB) — extract Qwen 3584-dim embeddings for listing images + new GSV headings
2. **Qwen3.5-9B** (~22GB) — VLM Stage H: score listing→GSV matches

**Key decision:** The 7B runs **in-process via HuggingFace** (not vLLM). This eliminates one
orchestrator slot and avoids a model swap. Only the 9B needs vLLM (Mamba hybrid requires it).

| Model | Runtime | Why |
|-------|---------|-----|
| Qwen2.5-VL-7B | **In-process (HuggingFace)** | Load → embed → `del model` → free VRAM. Simpler, no orchestrator needed. |
| Qwen3.5-9B | **vLLM server** | Mamba-Transformer hybrid not supported by HuggingFace. Must use vLLM. |
| v21 dual encoder | **In-process (PyTorch)** | 3GB, runs alongside either model. |

```
New listing arrives
  │
  ├─ Embedding step (in-process, no orchestrator):
  │   ├─ model_7b = Qwen2_5_VL.from_pretrained(...)   # ~10s, 14GB
  │   ├─ Extract listing Qwen embeddings               # ~20s
  │   ├─ Extract listing CLIP embeddings               # dual encoder in-process, ~5s
  │   ├─ Fetch + embed missing GSV headings            # if any z19s uncached
  │   └─ del model_7b; torch.cuda.empty_cache()        # free 14GB
  │
  ├─ Pipeline (only VLM needs orchestrator):
  │   ├─ POST /ensure?slot=geocv_vlm                   # 9B on port 8002
  │   ├─ Stages C→G2: use cached embeddings            # no model needed
  │   ├─ Stage H: VLM rerank top-10                    # ~30 calls, ~20s
  │   └─ POST /ping?slot=geocv_vlm                     # keep alive for next listing
  │
  └─ After batch: POST /stop?slot=geocv_vlm            # default model resumes
```

**Latency:** ~2 min cold (7B load + 9B swap), ~30s warm (embeddings cached, VLM loaded).

**Batch optimization:** For N listings, load 7B once → embed all → unload → start 9B once →
run all pipelines. One model swap total, not N swaps.

### Future: TurboQuant KV Cache Compression

[github.com/0xSero/turboquant](https://github.com/0xSero/turboquant) — 3-bit key / 2-bit value
KV cache quantization with vLLM integration (ICLR 2026).

- RTX 5090 + Qwen3.5-27B-AWQ: **2x token capacity**, +5.7% prefill, 30GB KV freed
- Requires vLLM 0.18.0 (we're on 0.17.0)
- Only compresses full-attention layers — Mamba/linear-attention layers unaffected
- For Qwen3.5-9B (Mamba hybrid, ~50% attention): ~15-30% VRAM savings
- Could enable 27B + 9B coexistence if KV savings free enough VRAM
- Won't fix Mamba state degradation (different issue)

### Known Fixes Applied

- `KillMode=control-group` on all vLLM services (2026-03-26) — prevents orphaned EngineCore VRAM leaks.
- `--limit-mm-per-prompt '{"image": 4}'` JSON format (vLLM 0.17) — fixes vision model startup failure.
- `DO_NOT_TRACK=1` on all services — disables vLLM + HuggingFace telemetry.
