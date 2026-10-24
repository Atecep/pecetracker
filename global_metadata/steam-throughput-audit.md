# Steam throughput audit — 2026-10-24

The reduction in b701c18 combined two independent workloads under a 600-app
selection limit and a reviews client with a hard 10-second floor. It also made
any acquisition error fail the whole publication. The historical 2500-app
capacity was real, but selection, details and reviews must be counted separately.

## Historical evidence

Logs were downloaded read-only with `gh run view RUN_ID --log`. Job wall times
below come from Actions run creation/completion timestamps, including setup,
HLTB and publication. Older Python output was buffered, so the timestamps beside
individual log lines cannot reliably measure HTTP intervals or Steam worker time.

| Actions run | Commit | Job wall time | Selected Steam games | Details obtained | Review summaries updated | Errors / outcome |
| --- | --- | --- | ---: | ---: | ---: | --- |
| [21 Oct, afternoon](https://github.com/Atecep/pecetracker/actions/runs/39636216221) | 02906be | 1h37m23s | 2500 | 2500 | 2500 | 0; published |
| [22 Oct, afternoon](https://github.com/Atecep/pecetracker/actions/runs/39815529492) | 02906be | 1h26m17s | 2134 | 2131 | 2131 | 2 Store HTTP 502; 1 unavailable; published |
| [23 Oct, morning](https://github.com/Atecep/pecetracker/actions/runs/39901022905) | 02906be | 1h11m48s | 1794 | 1794 | **0** | 1794 legacy review JSON failures; published despite review outage |
| [23 Oct, migration validation](https://github.com/Atecep/pecetracker/actions/runs/39937955893) | b701c18 | 4m07s | 20 | 20 | 20 | 0; success |
| [23 Oct, afternoon](https://github.com/Atecep/pecetracker/actions/runs/40015193401) | f7dae61 | 1h55m37s | **600** | **261** | **260** | One review read timeout at app 2799910 stopped Steam and blocked publication |

The last run attempted reviews for 261 apps; 339 selected apps were never
processed. Its valid 261 detail writes and 260 review summaries remained only in
the runner's working copy. Conversely, the older successful run with 1794 details
did not refresh a single review summary. Neither workflow success nor the
cumulative `Steam reviews` database total proves that reviews were refreshed in
that run. Historical counters refer to aggregate app summaries, not the number
of individual user review texts downloaded.

## Rate limiting investigation

The primary contract is Valve's
[IUserReviewsService documentation](https://partner.steamgames.com/doc/webapi/IUserReviewsService).
It documents optional publisher authentication associated with the app, lower
shared limits for anonymous calls, HTTP 429 and possible 10-minute anonymous
response caching. It specifies no numeric anonymous quota. A personal catalogue
API key does not establish publisher entitlement for arbitrary catalogue apps.

The migrated historical runs show 20 and 260 successful summaries at the previous
10-second spacing. The failed scheduled run ended with a read timeout, **not a
429**. These logs contain no evidence that 10 seconds is a required interval,
nor enough information to infer a sustained quota at any faster interval.

Before configuring the new builder cadence, a small anonymous probe used the
same request shape and strict parser on app IDs 620, 400, 440, 570, 730, 220, 550,
70, 240 and 4000, with starts spaced by at least 2 seconds. All ten returned valid
summaries, without HTTP errors or 429; total measured call time including pacing
was about 18.3 seconds. The first call took 0.480 seconds. The probe stopped on
any error, used no key and published nothing.

This establishes that the endpoint accepted this small local sample at 2 seconds.
It does **not** establish a sustained quota, a reset window or behavior on a
GitHub runner's shared IP. The new interval is configurable. Rate limits and
server/network failures defer only their own lane, honour longer `Retry-After`
values, and retain failed work for later runs. No burst retry or attempt to find
the maximum request rate was performed.

## Capacity and expected throughput

Scheduled budgets are restored to **2500 details plus 2500 review summaries**.
These can overlap on the same games; they are not a claim of 5000 unique games.
Details, reviews and HLTB run concurrently. The cron remains `12 4,16 * * *`,
HLTB remains 1300 games at a 1.9-second request interval, and the job timeout
remains 300 minutes.

| Workload | Old / migrated behavior | New expectation without failures |
| --- | --- | --- |
| Details | 2500 before migration; reduced to at most 600 and coupled to reviews afterward | Up to 2500, with the original 2-second per-game pause plus Store latency |
| Reviews | 10-second starts imposed by migration: 2500 require at least 416.5 minutes | 2-second starts: 2500 require at least 83.3 minutes, plus slow responses / waiting for details |
| Total Steam time | Coupled: slow reviews delayed every following detail | Approximately the slower lane's elapsed time; the lane times are not added |
| Errors | One timeout blocked publication of all progress | Valid transactions may publish with a visible partial report; fatal failures still block |

The disposable three-app integration check obtained 3 details in 4.805 seconds
and 3 validated/persisted summaries in 4.951 seconds, with no errors. Both lanes
were active concurrently and HLTB was disabled. Subtracting the two 2-second
details pauses gives about 0.268 seconds per Store fetch/write in this tiny
sample. If that average held, 2500 details would take roughly 94.5 minutes;
reviews could keep up at 2-second starts. That is compatible with the former
roughly 1h40 capacity, **as an estimate**, not a production benchmark. Regional
fallbacks, slow responses, SQLite overhead on the full catalogue, HLTB, setup and
asset handling can increase job time. A 150-minute budget per Steam lane bounds
acquisition and leaves room before the existing job timeout; it may defer work.

## Validation and remaining limits

All **171 offline regressions passed** with
`.venv-build-linux/bin/python -B tests/run_tests.py` (4.325 seconds). They cover
independent limits and cooldowns, reviews starting
before details finish, old values/timestamps on failure, future retries,
transaction rollback, actual row-update counters, deadlines, HTTP 401/403,
repeated bad response contracts and database failures. The additional live
reviews-only check used `--steam-limit 0 --steam-reviews-limit 3`, obtained and
persisted all 3 summaries in 4.115 seconds, issued no Store acquisition calls,
and reported `status=ok`. The combined three-app check also reported `status=ok`.
Both used disposable data and `--skip-catalog --hltb-limit 0`.

The workflow's nine multiline shell blocks passed `bash -n`; comparison against
the original workflow confirmed unchanged cron, HLTB budget/interval and job
timeout. `git diff --check` also passed.

Live test databases, reports and historical logs are local under
`/tmp/pecetracker-steam-audit/`; they are disposable diagnostics. No workflow was
triggered, no push was made and no release asset was published by this audit.

A sustained 2500-app run from GitHub Actions remains unmeasured because this
change has not been pushed or dispatched. Restoring the budgets removes the
artificial 600-app ceiling; it cannot guarantee every request succeeds or that
every run finishes in 1h40. The summary must distinguish selected, attempted,
committed and deferred work. Raw and compressed release asset pairs retain the
existing separate promotion/rollback behavior; this change does not create a
single atomic transaction across all release assets.
