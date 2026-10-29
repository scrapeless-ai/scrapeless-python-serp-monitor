# Example reports

`live-current.csv` is the redacted report from live run `60c13c8bd553` on October 29, 2026. Three successful keyword observations produced three CSV rows, with returned organic counts 9 / 7 / 9. The target appeared at position 3 for `scrapeless` and was absent from the other two returned sets. This is a first baseline.

Matching URL paths are generalized; ranks, counts, states and request timestamps are unchanged. `live-current.summary.json` records separate original and published hashes. The working credential, raw responses and SQLite database are private.

`migrated-history.csv` is a migration example, separate from the current live run. The `demo` command generates synthetic two-round reports without a key or API calls. Search positions and returned counts can vary between live runs.
