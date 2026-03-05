# Daily Economic Intelligence Pipeline

This project runs an autonomous daily economic intelligence workflow focused on Australian exposure to US and Canadian policy and market developments.

## What it achieves

On each run, the pipeline:
- Collects current US/Canada macro, policy, trade, and sector signals via web search and local context files.
- Applies an Australia-specific relevance filter to keep only material items.
- Produces a structured Morning Brief in markdown.
- Updates pipeline memory files (`brief_index`, `running_notes`, logs).
- Sends the brief by email through the Gmail API.

## How it works

Core runner: [pipeline.py](/Users/harrietgoers/Documents/aus_na_econ_policy_tracker/pipeline.py)

### Prompt-driven agent loop
- Loads runtime system prompt from [prompt.txt](/Users/harrietgoers/Documents/aus_na_econ_policy_tracker/prompt.txt).
- Builds today’s invocation message (America/New_York timestamp).
- Calls Anthropic in a tool loop until completion.

### Tooling exposed to the model
- `web_search`: live web retrieval with caching, recency/date filters, and per-phase budgets.
- `read_file`: reads pipeline context files.
- `write_file`: stages file updates in memory first.
- `send_email`: sends via Gmail API (multipart plain + HTML rendering from markdown).

### Reliability controls
- Default max turns: `100`.
- Retry/backoff on Anthropic API, web search, and Gmail send.
- Staged writes are only committed at successful completion after email step.
- API responses are archived to `logs/api_archive/` for audit.

## Directory layout

- `briefs/`
  - `brief_index.md`
  - `BRIEF-YYYY-NNN.md`
  - `failed_delivery/`
- `logs/`
  - `running_notes.md`
  - `pipeline_log.md`
  - `api_archive/`
- `data/`
  - `trade_exposure_register.md`

## Configuration

Create `.env` in project root with:

```env
ANTHROPIC_API_KEY_POLICY_TRACKER=...
RECIPIENT_EMAIL=...

GMAIL_CREDENTIALS_FILE=.secrets/gmail_credentials.json
GMAIL_TOKEN_FILE=.secrets/gmail_token.json
GMAIL_SENDER=
EMAIL_FROM=
CC_EMAIL=
```

## Quickstart

1. Create and activate a Python environment.
2. Install dependencies:
   ```bash
   pip3 install -r requirements.txt
   ```
3. Create secrets directory and add Google OAuth client JSON:
   - `.secrets/gmail_credentials.json`
4. Configure `.env` with `ANTHROPIC_API_KEY_POLICY_TRACKER` and `RECIPIENT_EMAIL`.
5. Run one-time Gmail auth:
   ```bash
   python3 pipeline.py --init-gmail-auth
   ```
6. Test end-to-end without live send effects:
   ```bash
   ./scripts/run_pipeline.sh --dry-run-email
   ```
7. Run live:
   ```bash
   ./scripts/run_pipeline.sh
   ```

## Common operations

- Increase turn/search budgets for heavy news days:
  ```bash
  ./scripts/run_pipeline.sh --max-turns 120 --max-web-search-calls 80
  ```
- Re-run Gmail OAuth setup:
  ```bash
  python3 pipeline.py --init-gmail-auth
  ```
- Schedule weekday runs at 06:30 ET:
  - See `scripts/cron_example.txt` (timezone-aware scheduler preferred).

## GitHub Actions automation (computer can be off)

Workflow file: [`.github/workflows/daily-pipeline.yml`](/Users/harrietgoers/Documents/aus_na_econ_policy_tracker/.github/workflows/daily-pipeline.yml)

- Runs daily (including weekends) at **06:00 America/New_York** (DST-safe via dual UTC cron + NY-time gate).
- Supports manual trigger via `workflow_dispatch`.
- Executes live pipeline run and uploads `logs/` + `briefs/` artifacts.

Required repository secrets:
- `ANTHROPIC_API_KEY_POLICY_TRACKER`
- `RECIPIENT_EMAIL`
- `GMAIL_CREDENTIALS_JSON_B64` (base64 of `gmail_credentials.json`)
- `GMAIL_TOKEN_JSON_B64` (base64 of `gmail_token.json`)

Optional repository secrets:
- `CC_EMAIL`
- `GMAIL_SENDER`
- `EMAIL_FROM`

Create base64 secrets locally:

```bash
base64 -i .secrets/gmail_credentials.json | pbcopy
base64 -i .secrets/gmail_token.json | pbcopy
```

## Notes

- Email recipient is enforced from `.env` (`RECIPIENT_EMAIL`), not model-provided `to`.
- Outgoing email strips the `For:` line and sends formatted HTML plus plain text.
- If a run fails, check `logs/pipeline_log.md` first, then inspect `logs/api_archive/`.

## Expected daily output example

Example sequence from a successful weekday run:

1. Brief file written:
   - `briefs/BRIEF-2026-003.md`
2. Index appended:
   - `BRIEF-2026-003 | 2026-03-06 | LEAD: Fed Holds; LNG Tightens`
3. Running notes appended:
   - New dated section in `logs/running_notes.md` with carry-forward watch items.
4. Pipeline log entry:
   - `[2026-03-06T06:32:41-05:00] | PIPELINE RUN END | Turns: 24 | Runtime: 148s | Dry run email: N`
