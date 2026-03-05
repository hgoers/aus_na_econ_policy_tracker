# Daily Economic Intelligence Pipeline

Implements the pipeline in `prompt.txt` using Anthropic tool use.

## Setup

1. Create and activate a Python environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill values.
4. Run:
   ```bash
   ./scripts/run_pipeline.sh --dry-run-email
   ```

## What this creates on first run

- `briefs/brief_index.md`
- `logs/running_notes.md`
- `logs/pipeline_log.md`
- `data/trade_exposure_register.md`
- `logs/api_archive/` for raw Anthropic responses
- `briefs/failed_delivery/`

## Production run

```bash
./scripts/run_pipeline.sh
```

Schedule at 06:30 America/New_York on weekdays via a timezone-aware scheduler.
See `scripts/cron_example.txt` for a local cron example.
