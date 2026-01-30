# Llama 3.2 3B Optimization Log & Technical Analysis

**Date:** January 29, 2026
**System Profile:** Linux / Intel Core i3-5005U (2 Cores, 4 Threads) / 8GB RAM / No GPU
**Model:** `Llama-3.2-3B-Instruct-Q4_K_M.gguf` (~1.9GB)

---

## 1. Executive Summary
Running a 3-billion parameter Large Language Model (LLM) locally on an 8GB RAM system without a GPU requires strict resource management. The initial implementation caused total system freezes due to memory exhaustion (OOM) and swap thrashing. Through iterative profiling and configuration tuning, we achieved a stable inference pipeline that operates safely within a **6GB hard limit** while maintaining functional accuracy.

---

## 2. Optimization Timeline & Technical Deep Dive

### Phase 1: The "Freeze" & Resource Starvation
**Initial State:**
- **Config:** Defaults (likely `n_ctx=8192`, `n_gpu_layers=-1`, `n_batch=512`, `n_threads=4`).
- **Symptom:** The PC froze completely, requiring a hard reboot.
- **Root Cause:**
    1.  **Memory:** The OS + Browser + Streamlit + Model Weights (2GB) + KV Cache (for 8k context) exceeded 8GB physical RAM. The OS pushed pages to Swap, causing "thrashing" where the CPU spends more time moving data between Disk and RAM than processing instructions.
    2.  **CPU Starvation:** Using `n_threads=4` on a 2-core/4-thread CPU (i3-5005U) saturated all logical cores, leaving 0% CPU for the OS UI/Kernel interrupts, causing the UI freeze.

### Phase 2: OS-Level Safety Guardrails
**Action:** Implemented `resource.setrlimit` logic in `app/ui.py`.
**Technical Detail:**
We utilized the Linux `setrlimit(RLIMIT_AS)` syscall. This sets a hard ceiling on the **Virtual Address Space** the Python process can request. If the app tries to `malloc` beyond this, the OS instantly returns a memory error (or kills the process) instead of swapping to death.
- **Initial Limit:** 4GB (4096MB).
- **Result:** Prevented freezing, but prevented the model from loading.

### Phase 3: Model Configuration Tuning
**Action:** Switched to `llama-cpp-python` with optimized parameters in `LocalProvider`.

| Parameter | Value | Technical Justification |
| :--- | :--- | :--- |
| `n_gpu_layers` | `0` | Explicitly disables VRAM offloading. Forces CPU-only tensor operations, avoiding CUDA/Metal initialization overhead. |
| `n_threads` | `2` | Leaves 50% of CPU threads free for the OS and Streamlit event loop, ensuring the UI remains responsive during inference. |
| `verbose` | `False` | Reduces I/O blocking overhead from logging token generation. |

### Phase 4: Addressing Allocation Failures (`std::bad_alloc`)
**Symptom:** `Failed to load Local Provider: Unable to allocate ...` and Core Dumps.
**Analysis:**
The 4GB limit was too aggressive.
- **Static Memory:** Model Weights (~1.9 GB) + Python/Streamlit Overhead (~1.2 GB).
- **Dynamic Memory:** The KV Cache (Key-Value storage for context) and Compute Buffers grow with context size.
**Action:**
1.  **Increased Limit:** Bumped `RLIMIT_AS` to **6GB** (6144MB). This leaves ~2GB buffer for the OS.
2.  **Reduced Batch Size (`n_batch`):** Reduced from 512 -> 256 -> **64**.
    - *Why?* During prompt processing, the model processes tokens in chunks. Larger chunks require larger temporary float32 tensors for matrix multiplication. Reducing batch size linearly reduces peak RAM spikes during the "prefill" phase.

### Phase 5: The Context Window Bottleneck
**Symptom:** `Failed to create llama_context` or generic OOM.
**Analysis:**
The KV Cache size is determined by `n_ctx` (Context Window).
- Formula (approx): `2 * n_layers * n_heads * n_embd * n_ctx * sizeof(float16)`.
- At `n_ctx=8192`, the cache reserved ~500MB-1GB of contiguous RAM. On a fragmented heap, this allocation fails.
**Action:**
- Reduced `n_ctx` to **2048**.
- This reduced KV Cache memory footprint by ~75%, making allocation reliable within the 6GB envelope.

### Phase 6: Input Overflow Handling
**Symptom:** `Requested tokens (2327) exceed context window of 2048`.
**Analysis:**
Some emails were longer than our new, smaller context window. When `llama.cpp` receives more tokens than `n_ctx`, it throws an error because it cannot allocate position embeddings for them.
**Action:**
- Implemented **Input Truncation** in `app/services/extractor.py`.
- `body[:3500]` ensures the raw text is ~1000 tokens. Combined with system prompts and schema instructions, the total stays safely under 2048 tokens.

### Phase 7: Robustness & Validation
**Symptom:** `Validation Error: Field required`.
**Analysis:**
The 3B model is "smart enough" but inconsistent. Sometimes it returns `null` for fields or invalid JSON, causing Pydantic validation to crash the app.
**Action:**
1.  **Schema Hardening:** Used `@model_validator(mode='before')` to intercept raw model output. If fields are missing/null, defaults are injected *before* validation occurs.
2.  **Heuristic Refinement:** The 3B model was too conservative (marking "Assessment Invitations" as "Applied"). Added a keyword-based post-processing step (`_refine_status`) to upgrade status based on strong signals ("Assessment", "Reject") in the raw text.

---

## 3. Final Stable Configuration

**Hardware:** 8GB RAM, CPU Only.

| Component | Setting | Value | Purpose |
| :--- | :--- | :--- | :--- |
| **System** | `RLIMIT_AS` | **6144 MB** | Prevents system freeze by killing app if it leaks. |
| **Model** | `n_ctx` | **2048** | Lowers KV Cache RAM usage significantly. |
| **Model** | `n_batch` | **64** | Lowers peak RAM spikes during inference. |
| **Model** | `n_threads` | **2** | Prevents UI lag/starvation. |
| **Input** | Body Length | **3500 chars** | Ensures input fits in the 2048 context window. |
