26-04-25 / tags: post-mortem, transcriber, ollama, performance, gpu, inference

---

# Post-Mortem: Ollama Analysis Timeouts — Apr 25, 2026

## Summary

Analysis stage repeatedly failed with `Ollama analysis failed: timed out` across all 4 retry attempts. Two distinct failure modes cascaded:

1. **Cold start** — the Ollama daemon was not running when the transcriber session began. Initial attempts hit connection failures.
2. **Slow start** — once the user manually launched the Ollama macOS app, the model loaded successfully but inference was crippled. A trivial "Say hi" prompt took **96 seconds** end-to-end (verified via direct `curl` against `/api/chat`). A request from the pipeline hit `500 | 10m2s` server-side. Each pipeline attempt exceeded the 600s client timeout.

**Verdict:** Ollama silently degraded into a CPU-bound configuration because the default model load reserved a 262,144-token context window. The KV cache for that context exceeded available VRAM, forcing the output projection layer to spill onto CPU. Every generated token then required CPU↔GPU round-trips, reducing throughput to the point that even sub-second responses took ~90s. The pipeline's lack of a preflight check, model warmup, and explicit `num_ctx` mask the misconfiguration as a generic "timeout."

---

## Timeline

| Time (CEST) | Event |
|---|---|
| Session start | Pipeline starts a transcription session. Ollama daemon not running on host |
| T+0 | `configure_ollama()` constructs `_ollama_client`. No connectivity probe. No model warmup |
| T+~0 | Pipeline reaches analysis stage, calls `analyze_with_ollama()` → connection fails fast → "timed out" logged (attempt 1/4) |
| T+5s | Backoff sleep, attempt 2/4 → still no daemon → fail |
| T+15s | User launches Ollama.app; daemon comes up. Attempt 3/4 sent during model load |
| 08:39:53 | Server log: `llama runner started in 18.82 seconds` |
| 08:39:53 | Server log: `offloading 32 repeating layers to GPU` + `offloading output layer to CPU` (32/33 layers on GPU) |
| 08:40:44 | First chat request aborted by client after 30s — `aborting completion request due to client closing the connection` (`500 | 30.04s`) |
| 08:47:32 | Second chat request aborted at 2m — `500 | 2m0s` |
| 08:49:37 | Third request hangs for 10 minutes before client gives up — `500 | 10m2s` |
| 08:49:40 | Direct `curl` "Say hi" test (no thinking, tiny prompt) succeeds — `200 | 1m36s` (96 seconds) |
| 08:53 | `/api/ps` confirms model resident: `size_vram=17.7 GB`, `context_length=262144` |
| Diagnosis | Root cause identified: KV cache for 262k context spills the output layer to CPU |

---

## Root Causes

### RC-1: Default 262,144-token context window forces output layer onto CPU (primary)
The `qwen3.5:9b` model was loaded by Ollama using its baked-in default context length of **262,144 tokens**. The KV cache for a context this large consumes the majority of available VRAM (17.7 GB on an 18.9 GB GPU). Ollama's scheduler computed that not all 33 layers fit, so it left the output projection layer on CPU. Every token sampled requires copying activations from GPU → CPU for the final projection, then back. Throughput collapses by ~10–50× depending on the prompt size.

**Evidence:**
- `server.log`: `offloading output layer to CPU` and `offloaded 32/33 layers to GPU`
- `/api/ps`: `size_vram: 17,706,274,816` against `context_length: 262144`
- Direct `curl` benchmark for a 4-token response: 96 seconds

### RC-2: No preflight check on `_ollama_client` (primary)
`configure_ollama()` constructs the client object but never confirms the daemon is reachable, the model is pulled, or the model is loadable. Any of these failures only surface deep inside the retry loop — by which point the pipeline has already burned its retry budget on what is effectively an environmental error.

### RC-3: No explicit `num_ctx` in `analyze_with_ollama()` call (primary)
The `chat()` invocation does not pass `options={"num_ctx": ...}`. Ollama therefore uses the model's default — which is 262k for this build. A pipeline that knows its prompt size (transcripts are bounded by recording length × ~10 tokens/sec) should declare its working context, not inherit a model default sized for million-token use cases.

### RC-4: No model warmup after `configure_ollama()` (contributing)
Cold model load took 18.82 seconds in this session. The first chat request bears that load latency on top of inference latency. With no warmup, the first analysis of every session is the slowest — and it falls inside the same retry budget as steady-state attempts.

### RC-5: Retry schedule is mis-tuned for cold-start (contributing)
`analysis_retry_backoff: [5, 10, 30]` — 5s and 10s gaps assume transient API errors (rate limit, brief network hiccup). They are far too short to absorb a daemon coming online, model loading, and a slow first inference. The pipeline burns retries 1–3 before the system is even ready to respond.

### RC-6: Failure mode opacity (contributing)
The pipeline's only signal is `❌ Ollama analysis failed: timed out`. There is no distinction between *connection refused*, *model not loaded*, *model loading*, *inference slow*, or *quota error*. An operator cannot triage from the log alone — the diagnosis required reading Ollama's server log, querying `/api/ps`, and benchmarking with `curl`.

---

## What Did NOT Happen

- No transcripts were lost — transcription completed normally; only the analysis stage failed
- No state corruption — affected files remain processable on retry
- The Gemini fallback path was not triggered because `analysis_provider` is hard-set to `ollama` (no auto-failover exists)
- The 600s `ollama_timeout` is correctly wired through the httpx client — the failures were not due to a misconfigured client timeout, they were due to inference genuinely exceeding 600s under the spilled-layer condition

---

## Fixes — Recommended Implementation Order

### Fix 1 — Pin `num_ctx` in the analysis call (immediate, highest leverage)

In [pipeline.py](../../pipeline.py) `analyze_with_ollama()`, pass an explicit context window:

```python
response = _ollama_client.chat(
    model=OLLAMA_MODEL,
    messages=[...],
    think=OLLAMA_THINKING,
    options={"num_ctx": OLLAMA_NUM_CTX},   # NEW
    keep_alive=OLLAMA_KEEP_ALIVE,          # NEW (see Fix 4)
)
```

Add to [config.yaml](../../config.yaml):

```yaml
ollama_num_ctx: 16384       # transcripts rarely exceed ~8k tokens; 16k is comfortable headroom
ollama_keep_alive: "30m"    # keep model resident between scan cycles
```

Add to [config.py](../../config.py):

```python
OLLAMA_NUM_CTX: int = _cfg.get("ollama_num_ctx", 16384)
OLLAMA_KEEP_ALIVE: str = _cfg.get("ollama_keep_alive", "30m")
```

**Expected effect:** All 33 layers fit on GPU. Inference latency for a typical transcript drops from ~10+ minutes to a handful of seconds.

### Fix 2 — Preflight check + model warmup in `configure_ollama()`

Reachability and load-state should be verified once at startup, not discovered through retry exhaustion mid-pipeline:

```python
def configure_ollama() -> None:
    global _ollama_client
    if ANALYSIS_PROVIDER != "ollama":
        return
    _ollama_client = _ollama_lib.Client(host=OLLAMA_HOST, timeout=OLLAMA_TIMEOUT)

    # Preflight: daemon reachable?
    try:
        models = _ollama_client.list()
    except Exception as e:
        raise FatalAPIError(f"Ollama daemon unreachable at {OLLAMA_HOST}: {e}")

    # Preflight: model present?
    if not any(m.model == OLLAMA_MODEL for m in models.models):
        raise FatalAPIError(f"Model '{OLLAMA_MODEL}' not found. Run: ollama pull {OLLAMA_MODEL}")

    # Warmup: trigger load with the *same* num_ctx the pipeline will use,
    # so the load matches steady-state config.
    print(f"⏳ Warming up {OLLAMA_MODEL} (num_ctx={OLLAMA_NUM_CTX})...", flush=True)
    t0 = time.monotonic()
    _ollama_client.chat(
        model=OLLAMA_MODEL,
        messages=[{"role": "user", "content": "ping"}],
        think=False,
        options={"num_ctx": OLLAMA_NUM_CTX},
        keep_alive=OLLAMA_KEEP_ALIVE,
    )
    print(f"✅ Ollama ready ({OLLAMA_MODEL}, warmup {time.monotonic()-t0:.1f}s)", flush=True)
```

Failure here aborts the daemon at startup — same blast-radius semantics as the existing `FatalAPIError` for Gemini auth failures (3-tier error handling, see [CLAUDE.md](../../CLAUDE.md)).

### Fix 3 — Distinguish failure modes in the retry loop

Replace the generic exception print with classification:

```python
except _ollama_lib.ResponseError as e:
    print(f"   ❌ Ollama API error (HTTP {e.status_code}): {e.error}", flush=True)
except httpx.ConnectError as e:
    print(f"   ❌ Ollama unreachable — is the daemon running? ({e})", flush=True)
except httpx.ReadTimeout:
    print(f"   ❌ Ollama timed out after {OLLAMA_TIMEOUT}s — model may be CPU-bound", flush=True)
except Exception as e:
    print(f"   ❌ Ollama unexpected error: {type(e).__name__}: {e}", flush=True)
```

This collapses diagnosis time from "open server.log + query /api/ps + benchmark with curl" to "read the pipeline log."

### Fix 4 — Tune retry backoff for cold-start tolerance

`analysis_retry_backoff: [5, 10, 30]` assumes transient error semantics. With Ollama, the first failure is much more likely to be a still-loading model than a true error. Recommend two-tier strategy:

- **Cold-start retries** (attempts 1–2): backoff `[20, 40]` — gives the daemon and model time to come up
- **Steady-state retries** (attempts 3–4): backoff `[60, 180]` — handles genuine inference congestion

OR, with Fix 2 in place, the cold-start case is fully eliminated and the existing `[5, 10, 30]` becomes appropriate again.

### Fix 5 — Per-request context discharge (defensive)

Even with `num_ctx` pinned, long-running daemons can accumulate KV cache state across many requests. Pass `keep_alive` per request to control residency, and consider an explicit unload between recordings if memory pressure becomes a recurring issue:

```python
# After processing a batch, release the model
_ollama_client.generate(model=OLLAMA_MODEL, prompt="", keep_alive=0)
```

This trades cold-start latency (~20s next request) for guaranteed clean state. Worth doing when batch size > N, where N depends on observed VRAM growth.

---

## Outstanding Actions

| Action | Status | Owner | Why |
|---|---|---|---|
| Fix 1 (pin `num_ctx`, `keep_alive`) | ✅ Done — `num_ctx: 65536`, `timeout: 1800` | Human/Claude | GPU-resident; covers all realistic meetings. See Follow-Up section for derivation |
| Fix 2 (preflight + warmup) | ❌ Not implemented | Claude/dev | Surfaces environmental failures at startup; turns "4 mystery timeouts" into "1 actionable error" |
| Fix 3 (exception classification) | ❌ Not implemented | Claude/dev | Cuts MTTR for next incident — operator can triage from log alone |
| Fix 4 (backoff tuning) | ❌ Not decided | Human | If Fix 2 is implemented, current backoff is fine. If not, retune for cold-start |
| Fix 5 (per-batch unload) | ❌ Not decided | Human | Trade-off between memory hygiene and warm-cache latency; observe VRAM growth before deciding |
| Make Ollama daemon launch-on-login | ❌ Not done | Human | Prevents the cold-start case entirely. macOS: `Ollama.app` → Settings → Open at Login |
| Add `analysis_provider` failover (Ollama → Gemini/Claude) | ❌ Future | Future | When local model is unrecoverable, fall back to cloud rather than returning no analysis |

---

## Verification

After Fix 1 is deployed, expected log on next analysis:

```
📊 Analyzing transcript [Ollama] (attempt 1/4)...
✅ Analysis succeeded via Ollama (attempt 1/4)
```

Direct curl smoke test (should complete in <5s after fix):

```bash
time curl -s -X POST http://localhost:11434/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5:9b","messages":[{"role":"user","content":"Say hi"}],
       "think":false,"stream":false,"options":{"num_ctx":16384},"keep_alive":"30m"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['message']['content'])"
```

`/api/ps` should show all layers on GPU (no `output layer to CPU` line in `server.log` on next model load).

---

## Follow-Up: num_ctx Too Small for Long Recordings — Apr 25, 2026

**Status: ongoing — no settled fix.**

Fix 1 was implemented as described above: `ollama_num_ctx: 16384`. The KV cache spill was resolved for normal-length meetings. However, on the same day, a **72-minute recording** produced 4 consecutive `timed out after 600s` failures — same surface symptom, different root cause.

**New root cause:** The assumption in Fix 1 — *"transcripts rarely exceed ~8k tokens; 16k is comfortable headroom"* — was wrong for long recordings. A 72-minute meeting at ~2 tokens/second produces roughly 8,600 transcript tokens. With the analysis prompt (~1,500 tokens) and thinking-mode output (2,000–4,000 tokens before the response), total context demand approaches or exceeds 16K. The model hits its context ceiling mid-generation and degrades: it cannot complete the response cleanly, inference stalls, and the client times out.

**Interim fix applied:** `ollama_num_ctx` bumped to **128,000** in `config.yaml`. This eliminates the context ceiling problem. However, this almost certainly re-introduces the original KV cache spill — see VRAM budget analysis below.

### VRAM Budget Analysis (qwen3.5:9b, Q4_K_M)

| num_ctx | KV cache (est.) | + model weights | Total | Fits in ~18.9 GB? |
|---|---|---|---|---|
| 16,384 | ~2.4 GB | 6.6 GB | ~9 GB | ✅ All layers on GPU |
| 32,768 | ~4.7 GB | 6.6 GB | ~11.3 GB | ✅ Comfortable |
| 65,536 | ~9.4 GB | 6.6 GB | ~16 GB | ✅ Tight but fits |
| 131,072 | ~18.9 GB | 6.6 GB | ~25.5 GB | ❌ Spills to CPU |

KV cache estimate: `2 (K+V) × 36 layers × 8 KV heads × 128 head_dim × num_ctx × 2 bytes`. At 128K context the KV cache alone exceeds total VRAM — the model weights haven't even been counted yet. The 128K interim fix will likely reproduce the slow token generation from the original incident, just triggered by long-recording load rather than a cold-start misconfiguration.

**The actual constraint:** the right `num_ctx` must be large enough that the longest realistic transcript fits comfortably (accounting for thinking tokens), but small enough that the KV cache + model weights stay within VRAM. Based on the table, ~64K appears to be the practical ceiling on this GPU.

**Token budget at 65,536 context:**

| Meeting length | Transcript tokens (~2 tok/sec) | Prompt + thinking + response | Total | Fits at 65K? |
|---|---|---|---|---|
| 30 min | ~3,600 | ~9,000 | ~12,600 | ✅ |
| 72 min | ~8,600 | ~9,000 | ~17,600 | ✅ |
| 3 hours | ~21,600 | ~9,000 | ~30,600 | ✅ |
| ~8 hours | ~57,600 | ~9,000 | ~66,600 | ⚠️ edge |

65,536 covers every realistic single-meeting recording with comfortable headroom. An 8-hour recording would be at the edge, but that scenario is not a realistic input.

**Timeout at 65,536 (GPU-resident):**
With all 33 layers on GPU, generation speed on Apple Silicon is ~20 tokens/sec. A full analysis response of ~5,000 tokens takes ~250 seconds. Prefill for a 3-hour transcript (~21K tokens) adds ~10 seconds. Worst case: **~260 seconds** for a 3-hour meeting. A timeout of **1800s (30 minutes)** is a safe ceiling with ~7× headroom for unusual model load, thinking verbosity, or thermal throttling.

**Settled fix applied (Apr 25, 2026):**
```yaml
ollama_num_ctx: 65536   # GPU-resident; covers meetings up to ~8h
ollama_timeout: 1800    # 30-min ceiling; ~7× headroom over worst-case 3h meeting
```

**Alternatively**, set `num_ctx` dynamically per-request based on actual transcript token count + headroom, rather than a single global constant. This caps the KV cache to what each specific recording actually needs, eliminating both the "too small for long meetings" and "too large for GPU" problems simultaneously. Deferred — 65K fixed value solves the problem without added complexity.

---

## Lessons

1. **A "timeout" is not a root cause.** It is the symptom of *something else* exceeding a budget. Always ask what the budget is, what consumed it, and what the floor latency should be.
2. **Local LLM defaults are tuned for benchmarks, not production.** Models published with 262k context windows are doing it for showcase reasons — production callers must declare their actual working context.
3. **Preflight checks are non-negotiable for stateful external services.** A client constructor that doesn't probe is a constructor that lies about readiness.
4. **Server-side logs are the source of truth, not client-side error strings.** The diagnosis lived in `~/.ollama/logs/server.log` from the first attempt — the pipeline log was actively misleading.

---

## References

- Pipeline: [pipeline.py](../../pipeline.py) — `configure_ollama()`, `analyze_with_ollama()`, `analyze_with_retry()`
- Config: [config.yaml](../../config.yaml), [config.py](../../config.py)
- Project conventions: [CLAUDE.md](../../CLAUDE.md) — 3-tier error handling, retry policy
- Ollama server log: `~/.ollama/logs/server.log`
- Ollama runtime state: `curl http://localhost:11434/api/ps`
- Related: model `qwen3.5:9b` (Q4_K_M, 9.7B params, 6.6 GB on disk)
