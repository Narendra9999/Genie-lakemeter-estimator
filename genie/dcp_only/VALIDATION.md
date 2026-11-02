# DCP-only agent validation — 2026-11-02

Created agent: `01f1d6efcecd1620b04cf487817675b7` (DCP Calculator), profile `fevm-naren`.

The existing mixed estimator agent was not updated. No UC functions, tables, rates or permissions were changed. This agent uses one input-schema table and the existing DCP functions. The separate deployment script creates/updates the agent only and requires the existing calculator installation.

## Verified

- All seven saved SQL examples executed successfully against the live warehouse.
- Agent-mode vague request names all five engines and asks which apply.
- Agent-mode incomplete JSON calls dcp_check_inputs first, asks for runs/month and unconfirmed areas, and shows no costs.
- Agent-mode follow-up validates the updated JSON before totals. The batch example returned $18/month, $216/year, zero pending; the answer identifies REFERENCE_ONLY list pricing and the planning-default DBU intensity.
- Agent-mode breakdown reuses LIVE_BATCH and groups line items by scenario, engine, recurrence. It reconciles to $18/month.
- Existing implementation regression suite: 26 tests passed.

## Limits

Use Agent mode for the guided workflow. Single-query Conversation API tests exposed automatic function shortcuts which can skip validation and pass literal NULL strings; removing registered functions caused worse calculation/discovery failures, so that experiment was reverted. Final Agent-mode tests pass with registered DCP functions. This is a conversational policy, not a hard enforcement boundary. Not every engine or pending-rate case has been tested through Agent-mode conversation; existing calculator regression tests cover separate aggregation behavior.

Full Agent-mode event evidence is in validation_agent/. Earlier single-query experiments are in validation/ and are not passing acceptance evidence.
