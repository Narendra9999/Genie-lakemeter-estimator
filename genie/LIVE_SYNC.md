# Live agent → code sync — November 2, 2026

Source: `fevm-naren`, agent `01f1c809927f1586bbd2566786d9c455`, updated `2026-11-02T19:02:17.134Z`.

The current live policy is DCP totals only; Lakemeter, cluster sizing, line items and details are out of scope. The full export is preserved, including its broader function registrations and older examples. No pruning or redesign was applied. The separately created DCP Calculator agent remains separate.

Synchronized instructions, full portable payload, flat compatibility fields, descriptions, tables/column metadata, function registrations, examples, sample questions, benchmarks, joins and IDs. Regenerated the one-time setup notebook from these sources.

Reviewed all 42 functions in the source schema, including registered functions and dependencies. Signatures, return types and calculation bodies match the codebase. Updated `dcp_check_inputs` and `dcp_calc_totals` descriptions in `calculator/build_sql.py` and generated SQL to match the live definitions exactly. The portable metadata/body snapshot is `live_function_definitions.json`. The catalog renders `get_dbu_price` with an additional redundant pair of parentheses; it has no calculation difference.

Live text says either validator/totals may validate first, while the live function comments direct new requests to validation first. Both are preserved as exported; this sync does not resolve or redesign that live behavior.

Checks: 29 offline tests passed, including all 42 function bodies, signatures, return types and comments; full exported payload/IDs and installer parity; and calculator regressions. The five-engine regression now reads a dedicated test fixture rather than a removed Genie example. No new live conversations or evaluations were run. The agent's etag, payload and update timestamp were checked unchanged after the sync.

No agent, UC object, data, rate or permission was modified remotely. Backups are in the timestamped `live_sync_backup_20261102T193004Z` folder. Setup was regenerated locally, not executed against a workspace.
