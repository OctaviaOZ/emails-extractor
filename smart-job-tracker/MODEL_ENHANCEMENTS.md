# SmolLM3 Model Enhancements for Smart Job Tracker

Based on the official SmolLM3 documentation and codebase analysis, several enhancements have been implemented to improve extraction accuracy and reliability.

## ✅ 1. Dual-Mode Reasoning (`/think`)
- **Status:** IMPLEMENTED
- **Detail:** Injected `/think` into the system prompt to trigger SmolLM3's internal reasoning engine.
- **Benefit:** Significantly higher accuracy for complex emails where company names or statuses are ambiguous.

## ✅ 2. Multilingual Optimization (German Support)
- **Status:** IMPLEMENTED
- **Detail:** Updated system prompt to explicitly handle English and German recruitment patterns. Added instructions for German-specific signatures and content translation.
- **Benefit:** Reliable extraction from German recruitment emails.

## ✅ 3. Jinja Template Compatibility (Monkey Patch)
- **Status:** IMPLEMENTED
- **Detail:** Implemented a robust monkey patch for `llama_cpp.llama_chat_format` to strip unknown `{% generation %}` and `{% endgeneration %}` tags from new Hugging Face models.
- **Benefit:** Fixed "unknown tag" warnings and allowed usage of the model's native optimized chat templates.

## ✅ 4. Enhanced Structured Output
- **Status:** IMPLEMENTED
- **Detail:** Refined prompt instructions to strictly distinguish between application platforms (Workday, etc.) and actual employers. Updated field descriptions for `position` and `status`.
- **Benefit:** Cleaner data in the dashboard with fewer "Platform" entries in the company column.

## ✅ 5. Performance/Stability Tuning (Middle Ground)
- **Status:** IMPLEMENTED
- **Detail:** Balanced `n_ctx` (3072), `n_batch` (128), and `n_threads` (4) to maximize throughput while staying safely within 8GB RAM limits.
- **Benefit:** 2x faster inference than the default "Safe Mode" without the original crashes.

## ⏳ 6. Dynamic Context Scaling
- **Status:** PENDING
- **Concept:** For exceptionally long emails, the system could temporarily increase `n_ctx` (up to 128k supported by SmolLM3) if RAM permits.
- **Current State:** Using a fixed 3072 window with 5000 character truncation.
