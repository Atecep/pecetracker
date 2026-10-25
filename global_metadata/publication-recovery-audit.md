# Publication recovery audit — 2026-10-25

## Evidence from failed Actions runs

Read-only inspection used `gh run list`, `gh run view RUN_ID --log`, and job step
outcomes. No workflow was dispatched and no release assets were modified.

| Run | Valid Store writes | Valid review writes | Acquisition failure | Outcome |
| --- | ---: | ---: | --- | --- |
| [25 Oct, 09:05 UTC](https://github.com/Atecep/pecetracker/actions/runs/40179235239) | 1133 | 123 | Four API errors for AppID 4916080; reviews raised `3 consecutive invalid responses` | Builder and health check failed; checkpoint and both publication steps skipped |
| [25 Oct, 12:01 UTC](https://github.com/Atecep/pecetracker/actions/runs/40191321242) | 1471 | 137 | Same AppID, same four errors and fatal exception | Same publication blockage |
| [23 Oct, 19:55 UTC](https://github.com/Atecep/pecetracker/actions/runs/40015193401) | 261 | 260 | One review read timeout | Older implementation stopped Steam and exited 2, blocking publication |

The two newest failures ran commit `8793eae`. Their reviews workers attempted
127 and 141 requests respectively, with four failures each. The same failed app
became eligible every five minutes while the Store worker was still running;
reviews selected it again. At roughly 901 seconds, the invalid-response counter
raised a `RuntimeError`. `update_steam()` converted that exception into
`fatal_error`; `steam_run_failed()` made `main()` exit 1 with
`publishable=false`. The workflow's health check failed as well, so default
success conditions skipped the integrity and publication steps.

## Corrections

- Remove the consecutive acquisition error threshold and HTTP 401/403 promotion
  to fatal errors. Their count cannot block publication of valid transactions.
- Attempt each game's reviews once per run, including while waiting for new
  Store details and after a per-app retry delay expires. Future runs retain
  normal eligibility; successful writes still clear their retry records.
- Keep the existing independent service cooldowns for rate limits, transport
  failures and upstream outages. Other services continue; no cadence, budget or
  backoff increases were introduced.
- Treat malformed Store payloads as acquisition errors. Only explicit negative
  availability in both existing market attempts is stored as `unavailable`.
  Validate fields before SQLite receives them, preserving old rows on failure.
- Keep HLTB acquisition errors pending without changing cached metadata. Validate
  wrapper result shapes before treating an empty search as a no-match. Move
  persistence outside the acquisition exception handler so database/transaction
  failures remain fatal instead of being disguised as HLTB request failures.
- Report completed, failed, pending and unattempted work per service, including
  HLTB. Record publication outcomes separately for raw/compressed assets; only
  verified successful promotion counts as published. Emit explicit warnings for
  partial acquisition and retain both diagnostic artifacts.
- Keep critical checks mandatory: SQLite checkpoint completion, full integrity
  results, foreign keys, checksums and the existing verified promotion/rollback.

Steam Reviews remains at **2 seconds**, with independent scheduled budgets of
**2500 Store details and 2500 review summaries**. Store pacing, Steam time budget,
HLTB cadence/budget/backoffs, schedule and job timeout are unchanged.

## Validation

The regression suite includes a real temporary SQLite database with 2500 games,
old valid cached data, and 400 consecutive acquisition failures in each Steam
service. Each service attempts 2500 distinct games and commits 2100 updates;
400 retain their old values/timestamps and retry eligibility. `main()` exits
zero with `status=partial` and `publishable=true`. Both SQLite integrity checks
pass. No API requests or release uploads are made by this test.

Additional tests cover the real reviews client retaining exact two-second
request starts after three invalid responses, no repeat attempts after retry
expiry in the same run, isolated failures, HTTP 429/500/503 and transport errors,
independent service cooldowns, malformed Store responses, valid regional Store
fallback, HLTB error reporting/preservation, fatal database errors, Actions warning
and publication counters, foreign-key corruption, and workflow shell syntax.

Validation command: `.venv-build-linux/bin/python -B tests/run_tests.py`.
Final result: **186 tests passed in 6.185 seconds**. `git diff --check` passed.
These are local regressions; an actual Actions run and release publication
require the changes to be pushed.
