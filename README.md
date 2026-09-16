# Parks Canada Lake O'Hara Cancellation Alerts

Monitors the Parks Canada reservation system for cancellations at **Lake O'Hara
Backcountry Camping** (Yoho National Park) and emails you when a night opens up.

Runs on GitHub Actions. No server, no dependencies outside the Python standard
library.

## How it works

```
GitHub Actions (continuous) → Parks Canada API → email alert when a night frees up
```

Each workflow run polls for ~5h50m at 30-second intervals. Effective check
latency is under a minute.

**Why 30 seconds.** On 2026-09-15 the bot caught two real cancellations, both
single nights. Each was visible on exactly one check and gone by the next one
two minutes later:

```
[21:06:58]  0/19 nights open
[21:08:59]  1/19 nights open   <- appeared, alert sent
[21:11:00]  0/19 nights open   <- gone
```

Lake O'Hara cancellations do not linger. Detection speed is the whole game,
and the alert subject carries the date so it can be triaged from a watch face
without opening anything.

**The cron is not the polling interval.** GitHub's scheduler is unreliable: a
`*/10` cron on this repo actually fired roughly every 4 hours, and an hourly
cron was observed skipping three consecutive hours outright. So each run polls
in-process for just under GitHub's 6-hour job limit, and the cron exists only to
(re)start a run. The concurrency group serialises runs — while one is running
the next is queued and starts the instant the current one ends — so coverage
stays continuous as long as the schedule fires once every ~6 hours.

The cron deliberately avoids minute 0; the top of the hour is the most congested
slot on GitHub's scheduler and the most likely to be dropped.

## Important: this repo should stay public

Public repositories get unlimited free GitHub Actions minutes. The continuous
polling above costs roughly 24 runner-hours per day, which is free on a public
repo and would exhaust the private-repo free tier (2,000 min/month) in about
three days.

If you need the repo private, drop `POLL_DURATION_MINUTES` and accept that
GitHub throttles scheduled workflows to roughly one run every 1-4 hours, or run
the checker on your own always-on machine instead:

```bash
POLL_DURATION_MINUTES=100000 POLL_INTERVAL_SECONDS=120 \
  EMAIL_USERNAME=... EMAIL_PASSWORD=... EMAIL_TO_ADDRESS=... \
  python3 check_availability.py
```

There are no credentials in this repository or its git history — the Gmail app
password lives in GitHub Secrets.

## Reading the Parks Canada API

This is the part that matters, and the part that was wrong for seven months.

`/api/availability/map` is the obvious endpoint and it works fine for
frontcountry campgrounds, returning a clean per-site availability code:

| Code | Meaning |
|-----:|---------|
| `0` | Available |
| `1` | Booked |
| `2` | Outside the booking window |
| `3` | Closed for the season |
| `4` | Site closed |

**It does not work for backcountry zones.** For every Backcountry Zone resource
in the Parks Canada system — Lake O'Hara included — it returns `5` on every
date, regardless of real availability and regardless of query parameters
(`bookingCategoryId`, equipment category, sub-equipment, party size). A detector
built on `== 0` can never fire.

`/api/availability/resourceDailyAvailability` exposes the underlying fields:

| Field | Meaning |
|-------|---------|
| `availability` | `0` = quota free, `1` = taken, `4` = resource closed |
| `restrictionReason` | `0` none · `1` outside booking window · `2` closed for season · `3` backcountry-zone rules |
| `processedAvailability` | what the map endpoint would have returned |

Observed states:

| `processed` | `availability` | `restriction` | Meaning |
|---:|---:|---:|---|
| 0 | 0 | 0 | frontcountry, bookable |
| 1 | 1 | 0 | frontcountry, booked |
| 2 | 0 | 1 | outside booking window |
| 3 | 0 | 2 | closed for the season |
| 4 | 4 | 1 | resource closed |
| 5 | 0 | 3 | **backcountry zone, quota free — an opening** |
| 5 | 1 | 3 | backcountry zone, full |

So a night is open when `availability == 0` **and** `restrictionReason` is not
`1` or `2`. The second half matters: out-of-season dates also carry
`availability == 0`, and without the guard every one of them looks like a
cancellation.

## Which resource to watch

Lake O'Hara's resource tree is a trap. The sub-zone map `-2147483028` contains
sixteen resources with promising names, and **none of them are the campground**:

- `Site 1-10` — alpine climbing bivouacs (Mt. Victoria, Abbot Pass, Mt. Odaray…)
- `LOH Backcountry - Last-Minute Camping 1-5`
- `LOH Backcountry - Staff/Guest/Emergency Camping`

The campground is a single quota-based resource on the **root** map:

```
resourceLocationId  -2147483538   Yoho - Lake O'Hara Backcountry
mapId               -2147483181   root map
resourceId          -2147471963   Lake O'Hara Backcountry Sites
```

## Transient failures vs. real failures

Parks Canada sits behind an Azure WAF that intermittently returns 403 to cloud
IP ranges, GitHub runners included. This is normal and usually clears within
minutes.

The bot distinguishes three states, and the distinction is load-bearing:

| Canary result | Meaning | Response |
|---|---|---|
| `ok` | control zone reads as open | keep polling |
| `broken` | request succeeded, but the detector reports known-open inventory as **full** | abort, email `SELF-CHECK FAILED` |
| `unreachable` | the request itself failed (403, timeout, DNS) | keep polling, re-verify later |

Only `broken` means the bot would silently miss a cancellation. Treating
`unreachable` the same way is what took the bot offline for three hours on
2026-09-16 and sent a false alarm at 03:26 UTC: a single WAF 403 during startup
aborted a six-hour run.

During a run the canary re-verifies every `CANARY_RECHECK_MINUTES`. If the API
stays unreachable for `CANARY_STALE_MINUTES` *and* checks are failing, the run
ends deliberately so the scheduler starts a fresh one — most likely on a
different IP — and the email says *cannot reach Parks Canada*, not *detector
broken*.

## The canary

The original bot ran 4,272 times, reported success every time, and emailed a
healthy status report every morning — while being structurally incapable of
detecting anything.

To make that failure mode impossible, every run first checks a **control
resource**: a backcountry zone that is reliably open, read through the exact
same code path. If the detector reports it as full, the bot emails
`SELF-CHECK FAILED` and exits non-zero instead of quietly reporting "no
availability".

The daily status email does the same, and reports open nights as a measurement
(`0 of 19 nights currently open`) rather than as silence.

It also reports **coverage**, not run count:

```
[PASS] Coverage (last 24h)
       watching 100% of the day across 5 run(s), roughly 2880 checks
       largest blind gap: 0 min
       watching right now: yes
```

Run count is a misleading signal once runs are six hours long — four runs a day
is full coverage, while twelve short ones would be mostly gaps. The check
measures merged job intervals and the largest blind gap, using each run's *job*
timestamps rather than the run's own, since a run queued behind the concurrency
group reports `run_started_at` at queue time and would otherwise count hours of
waiting as coverage.

A coverage gap is a **warning**, not a problem. The subject only says `PROBLEM`
when the bot genuinely cannot detect a cancellation — a broken detector or a
failed read. Anything that still leaves it watching is reported without crying
wolf.

## Configuration

Set in `.github/workflows/check-availability.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `FIRST_NIGHT` | `2026-09-15` | first night to watch |
| `LAST_NIGHT` | `2026-10-03` | last night of the season |
| `MIN_NIGHTS` | `1` | shortest stay worth alerting on |
| `ALLOWED_DAYS` | all 7 | permitted check-in days |
| `ALERT_COOLDOWN_HOURS` | `12` | re-alert interval for a still-open window |
| `POLL_DURATION_MINUTES` | `350` | in-process polling per run (job limit is 360) |
| `POLL_INTERVAL_SECONDS` | `30` | seconds between checks |
| `MAX_CONSECUTIVE_FAILURES` | `10` | consecutive API failures before considering the run blind |
| `API_RETRIES` | `8` | retries per request |
| `API_BACKOFF_CEILING_SECONDS` | `120` | max jittered backoff between retries |
| `CANARY_RECHECK_MINUTES` | `30` | how often to re-verify the detector mid-run |
| `CANARY_STALE_MINUTES` | `60` | unreachable for this long (and failing) ends the run |

Secrets (**Settings → Secrets and variables → Actions**):

| Secret | Value |
|---|---|
| `EMAIL_TO_ADDRESS` | where alerts go |
| `EMAIL_USERNAME` | your Gmail address |
| `EMAIL_PASSWORD` | Gmail [App Password](https://myaccount.google.com/apppasswords), no spaces |

### Rolling to the 2027 season

Update `FIRST_NIGHT` / `LAST_NIGHT` in both workflows. Until Parks Canada opens
the season, those dates return `restrictionReason == 1` and the bot correctly
reports nothing — the daily status email will still confirm the detector is
alive via the canary.

## Testing

```bash
python3 -m unittest test_availability -v           # all tests, hits the live API
python3 -m unittest test_availability.OfflineTests # no network
python3 check_availability.py --self-test          # canary + one read, sends nothing
python3 qa_drill.py --no-email                     # full drill, no email
python3 qa_drill.py                                # full drill incl. real test email
```

The unit suite pins the API state table, the window logic and the alert
cooldown.

### QA drill

`qa_drill.py` answers the question the unit tests cannot: *if a cancellation
appeared right now, would an email with a working booking link actually land in
the inbox?* Seven checks, all against live data, nothing mocked:

1. **Lake O'Hara read** — payload parses, and the observed state tuples are reported
2. **Canary** — detector reports a known-open backcountry zone as open
3. **Windows from real inventory** — open nights become windows and render an alert body
4. **Single-night cancellation** — hunts live frontcountry inventory for a night
   that is open with booked nights on *both* sides, then asserts `MIN_NIGHTS=1`
   catches it and `MIN_NIGHTS=2` does not
5. **Out-of-season not flagged** — post-season nights all carry
   `availability == 0`; asserts none are mistaken for openings
6. **Booking link resolves** — HTTP 200 on the URL an alert would contain
7. **Alert email delivered** — sends a real, clearly-labelled drill email

Run it from the Actions tab via the **QA Drill** workflow (it runs the unit
suite first), or locally with `--no-email`.

## Limitations

- **Does not auto-book.** Cancellations get taken fast; the email has a direct
  booking link, but you still have to click it.
- **Alerts on quota, not on a specific site.** Lake O'Hara is a single
  quota-based resource, so the bot can tell you a night is free but not which
  tent pad.
- **GitHub throttles schedules.** Handled by polling in-process rather than
  relying on cron frequency.
