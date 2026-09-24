# Final presentation runbook

This checklist follows SRD 9.2 and 9.4. Record actual times, results and release IDs;
empty boxes are not acceptance evidence. Use synthetic Mirror data only.

## Before freeze

1. Deploy reviewed, tagged backend/frontend and Mirror releases to `final`; record both git
   SHAs, prompt version, model IDs, Guardrail version and Mirror version in release notes.
2. Run all acceptance tests in `final`, including live email, WhatsApp, A2A/MCP, ten
   consecutive random Lab scenarios and 50 Lab runs with at least 95% correct outcomes.
3. Seed 30 **distinct active cases** for AT-28. Verify `/cases` returns at least 30 before
   testing. Set a short-lived Cognito planner token in `AERA_LOAD_ACCESS_TOKEN` in the
   operator's terminal. Run:

   ```powershell
   uv sync --locked --group load
   uv run --locked --group load locust -f load/locustfile.py --headless -u 30 -r 30 -t 30m --host <HTTPS_API_STAGE_URL_NO_TRAILING_SLASH> --csv load-results
   ```

   `load-result.json` records duration, signals/hour, minimum active cases, signal
   accounting and request failures. Check `load-results_stats.csv` and cloud traces for
   ingestion-to-board p95 <= 15 s (including one-page Textract), ten warm reference
   agent runs p95 <= 90 s excluding human wait, and approval-to-verified-writes p95
   <= 30 s. A passing ledger alone does not establish AT-28.
4. Run full evaluation from the tagged head. Export failure list, measured timing and
   cost with sample sizes. Put industry benchmarks in a separate labelled block.
5. Record a fallback video from one complete live end-to-end case, including SAP Mirror
   writes and a judge-style Lab run. Keep it available offline during the presentation.
6. Rehearse the 9.4 sequence three times. Each rehearsal includes at least two random
   Lab scenarios, measures judge-selection-to-verified-plan time, and records glitches.
   Fix glitches and repeat until three consecutive rehearsals are clean.

## T-24 hours

- Freeze `final`. Run final acceptance and export the fallback video. Do not change
  configuration or code after freeze without repeating the affected checks.
- Record final release IDs and measured claims in the pitch. Mark any unavailable live
  metric as unavailable; never substitute a benchmark or fixture result.

## T-60 minutes

- Check AWS health, budget alerts, SES/WhatsApp webhook readiness, Mirror health and
  AgentCore Runtime status.
- Set a short-lived Cognito planner token in `AERA_PROBE_ACCESS_TOKEN`; run:

  ```powershell
  uv run --locked python scripts/release_probe.py --api-url <HTTPS_API_STAGE_URL>
  ```

- Run the SAP sandbox read check and Mirror read/write smoke against the deployed
  endpoints. Record counts and HTTP status, with no credential values.

## T-15 minutes

- Reset the Mirror with the admin console's typed confirmation. Check reference cases
  and the six seeded purchase orders.
- Choose a dedicated, safe warm-up case; run the agent and Lambda path:

  ```powershell
  uv run --locked python scripts/release_probe.py --api-url <HTTPS_API_STAGE_URL> --warmup-case <CASE_ID>
  ```

- Open console full screen. Open the recorded fallback video in another tab.

## Presentation

- Run reference scenario live. Ask a judge for Lab parameters; run the selected
  synthetic scenario without scripted intervention. End on measured KPI dashboard.
- If any live step stalls more than 20 seconds, announce the stall and switch to the
  recorded video at the same stage. Do not describe video or replay as a live result.
