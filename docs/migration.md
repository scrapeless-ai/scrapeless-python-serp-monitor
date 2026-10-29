# Database schema version 2 migration

Stop other collectors and preserve your database. On the first `collect`, `history`, `export` or daily-workflow command, the program takes the shared database lock and checks `PRAGMA user_version`.

- An original unversioned database is copied using SQLite's backup API to `<database>.pre-v2.sqlite` with private file permissions before schema changes. An existing backup of that name is never overwritten: preserve and move it aside before retrying a migration.
- The upgrade adds submission/receipt timestamps, timing provenance, frozen baseline fields, minimized diagnostics, a run report-version pointer and `report_versions`. It preserves run IDs, snapshot IDs, configurations, raw JSON, organic JSON and task IDs.
- The old `captured_at` is retained and copied to `received_at`; it is a local result-processing timestamp, not a verified provider collection time. Old request submission time remains unknown.
- Legacy successful rows remain visible. Their new exports use `not_comparable`; old change directions cannot be reliably reconstructed. Archived CSV files on disk are not rewritten. Original empty successes are quarantined as `empty_unverified`, with original data retained.
- A future new request may use a nonempty legacy result whose known receipt time is before the new request starts. That upper bound establishes order for the new comparison without inventing the older submission time. Unknown upper bounds or overlapping intervals remain non-comparable.
- A legacy saved task ID can still be resumed using GET, but missing original submission timing remains unknown. Successful items skip; uncertain no-ID submissions are not reposted.
- The first new export saves version 1 of the migrated interpretation. A resumed partial run can create version 2; completed saved versions do not change. Baseline IDs, including NULL, stay fixed. No implicit recomputation option is exposed.
- Reopening version 2 is idempotent. A newer unsupported schema is rejected. SQL field additions and metadata changes are transactional. If migration fails, stop writers and restore the retained pre-v2 copy to a separate verified path before replacing the working database.

## Verify

```bash
python3 serp_monitor.py history --db data/rankings.sqlite
python3 serp_monitor.py export --db data/rankings.sqlite --run-id YOUR_RUN_ID --output output/migrated.csv
```

Both commands make no API calls. Check the row count, old IDs, task IDs, `time_basis`, report version and blank changes for unknown legacy order. Keep database backups private; they may contain raw search responses and private inputs. This migration does not repair or reinterpret the separate previously exported historical CSV files.
