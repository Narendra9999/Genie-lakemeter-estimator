# Live sync and new-agent test — November 4, 2026

Source: `01f1d6efcecd1620b04cf487817675b7`, live update `2026-11-04T19:10:18.120Z`.
New agent: `01f1d883dc591276a3d85615bcfc12bb` ([open](https://fevm-classic-stable-naren.cloud.databricks.com/genie/rooms/01f1d883dc591276a3d85615bcfc12bb)).

All nine registered functions and the entire latest payload were retained. Function bodies, comments and contracts match local source. The setup notebook was regenerated. 21 offline tests pass. Source agent etag was checked unchanged after testing. The new agent shares existing `fevm_catalog_naren.dcp_estimator` UC objects; it tests imported configuration, not a fresh calculator installation. No UC objects or rates were changed.

## Live Agent-mode observations

- Missing batch facts: no SQL, asks runs/month, identities and remaining scope; no estimate.
- Invalid 45 SQL working days: no SQL, rejects value and requests correction.
- SEND_US01 EXPECTED: $610.38 monthly plus $125.00 one-time, with two pending items disclosed and reference list-price basis.
- Corrected batch follow-up: validation before totals, same request retained; $18 monthly, no pending items.
- Explanation follow-up: calls dcp_calc_details on the same request and explains rate, planning-default intensity and LOW confidence.
- Component breakdown: declined because the latest source instructions explicitly exclude it. This is consistent with the imported policy but does not satisfy the earlier desire for DCP component breakdowns. No prompt changes were made to override the live source.

## Remaining issues

Totals responses expose too many raw result columns despite the concise presentation instruction. Component breakdown support requires a future instruction change; the line-item function is retained and available. The explanation additionally stated a confidence threshold beyond the returned rows; this statement was not independently checked here. These tests are observed samples, not comprehensive quality certification. Raw Agent-mode events and summarized results are saved alongside this report.
