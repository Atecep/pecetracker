# Global Metadata Builder

This directory contains the standalone builder for the Steam Library Tracker
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
- Steam: 2500 games
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

## Measuring local ownership coverage

After refreshing your account in SLT, run:

```bash
python global_metadata/check_library_coverage.py /path/to/global_metadata.db
```

Coverage uses the last successful ownership snapshot for the configured account, including games with no personal edits. Use `--steam-id STEAMID64` to select another account with a saved snapshot. Older installations need one successful library refresh to create this snapshot; the tool no longer substitutes edited-game rows as the denominator.

The desktop bootstrap retains the catalogue's original fetch dates. It does not renew cache freshness when copying rows. The desktop and global matcher reject conflicting sequel numbers, while manual local HLTB IDs remain authoritative. Global Steam HTTP 429/server/network failures pause the rest of a batch and persist a service cooldown for the next run.
