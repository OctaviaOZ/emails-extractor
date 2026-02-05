# AI Extraction & Accuracy Enhancements

This document describes the multi-layered strategy implemented to maximize the quality of job application data extraction using LLMs (Local Llama 3.2, Claude, OpenAI, and Gemini).

## 1. AI-First Extraction Logic
The system is configured with an **AI-First** mandate. Heuristic (pattern-based) extraction is no longer a primary rule but a "safety net" for individual failures.
- **Always Attempt AI:** Every email is first sent to the configured LLM providers.
- **Failover Chain:** If the primary cloud model fails (e.g., rate limits), the system falls back to the high-speed local Llama 3.2 model.
- **Individual Recovery:** Heuristics are only invoked if all AI providers fail to parse a specific email, preventing a global "lock-out" of AI capabilities.

## 2. Robust JSON Parsing (Local Model)
Small local models (like Llama 3.2 3B) often struggle to output *only* raw JSON. We implemented a robust parsing engine:
- **Markdown Stripping:** Automatically removes ```json tags if the model wraps its output in markdown.
- **conversational Cleaning:** Uses fuzzy regex to find the first `{` and last `}` in the output, ignoring conversational preamble or postscript added by the AI.

## 3. Hybrid Refinement Strategy
Instead of discarding AI results when they contain technical errors, we use a **Refine-after-Extract** approach:
- **Company Name Filter:** If the AI identifies a technical platform (e.g., Successfactors, Workday, Greenhouse) as the employer, the system preserves the AI's status and summary but uses heuristics to extract the *real* company name from the email signature or sender name.
- **Status Keyword Overrides:** A secondary logic layer checks for high-confidence German/English keywords (e.g., "Arbeitsprobe", "Absage"). These can override the AI's status if the model is too conservative or misinterprets positive feedback as a job offer.

## 4. Prompt Engineering & Forceful Instructions
The system prompts have been iteratively refined to overcome common small-model biases:
- **Forceful Negation:** Instructions like "NEVER use platform names" are placed at the beginning of the prompt to maximize attention.
- **Few-Shot Context:** Explicit examples (e.g., DKB vs. Richemont on Successfactors) are provided to the model to explain the difference between the email infrastructure and the actual employer.
- **Schema Hints:** The Pydantic data models used for validation include metadata descriptions that guide the LLM's field selection.

## 5. Data Synchronization
To prevent system crashes during extraction:
- **Enum Alignment:** All application statuses are synchronized to **UPPERCASE** across the Database (Postgres), Python Models (SQLModel), and Reporting layers.
- **Field Mapping:** The system automatically maps synonymous fields (e.g., mapping `employer` to `company_name` or `description` to `summary`) to handle LLM inconsistency.

## 6. History & Process Tracking
- **Event Logging:** Every email is logged as an `ApplicationEventLog`.
- **Deduplication:** The system uses a specialized processor to decide whether an email is a status update for an existing process or the start of a new one, preventing the merging of separate applications at the same company.
