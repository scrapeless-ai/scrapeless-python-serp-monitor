# CLI reference

Use Python 3.9+ on macOS or Linux with local SQLite storage. All collection paths use the same POSIX database lock. Numeric timeouts must be finite and positive. Run from the project root or supply explicit paths.

## collect

| Option | Default | Purpose |
| --- | --- | --- |
| `--config PATH` | `config.json` | Watchlist and settings for a new run |
| `--env-file PATH` | `.env` | Local key; environment wins |
| `--db PATH` | `data/rankings.sqlite` | Persistent SQLite path |
| `--output PATH` | `output/<run-id>-v<version>.csv` | CSV destination |
| `--resume RUN_ID` | Unset | Use saved configuration and task IDs |
| `--timeout SECONDS` | 60 | Per-request network timeout |
| `--poll-timeout SECONDS` | 120 | Polling deadline per pending attempt |
| `--poll-interval SECONDS` | 2 | Normal interval; transient GET failures back off |

New default runs submit up to three searches. Resume skips success, polls saved task IDs, and may submit never-submitted queued keywords. Empty-unverified rows and uncertain failed requests without IDs are not automatically resubmitted. Keep the same database. Older schemas migrate before use; see [migration](migration.md).

## history

```bash
python3 serp_monitor.py history --db data/rankings.sqlite
```

Prints run ID, start time, mode and status, newest first. No API calls.

## export

| Option | Default | Purpose |
| --- | --- | --- |
| `--db PATH` | `data/rankings.sqlite` | Source database |
| `--run-id RUN_ID` | Latest created run | Saved report selection |
| `--version INTEGER` | Latest saved version for that run | Immutable report revision |
| `--output PATH` | `output/<run-id>-v<version>.csv` | Destination |

```bash
python3 serp_monitor.py export --run-id YOUR_RUN_ID --version 1 --output output/saved-v1.csv
```

No API calls. The selected version is read from saved rows. A previously unexported migrated or interrupted run gets its first saved interpretation; completed versions are not recalculated.

## demo

```bash
python3 serp_monitor.py demo --output-dir output/demo
```

Two synthetic rounds in an in-memory database; no key, API requests or live database changes. Writes `round-1.csv` and `round-2.csv`; repeated demos overwrite these demo-only paths.

## Daily operations

`daily_workflow.py` supports `run`, `resume`, `tick` and `backup`. It requires `--db` and `--artifacts`, resolves them absolutely, and shares the core lock. It defaults `--config` and `--env-file` to the project directory. `resume` requires `--run-id`; `backup` requires a new `--destination`. Timeout options match `collect`; `tick --at-utc HH:MM` defaults to `09:00`. `--controlled-url` is a test-only HTTP loopback origin using a dummy token and demo mode; it cannot send a real key to an arbitrary endpoint. See [operations](operations.md) for commands, schedule activation and recovery.

## Exit codes

`0`: command/collection completed; review may still be needed. `1`: incomplete work, data-quality failure or preflight failure. `2`: argparse usage error. `75`: busy database, no request from rejected collector. `130`: interrupted original CLI. Forced process termination has OS-specific status. On an interrupted daily attempt, the latest-attempt pointer remains incomplete/running until an explicit recovery completes.

Private files include `.env`, database backups and successful raw response JSON. Keep them outside published packages. Do not delete a lock sidecar to recover a process.

## Beginner local page

Run `python3 tutorial.py` from the extracted project root and open the printed loopback URL. The default is http://127.0.0.1:8791/; `--port` selects another local port. The page saves the key privately, never returns it, and runs only after an explicit click. Collection uses the same core lock and frozen reports as collect. The startup command itself makes no search requests.
