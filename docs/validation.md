# Validation scope

## Live collection

Run `60c13c8bd553` on October 29, 2026 used a clean downloaded project folder, a privately saved API key, and the supplied three-keyword configuration. Collection completed successfully, saved one run and three successful SQLite snapshots, and exported three report rows. Returned organic counts were 9 / 7 / 9. The target ranked 3 for `scrapeless` and was absent from the other returned sets. This is a first baseline, not evidence of a multi-day change.

The article's read-only SQLite example was executed against that database. Re-exporting the same run and version produced identical CSV bytes. Public CSV URL paths are generalized, while ranks, counts, states and request timestamps are preserved. See [example reports](../examples/README.md).

## Figure provenance

Project-file and API-key-management figures come from actual user-operated pages. The key, creation-time field and account balance are completely redacted. Later configuration, log, database and CSV figures are actual browser captures of read-only local file inspection pages. The log figure displays retained stdout from a command executed separately; it is not a native-terminal screenshot. Database and CSV figures show selected fields, not a database application or spreadsheet application. No interface or result values were reconstructed.

## Automated and operational scope

The retained collector implementation includes controlled transport, persistence, locking, recovery and report-version checks. Automated tests and synthetic demonstrations do not establish live schedule reliability. No permanent schedule, machine reboot or consecutive multi-day live collection is claimed. The optional browser helper is a local interface, not a hosted service.

Actual website publication of the companion article is a separate acceptance step. Repository visibility and website publication are independent of local program validation.
