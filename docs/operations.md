# Daily operations and recovery

## Run and review

Use macOS or Linux with Python 3.9+, local SQLite storage and working POSIX file locks. Use one fixed configuration, database and artifact directory per watchlist. Configure all four search settings explicitly: `gl`, `hl`, `google_domain`, `location`.

From the installed project root:

```bash
chmod 600 .env
python3 daily_workflow.py run --config "$PWD/config.json" --env-file "$PWD/.env" \
  --db "$PWD/data/rankings.sqlite" --artifacts "$PWD/output/daily"
```

The wrapper resolves paths before execution. The environment key wins over `.env`; the file is parsed, never sourced. It shares `serp_monitor.py`'s database lock. A busy command exits 75 before making requests. The OS releases the lock after a crash; do not remove the sidecar file. Network/shared-volume locking and multiple hosts are outside the tested scope.

Check `output/daily/latest-attempt.json` before opening its referenced summary. Attempt output is unique and does not overwrite a previous attempt. Summaries contain run ID, start/finish times, snapshot counts, report paths, fixed settings, Scrapeless API POST/GET attempt counts and separate Google redirect lookup counts and sanitized validation diagnostics. The review CSV flags declines, changed landing URLs, missing targets, changed coverage, uncertain empty arrays and unknown comparison order. Receipt gaps describe client receipt timestamps, not exact provider collection intervals. Only `tick` declares a planned 24-hour interval.

`latest-success.json` remains on the previous successful collection after a failed attempt. Read it alongside the latest-attempt state. Exit 0 means collection completed, not that every rank is comparable or that no review item exists.

## macOS daily schedule

This example is a UTC target schedule, independent of the operator's local settings. Rendering creates a plist only. Starting it can immediately make paid searches when today's slot is due.

```bash
python3 render_launchd.py --python "$PWD/.venv/bin/python" --project "$PWD" \
  --config "$PWD/config.json" --env-file "$PWD/.env" \
  --db "$PWD/data/rankings.sqlite" --artifacts "$PWD/output/daily" \
  --at-utc 09:00 --output "$PWD/output/serp-daily.plist"
launchctl bootstrap "gui/$(id -u)" "$PWD/output/serp-daily.plist"
```

The job runs `tick` on load and every 60 seconds. The database claims one slot per UTC date at or after 09:00. A missed day is not backfilled; a failed or interrupted claimed slot is not automatically retried. An offline machine cannot collect at the planned time. Repeated ticks and reloads retain the same slot/history. Editing the configured slot can create a different slot; investigate before changing it.

Inspect `output/daily/scheduler.log`, `scheduler.err`, the latest-attempt pointer and the saved run's CSV. To stop the example:

```bash
launchctl bootout "gui/$(id -u)/com.scrapeless.serp.daily.example"
```

A short-interval loopback test verified actual timer execution and unload/reload from another working directory. Reboot behavior and 3-7 consecutive live scheduled days have not been verified. No live schedule was activated as part of this revision.

## Backup and restore

Use a new backup destination. The SQLite backup API preserves a consistent history while holding the shared lock.

```bash
python3 daily_workflow.py backup --db "$PWD/data/rankings.sqlite" \
  --artifacts "$PWD/output/daily" --destination "$PWD/backups/rankings-backup.sqlite"
```

To restore, stop the scheduler and every collector, preserve the current database, then copy the chosen backup to the configured database path. Keep the database and its metadata together; CSVs alone cannot restore task IDs or frozen baseline IDs. The next command checks/migrates its schema. After restoring a database older than a paid submission, investigate missing task state before starting new searches.

## Recovery table

| Condition | Identify | Action | New search risk | Verify afterwards |
| --- | --- | --- | --- | --- |
| Invalid/missing key or 401 | Exit 1, fixed key hint; remaining items may be queued | Fix credentials and inspect history; resume the chosen run | Previously queued items may submit; failed no-ID item stays failed | Snapshot counts and report version |
| 402 or 403 | Balance/access hint | Resolve account access, inspect failed and queued items | A deliberate new run consumes credits | No stale success pointer interpreted as this attempt |
| 429 | Rate-limit hint | Wait, reduce frequency and inspect the run before resuming | Queued items may submit | POST/GET counts and partial rows |
| Task ID saved, polling timeout | `pending`, saved task ID | Resume same database and run | Saved IDs use GET only; queued items may POST | New report version and saved successful rows |
| Network interruption without task ID | `error` or `requesting`, no ID | Investigate task outcome; do not blindly restart the batch | Provider may already have accepted POST | No automatic repost; decide explicitly whether to start another run |
| Partial batch or repeated interruption | Exit 1/130 or process killed | Keep database; inspect history and resume | Successful items skip; saved IDs GET; never-submitted queued items may POST | Old version remains available; inspect latest-attempt state |
| Empty organic array | `empty_unverified` | Review data quality; no automatic confirmation rule | New run is a deliberate new search | It was excluded from baseline and lost/new alerts |
| Invalid destination URL | `error`, diagnostics list invalid indexes | Check API schema/support using sanitized evidence | Do not blindly retry or silently drop invalid entries | No failed collection treated as a ranking loss |
| Unknown/overlapping timing | `not_comparable` | Review intervals or migration provenance | Export makes no API calls | Current rank may exist; numeric change stays blank |
| Busy process | Exit 75 | Wait for the owner; after a crash the OS releases the lock | No request from the rejected process | Lock is not manually deleted |

```bash
python3 serp_monitor.py history --db "$PWD/data/rankings.sqlite"
python3 daily_workflow.py resume --run-id YOUR_RUN_ID \
  --db "$PWD/data/rankings.sqlite" --artifacts "$PWD/output/daily" --env-file "$PWD/.env"
python3 serp_monitor.py export --db "$PWD/data/rankings.sqlite" \
  --run-id YOUR_RUN_ID --version 1 --output "$PWD/output/saved-v1.csv"
```

## Request volume and validation

For K keywords and F new collections, plan up to K * F new POSTs, plus previously unsubmitted queued keywords explicitly resumed later. Multiple target domains reuse each result. Polling GETs are counted separately; request counts do not establish prices or billing rules. No automatic POST retry or automatic paid backfill is implemented.

On macOS, run the additional real-process/OS-timer harness against an isolated loopback service:

```bash
python tests/run_feasibility.py /tmp/serp-controlled-check
```

It creates and removes a temporary controlled timer; it does not install the live daily job. Keep the output directory new. See the validation scope in `README.md` for retained real-call boundaries.
