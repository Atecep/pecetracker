# Global Metadata Builder

This directory contains the standalone builder for the Pecetracker
global metadata catalogue.

It does **not** modify the desktop application's local database.

The builder reuses the existing Steam metadata fetcher and HLTB matcher from
the project so both the desktop app and global catalogue use the same rules.

## Test run

Set your Steam Web API key.

### Linux

```bash
export STEAM_WEB_API_KEY="YOUR_KEY"
```

### Windows PowerShell

```powershell
$env:STEAM_WEB_API_KEY="YOUR_KEY"
```

For a small first test, import only the first 1,000 catalogue entries and
process 100 Steam + 100 HLTB records:

```bash
python global_metadata/build_global_database.py \
  --catalog-page-size 1000 \
  --catalog-max-pages 1 \
  --steam-limit 100 \
  --hltb-limit 100
```

The test page limit deliberately prevents the catalogue sync from being marked
as complete.

The database is written to:

```text
global_metadata/data/global_metadata.db
```

Show current totals without making network requests:

```bash
python global_metadata/build_global_database.py --stats-only
```

## Full catalogue sync

After deleting the test database, run:

```bash
python global_metadata/build_global_database.py \
  --steam-limit 100 \
  --hltb-limit 100
```

The first run walks the complete Steam game catalogue. Later complete runs use
Steam's modification timestamp so only new/changed catalogue entries need to be
returned.

Steam metadata and HLTB metadata are populated incrementally and can resume
across runs.

## GitHub Actions maintenance

After the initial bootstrap, the workflow runs twice daily:
04:12 and 16:12 UTC.

Scheduled limits:
- Steam Store details: up to 2500 games
- Steam reviews: up to 2500 summaries (independent budget)
- HLTB: 1300 games
- HLTB throttle: 1.9 seconds

Global metadata refresh:
- Steam details: 365 days or catalogue changes
- Steam reviews: 90 days
- HLTB matched: 180 days
- HLTB no_match: 90 days
- HLTB errors: retry cooldowns of 1h, 6h or 24h

The desktop application's local HLTB no_match cache
remains at 30 days.

Only eligible records are refreshed during maintenance.

## Official reviews API, independent scheduling and publication

Both acquisition paths use `steam_reviews.py` and Valve's
[IUserReviewsService/GetAppReviews/v1](https://partner.steamgames.com/doc/webapi/IUserReviewsService).
Requests use `input_json`, Helpful=0, languages=["all"], review_type=All=0,
purchase_type=All=1 and the first page. Descriptions remain English and off-topic
filtering retains the previous default. The catalogue's ordinary Steam Web API
key is never sent to reviews; an app-associated publisher key is a different
credential.

Valve documents anonymous access, a lower shared limit and HTTP 429, without a
numeric anonymous quota or reset interval. The global builder uses a configurable
2-second minimum between review request starts, based on the limited observations
in [the throughput audit](steam-throughput-audit.md). This is an operating policy,
not a discovered quota. The desktop client's default remains 10 seconds.

Three workers run concurrently: Store details, reviews and the existing HLTB
worker. `--steam-limit` now budgets details only; `--steam-reviews-limit` budgets
reviews separately (defaults to `--steam-limit` when omitted). Details keep their
existing 2-second pause between games. Reviews can process already cached details
while new details are being acquired and pick up new ready rows as they arrive.
They select missing/oldest summaries independently of catalogue dirtiness; a
catalogue change alone no longer forces a fresh review summary to be fetched.
Review-only work does not consume the details budget. Disabling either Steam
lane does not disable the other. HLTB limits, cadence and matching are unchanged.

Each Steam lane has a 150-minute time budget (`--steam-budget-seconds 9000`),
measured from their common start, rather than a combined request allowance.
This reserves room within the existing 300-minute job timeout. An in-flight
request can overrun this budget by its bounded HTTP timeout (Store may try two
markets). HLTB retains its existing behavior and can still determine total run
time. The selected count is an upper bound, not a promise of completed work.

HTTP 429, transport failures and server errors stop only the affected Steam
lane, without immediate retries. Its cooldown is persisted independently:
`steam_details_retry_after` or `steam_reviews_retry_after`. Backoff is at least
15 minutes for 429 and 5 minutes otherwise; a longer `Retry-After` (seconds or
HTTP date) is honoured. The previous `steam_service_retry_after` is also honoured
for reviews during migration, without blocking details.

An invalid summary/other app-specific acquisition failure records a
per-app, per-service retry in `steam_fetch_retries` and continues with other apps.
Repeated invalid responses and HTTP 401/403 are acquisition failures too: their
count never makes otherwise valid progress unpublishable. Each app's reviews
are attempted at most once per run, even if its persistent retry delay expires
while Store details are still running. Store responses without a valid requested
entry are errors; only explicit `success=false` in both markets means unavailable.
Failures never refresh review dates or replace old counts with zero. A validated
summary containing explicit zero counts is valid. Successful row writes, dirty
flags and retry cleanup commit in short SQLite transactions. The review success
counter advances only when the database actually updates a ready row.

`--report PATH` writes separate `details` and `reviews` objects: `limit`,
`selected`, `attempted`, `completed`, `pending`, `not_attempted`, `updated`,
`unavailable`, `skipped`, `errors`,
`deferred`, `budget_exhausted`, `elapsed_seconds`, `fatal_error` and up to ten
error samples. `selected` counts candidate apps for that lane, `attempted` counts
app-level acquisition calls (Store can issue two HTTP requests), and reviews
`updated` counts persisted aggregate summaries, not individual user reviews.
The same app can appear in both lanes; do not add their counts to infer unique
games. No eligible work reports zero, not a successful API validation.
`completed` counts committed successful results (`updated` plus explicit Store
`unavailable`); `pending` counts selected work not completed, including failures,
skipped writes and unattempted work. These counters describe this run's selected
batch, not the entire catalogue backlog or rows still in a previous cooldown.
The report also includes `hltb`, with `matched`, `no_match`, acquisition errors,
pending work and any fatal persistence error. HLTB acquisition failures preserve
cached matches, dirty flags and future retry eligibility. HLTB database writes
are outside the acquisition-error handler, so transaction failures block release.

A run with acquisition errors (regardless of count), a service cooldown or an
exhausted time budget reports `status=partial`, `publishable=true` and exits zero.
Actions emits a visible warning and retains the report; valid progress can pass
the mandatory SQLite integrity/checksum checks and safe release promotion. Failed
apps remain pending. Corruption, integrity/transaction failures and unexpected
internal errors that prevent establishing safe publication report
`status=fatal`, `publishable=false` and exit nonzero, blocking publication.
Catalogue and uncaught worker failures also fail the job. The existing asset
promotion/rollback mechanism is retained; raw and compressed asset pairs are
promoted separately, as before.
Actions reports acquisition separately from actual publication. The final
`publication-summary.json` artifact records each asset's promotion outcome and
published updates per service. A failed/skipped promotion confirms no published
updates; a successful promotion reports committed updates, without adding raw
and compressed copies together. A partial acquisition exits zero with an explicit
warning; the SQLite checkpoint, full integrity check, foreign-key check, checksums
and verified promotion remain mandatory.

For a small Actions validation, select the branch containing this change, set
`steam_limit=3`, `steam_reviews_limit=3`, `hltb_limit=0`, and
`validation_only=true`. Each Steam budget must be 0..10 with at least one enabled.
This checks a downloaded disposable DB, skips catalogue sync and both publication
steps. Inspect both lane counters and `status`: a partial run can publish valid
progress but is not evidence of a fully successful review probe. `updated=0`
with no eligible candidates also does not validate the API. Rerunning an older
workflow uses its original commit. No workflow needs to be dispatched to run the
offline regressions:

```bash
.venv-build-linux/bin/python -B tests/run_tests.py
```

## Measuring local ownership coverage

After refreshing your account in Pecetracker, run:

```bash
python global_metadata/check_library_coverage.py /path/to/global_metadata.db
```

Coverage uses the last successful ownership snapshot for the configured account, including games with no personal edits. Use `--steam-id STEAMID64` to select another account with a saved snapshot. Older installations need one successful library refresh to create this snapshot; the tool no longer substitutes edited-game rows as the denominator.

The desktop bootstrap retains the catalogue's original fetch dates. It does not renew cache freshness when copying rows. The desktop and global matcher reject conflicting sequel numbers, while manual local HLTB IDs remain authoritative. Global Steam HTTP 429/server/network failures pause only the affected Steam lane and persist its cooldown for the next run.
