# Smart Job Tracker

An AI-powered Streamlit dashboard that automatically tracks your job applications by syncing Gmail, extracting structured data with LLMs or heuristics, and storing everything in PostgreSQL.

## How It Works

```
Gmail → SyncService → EmailExtractor → ApplicationProcessor → PostgreSQL → Streamlit UI
```

1. **Gmail Sync** — authenticates with OAuth2, fetches emails under a configured label (default: `apply`), cleans HTML for LLM input
2. **Email Extraction** — routes each email through a configured AI provider (or falls back to regex heuristics) to produce structured `ApplicationData`: company name, job title, status, summary, next step
3. **Application Processing** — uses a tiered matching strategy to find or create a `JobApplication` record: thread ID → exact email → company domain → fuzzy name
4. **Status Progression** — statuses only advance forward (APPLIED → ASSESSMENT → INTERVIEW → OFFER); `REJECTED` can override at any point

---

## Features

- **Auto-sync** from Gmail using a label you define (e.g. `apply`)
- **Multi-provider AI extraction** — local Llama, Claude (Anthropic), GPT-4o-mini (OpenAI), or Gemini 2.0 Flash (Google)
- **Heuristic fallback** — robust regex-based extraction when no LLM is available or when it fails
- **Bilingual support** — handles English and German job emails natively
- **Kanban board** — drag-free visual pipeline across all statuses
- **Dashboard** — funnel metrics, timelines, and application stats
- **History tab** — full status-change event log per application
- **Document storage** — attach CVs and cover letters per application
- **Duplicate prevention** — `ProcessedEmail` table prevents re-processing the same email
- **Status rank enforcement** — `can_update_status()` blocks backwards status changes

---

## Application Statuses

| Status | Meaning |
|---|---|
| `APPLIED` | Application received / confirmation email |
| `PENDING` | Under review, no decision yet |
| `COMMUNICATION` | General back-and-forth |
| `ASSESSMENT` | Coding challenge, take-home task, or test |
| `INTERVIEW` | Invited to a call, video, or in-person interview |
| `OFFER` | Explicit job offer or contract received |
| `REJECTED` | Application declined |

---

## Setup

### Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/)
- PostgreSQL (running locally or remote)
- A Google Cloud project with the Gmail API enabled

### 1. Install dependencies

```bash
cd smart-job-tracker
poetry install
```

### 2. Configure the database

Set the `DATABASE_URL` environment variable (defaults to `postgresql:///job_tracker`):

```bash
export DATABASE_URL="postgresql://user:password@localhost/job_tracker"
```

Run migrations:

```bash
poetry run alembic upgrade head
```

### 3. Gmail credentials

**Option A — Local file:** Place your Google OAuth2 `credentials.json` in `smart-job-tracker/`.

**Option B — Google Cloud Secret Manager:** Set these environment variables:

```bash
export GCP_PROJECT_ID="your-gcp-project"
export GCP_GMAIL_SECRET_NAME="gmail-oauth-client-id"
```

On first run the app will open a browser for OAuth consent and save a `token.pickle`.

### 4. Configure an AI provider

Edit `smart-job-tracker/config.yaml` (created on first run if absent) or set environment variables:

#### Local Llama (default, no API key needed)

Download a model:

```bash
poetry run python download_model.py
```

This downloads `Llama-3.2-3B-Instruct-Q4_K_M.gguf` into `smart-job-tracker/models/`.

`config.yaml`:
```yaml
ai:
  provider: local
  local_model_name: Llama-3.2-3B-Instruct-Q4_K_M.gguf
```

#### Anthropic Claude

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```
```yaml
ai:
  provider: anthropic
```

Uses `claude-3-5-sonnet-20241022`.

#### OpenAI

```bash
export OPENAI_API_KEY="sk-..."
```
```yaml
ai:
  provider: openai
```

Uses `gpt-4o-mini` with structured output (Pydantic response format).

#### Google Gemini

```bash
export GOOGLE_API_KEY="..."
```
```yaml
ai:
  provider: google
```

Uses `gemini-2.0-flash` with JSON response MIME type.

### 5. Run the app

```bash
cd smart-job-tracker
poetry run streamlit run run_app.py
```

---

## Configuration Reference

All settings live in `smart-job-tracker/config.yaml` and can be overridden with environment variables.

| Key | Default | Description |
|---|---|---|
| `label_name` | `apply` | Gmail label to sync |
| `start_date` | `2025-01-01` | Earliest email date to sync |
| `skip_domains` | `[]` | Email domains to ignore |
| `skip_emails` | `[]` | Specific email addresses to ignore |
| `ai.provider` | `local` | LLM provider: `local`, `anthropic`, `openai`, `google` |
| `ai.local_model_name` | `Llama-3.2-3B-Instruct-Q4_K_M.gguf` | GGUF model filename |
| `ai.temperature` | `0.1` | LLM temperature |
| `ai.max_tokens` | `768` | Max tokens for LLM response |

---

## AI Extraction Pipeline

Each email goes through `EmailExtractor.extract()`:

1. **LLM provider call** — sends sender, subject, and up to 3000 chars of body to the configured provider with a structured system prompt
2. **Company name refinement** — strips platform names (Workday, Greenhouse, etc.), cross-validates against sender domain, falls back to heuristic if needed
3. **Status refinement** — keyword-based post-processing overrides weak LLM classifications:
   - Rejection keywords always win
   - Assessment keywords override if not rejected
   - Strong application-confirmation phrases lock status to `APPLIED`
4. **Summary refinement** — falls back to email subject if LLM summary is missing or generic
5. **Heuristic fallback** — if the LLM fails entirely, extracts company name from sender name/domain and status from keyword matching

---

## Database Schema

| Table | Purpose |
|---|---|
| `jobapplication` | One record per job application; holds status, dates, metadata |
| `company` | Deduplicated employer records |
| `companyemail` | Email addresses seen from each company (aids matching) |
| `applicationevent` | Full status-change history per application |
| `interview` | Interview date, location, notes |
| `assessment` | Challenge type, due date, notes |
| `offer` | Salary, benefits, deadline |
| `applicationdocument` | Binary CV/cover letter storage |
| `processedemail` | Deduplication guard — email IDs already synced |
| `processinglog` | Stats per sync run |

---

## Development

```bash
# Run all tests
poetry run pytest

# Run a single test file
poetry run pytest tests/test_processor.py

# Lint
poetry run ruff check app/

# Format
poetry run ruff format app/

# Create a new migration after model changes
poetry run alembic revision --autogenerate -m "description"
poetry run alembic upgrade head
```

Tests mock the Llama model automatically (`conftest.py`) to prevent loading the 2+ GB GGUF file. A 2.5 GB memory limit is enforced during tests; override with `TEST_MEMORY_LIMIT_MB`.
