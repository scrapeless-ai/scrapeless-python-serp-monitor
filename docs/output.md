# Report fields and interpretation

A report has one row per snapshot and configured target domain. Inputs and live/demo mode isolate comparison series. Rows, target configuration, baseline IDs and the no-baseline outcome are saved in an immutable version; export reads that version instead of recalculating against changing history.

| Field | Meaning |
| --- | --- |
| `run_id` / `snapshot_id` | Batch and current keyword observation IDs |
| `baseline_snapshot_id` | Frozen previous observation ID; blank is a fixed no-baseline/unknown-order result |
| `report_version` / `report_state` | Immutable revision number; `complete` or `partial` collection |
| `submitted_at` | Client timestamp just before initial POST; blank for unknown legacy submissions |
| `received_at` | Client timestamp after a result is received; never claimed as provider collection time |
| `captured_at` | Compatibility alias of `received_at` |
| `time_basis` | `request_interval`, `legacy_unknown`, or blank when unavailable |
| `comparison_state` | `comparable`, `baseline`, `not_comparable`, `legacy_unknown`, or blank for incomplete rows |
| `comparison_note` | Why no numeric change is calculated |
| `keyword` / `target_domain` | Search query and hostname matched within that result |
| `status` | Business, collection or data-quality state below |
| `current_rank` / `previous_rank` | Lowest matching returned organic `position`, or blank |
| `rank_change` | Previous minus current, only for comparable matched observations |
| `current_url` / `previous_url` | Matching URLs, or blank |
| `organic_count` / `previous_organic_count` | Returned organic count in the usable current/previous sets |
| `previous_at` | Previous result receipt timestamp, not provider collection time |
| `gl` / `hl` / `google_domain` / `location` | Requested search conditions |
| `error` | Fixed sanitized incomplete/data-quality explanation |

## Status and coverage

`baseline` / `baseline_absent` means no earlier usable comparable result, with / without a domain match. `up`, `down`, `unchanged` compare two matched positions. `new`, `lost`, `still_absent` describe a target within nonempty usable organic sets. A missing target is not rank 0, rank 100 or proof of falling outside a fixed top ten.

`empty_unverified` means the whole array was empty and has not been confirmed as usable; it is excluded from normal baselines. There is no automatic valid-empty confirmation rule. `pending` and `error` are incomplete observations. None of these three states is a lost/new alert.

`not_comparable` retains a usable current position but leaves previous rank/change blank when request intervals overlap or original order is unknown. A result is eligible as a predecessor only when its receipt upper bound is before the current request submission. This project does not have trustworthy provider collection timestamps. Different returned counts can occur with the same settings; review coverage alongside rank changes.

## Version stability

A completed report repeated with the same `--run-id` and `--version` has identical CSV bytes and baseline IDs. Resume may create a new partial/completed version; use `--version 1` to retrieve the prior one. A successful no-op resume does not create a version. Default filenames contain the run ID and version. An explicit `--output` can overwrite the file you selected; saved database versions remain available.

CSV uses UTF-8 with a byte-order mark, and formula-like text cells are escaped. See [migration](migration.md) for older history and [operations](operations.md) for review summaries.

## Link provenance

Matching uses the normalized destination URL. For recognized Google wrappers, private organic JSON also retains `source_link` and `link_source=google_redirect_location`. Sanitized diagnostics list resolved item indexes. CSV rows contain matching target URLs only; the core never silently drops an unresolved organic item.
