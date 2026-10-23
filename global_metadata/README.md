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
- Steam: 600 games
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

## Official reviews API and a safe Actions validation

Both acquisition paths use `steam_reviews.py` and Valve's
[IUserReviewsService/GetAppReviews/v1](https://partner.steamgames.com/doc/webapi/IUserReviewsService).
The request uses `input_json`, Helpful=0, languages=["all"], review_type=All=0,
purchase_type=All=1 and the first page. Review descriptions remain English and
off-topic filtering retains the previous default. Reviews are anonymous: the
ordinary Steam Web API key used for the catalogue is not a publisher key and
is never sent to this endpoint.

Valve documents a lower shared anonymous limit but no numeric quota. The shared
client enforces at least 10 seconds between request starts within the process,
serializes concurrent callers, makes no immediate retries, and opens a circuit
on HTTP, JSON, missing-summary and network failures. HTTP 429 backs off for at
least 15 minutes; other failures for at least 5 minutes. A longer `Retry-After`
(seconds or HTTP date) is honoured. The global builder persists the cooldown
in its working database and stops the batch; desktop retries are also deferred.
Other applications sharing the runner's public IP may consume the same limit,
so the interval is a conservative policy, not a guarantee of Valve's quota.

Existing counts and `reviews_fetched_at` are retained on failures. Only a
validated first-page summary updates reviews; an HTTP 200 with an empty
`response` is not evidence of zero reviews. Real zero-count summaries are valid.

Before resuming scheduled publication:

1. Push the migration commit, open **Actions → Update Pecetracker Global Metadata → Run
   workflow**, and select the branch containing it. Rerunning old run #231
   would still use its old commit.
2. Set `steam_limit=3`, `hltb_limit=0`, `validation_only=true`. This downloads and
   verifies the existing catalogue, skips catalogue sync, updates the runner's
   disposable copy, and skips both publication steps. No release asset is reset
   or replaced. Validation accepts at most 10 Steam games and no HLTB work.
3. Check the logged **Source commit** and job summary/artifact
   `metadata-acquisition-*`: `status=ok`, `reviews_attempted>0`,
   `reviews_refreshed=reviews_attempted`, `review_errors=0`, `deferred=false`.
   `selected=0` means no eligible work, not a successful API probe.
4. Only after that check, run a small normal batch with `validation_only=false`,
   then resume maintenance. Scheduled Steam batches are limited to 600 because
   2500 anonymous calls at 10-second intervals exceed the 300-minute job limit.

The builder's `--report PATH` writes per-run counters. Failed/deferred Steam
work exits with code 2, so the job cannot publish or report Success after
silently collecting no reviews. A report is retained even on this failure.
No eligible work or disabled Steam work is distinguished by the counters and
does not fail the run. Publication failure recovery remains unchanged.

## Measuring local ownership coverage

After refreshing your account in Pecetracker, run:

```bash
python global_metadata/check_library_coverage.py /path/to/global_metadata.db
```

Coverage uses the last successful ownership snapshot for the configured account, including games with no personal edits. Use `--steam-id STEAMID64` to select another account with a saved snapshot. Older installations need one successful library refresh to create this snapshot; the tool no longer substitutes edited-game rows as the denominator.

The desktop bootstrap retains the catalogue's original fetch dates. It does not renew cache freshness when copying rows. The desktop and global matcher reject conflicting sequel numbers, while manual local HLTB IDs remain authoritative. Global Steam HTTP 429/server/network failures pause the rest of a batch and persist a service cooldown for the next run.
