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
| Centralized service files | Done | `llm-infra/services/` |
| Start/stop via systemd | Done | `model_manager.py` uses systemd backend |
| Conflict detection (GPU) | Done | `_LLM_TYPES` in model_manager |
| Idle timeout + auto-stop | Done | Reaper loop |
| Clean public API on model_manager | Done | `is_running()` public, no private access from callers |
| **Auto-swap-back to default** | **Partial** | Reaper stops idle models but doesn't restart default |
| **HTTP endpoint for cross-repo** | **Missing** | `ensure_model()` only callable within SecondBrain |
| **geo-cv models in orchestrator** | **Missing** | Only fast/reason/vision/embed registered |
| **Orchestrator as standalone** | **Missing** | Embedded in SecondBrain's FastAPI app |
| **Mamba restart_interval** | **Missing** | No concept of periodic restarts |

## 5. Implementation Plan

### Phase 1: Service file consolidation (no code changes)

1. Add `DO_NOT_TRACK=1` environment to all `llm-infra/services/*.service` files.
2. Fix `--limit-mm-per-prompt` JSON format in `vllm-vlm-vl.service` (vLLM 0.17 change).
3. Ensure all services have `KillMode=control-group` and `TimeoutStopSec=60`.
4. Create `vllm-geocv-9b.service` for Qwen3.5-9B Mamba:
   ```ini
   [Unit]
   Description=vLLM geo-cv VLM (Qwen3.5-9B Mamba hybrid)
   After=network-online.target
   Conflicts=vllm-fast.service vllm-fast-q35.service vllm-reason.service vllm-vision.service

   [Service]
   Type=simple
   Environment="DO_NOT_TRACK=1"
   ExecStart=%h/dev/secondbrain/.venv-vllm-0.17/bin/python \
     -m vllm.entrypoints.openai.api_server \
     --model %h/dev/secondbrain/data/models/Qwen3.5-9B \
     --served-model-name Qwen/Qwen3.5-9B \
     --max-model-len 4096 --host 127.0.0.1 --port 8002 \
     --gpu-memory-utilization 0.70 --max-num-batched-tokens 4096 \
     --mamba-ssm-cache-dtype float32 --dtype bfloat16 \
     --max-cudagraph-capture-size 8
   KillSignal=SIGTERM
   KillMode=control-group
   TimeoutStopSec=60

   [Install]
   WantedBy=default.target
   ```
5. Run `install-services.sh` to deploy all files to `~/.config/systemd/user/`.

### Phase 2: Standalone orchestrator

**Prep done:** model_manager.py cleaned up — public `is_running()` method, all private `_models`/`_is_active` access removed from callers. Extraction surface is now clean.

1. Create `llm-infra/orchestrator/` directory.
2. Extract `model_manager.py` from SecondBrain:
   - Replace `ModelType` enum with config-driven slots from `config.yaml`.
   - Generalize `ensure_model()`, `stop_model()`, `record_request()`.
   - Add `restart_interval` support: track request count per slot, auto-stop→start when threshold reached.
   - Add swap-back: when a non-default model is reaped, auto-start `default_slot`.
3. Create `server.py` — minimal FastAPI (~100 lines):
   - `POST /ensure?slot=` — stop conflicts, start, wait for health, return.
   - `POST /stop?slot=` — stop model.
   - `POST /ping?slot=` — update idle tracker.
   - `GET /status` — all slots with state/idle/model/VRAM.
   - `POST /config` — runtime config updates.
4. Create `gpu-orchestrator.service`:
   ```ini
   [Unit]
   Description=GPU Model Orchestrator
   After=network.target

   [Service]
   Type=simple
   ExecStart=%h/dev/tools/llm-infra/orchestrator/venv/bin/uvicorn \
     orchestrator.server:app --host 127.0.0.1 --port 8099
   Restart=always
   RestartSec=3

   [Install]
   WantedBy=default.target
   ```
5. Add to `start-all.sh`.

### Phase 3: SecondBrain integration

Replace SecondBrain's `model_manager.py` (~250 lines) with a thin HTTP client:

```python
ORCHESTRATOR = "http://localhost:8099"

async def ensure_model(slot: str) -> bool:
    resp = await httpx.post(f"{ORCHESTRATOR}/ensure?slot={slot}", timeout=120)
    return resp.status_code == 200

async def stop_model(slot: str) -> bool:
    resp = await httpx.post(f"{ORCHESTRATOR}/stop?slot={slot}", timeout=30)
    return resp.status_code == 200

async def record_request(slot: str):
    await httpx.post(f"{ORCHESTRATOR}/ping?slot={slot}", timeout=5)
```

Call sites to update:
- `brain_router.py` — chat, tool calling, retry logic
- `invoice_extractor.py` — invoice field extraction
- `finance_llm_categorization.py` — transaction categorization
- `finance_llm_matching.py` — transaction matching
- `finance_counterparty_resolution.py` — counterparty resolution
- `entity_resolution.py` — entity dedup
- `document_vision.py` — batch doc OCR
- `memory_extractor.py` / `memory_consolidator.py`

### Phase 4: geo-cv integration

Add orchestrator preflight to batch scripts (no pipeline code changes):

```python
# Before scoring
httpx.post("http://localhost:8099/ensure?slot=geocv_vlm", timeout=120)

# During scoring (every ~10 listings)
httpx.post("http://localhost:8099/ping?slot=geocv_vlm", timeout=5)

# Mamba degradation restart (every ~40 listings)
httpx.post("http://localhost:8099/stop?slot=geocv_vlm", timeout=30)
httpx.post("http://localhost:8099/ensure?slot=geocv_vlm", timeout=120)

# After scoring — orchestrator auto-starts default_slot after idle timeout
```

For embedding precompute:
```python
httpx.post("http://localhost:8099/ensure?slot=vision_embed", timeout=120)
# ... run batch ...
httpx.post("http://localhost:8099/stop?slot=vision_embed", timeout=30)
```

### Phase 5: Cleanup

- Retire `Qwen3-VL-30B-A3B` local VLM (deprecated by 9B + remote APIs).
- Evaluate removing `DeepSeek-R1-8B` (rare JSON fallback — retry on 27B instead).
- Symlink service files from `llm-infra/services/` → `~/.config/systemd/user/` instead of copying, so fixes propagate immediately.

## 6. Open Questions

### Q1: Orchestrator availability

If `gpu-orchestrator.service` crashes, neither SecondBrain nor geo-cv can start models. Options:
- **a)** Trust `Restart=always` — downtime is ~3s, acceptable.
- **b)** Clients fall back to direct `systemctl` calls when orchestrator is unreachable. Adds complexity and reintroduces the reaper-kills-external-starts problem.
- **c)** Orchestrator is so simple (~100 lines) that crashes are unlikely. Monitor via systemd `OnFailure=` notification.

### Q2: Concurrent `/ensure` — who wins?

SecondBrain sweep calls `ensure?slot=fast` while geo-cv calls `ensure?slot=geocv_vlm` simultaneously. The orchestrator needs a mutex, but what's the policy?
- **a)** First-come-first-served — whoever grabs the lock wins, other blocks until slot is free.
- **b)** Priority-based — SecondBrain (interactive) preempts geo-cv (batch). Requires a priority field per slot.
- **c)** Reject with 409 Conflict — let the client retry. Simple but noisy.

### Q3: Mamba restart vs in-flight requests

`restart_interval: 40` triggers stop→start after 40 requests. If a VLM call is in-flight, it gets a connection error. Options:
- **a)** Orchestrator tracks in-flight count, waits for zero before restarting.
- **b)** geo-cv's responsibility — retry on 503/connection error. Simpler orchestrator.
- **c)** geo-cv initiates restarts explicitly (current Phase 4 pattern) instead of orchestrator auto-restarting.

### Q4: Sweep atomicity

SecondBrain sweep does stop-fast→start-vision→stop-vision in sequence (~6 min). If geo-cv calls `/ensure?slot=geocv_vlm` between steps 6 and 7, it steals the GPU. Options:
- **a)** Add a "lease" or "transaction" concept — `POST /lease?slots=fast,vision&duration=600` locks both slots for the caller.
- **b)** The 15-min idle timeout is sufficient — geo-cv batches are long-running and unlikely to start mid-sweep by coincidence.
- **c)** SecondBrain sweep calls `/ensure` for each step individually, and if preempted, retries after the other model is reaped.

### Q5: Should `embed-cpu` be orchestrator-managed?

Config shows it as a slot with `conflict_group: null`. It's always-on and has no GPU conflicts. Options:
- **a)** Remove from orchestrator config — leave as standalone systemd service. Less complexity, same outcome.
- **b)** Keep in orchestrator for unified `/status` visibility, but never stop/swap it.

### Q6: vLLM venv ownership

The `vllm-geocv-9b.service` references `secondbrain/.venv-vllm-0.17/`. If the orchestrator lives in `llm-infra/`, geo-cv's service depending on SecondBrain's venv is a coupling problem. Options:
- **a)** Move the vLLM venv to `llm-infra/.venv-vllm/` — all services reference it. Single install point.
- **b)** Each repo maintains its own venv — duplication but no cross-repo dependency.
- **c)** Symlink `llm-infra/.venv-vllm` → `secondbrain/.venv-vllm-0.17/` as a short-term bridge.

### Q7: Service file symlinks vs copies

Phase 5 proposes symlinks from `llm-infra/services/` → `~/.config/systemd/user/`. systemd resolves symlinks at `daemon-reload` time, so edits to the source propagate after reload. But the current approach uses copies via `install-services.sh`. Questions:
- Was there a specific reason copies were chosen over symlinks? (Permissions, SELinux, WSL quirk?)
- Does `systemctl --user` handle symlinks correctly on this WSL2 setup?
- Worth testing before committing to Phase 5.

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
