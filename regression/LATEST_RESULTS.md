# Phase 1 DAB validation — November 12, 2026

Target: `fevm-naren`, isolated schema `fevm_catalog_naren.dcp_phase1_dab_validation`, test agent `01f1dec23c071fe68b29cab8c42f3fe8`. The original working agent and AWS source table were not changed.

- Local suite: **44 PASS** before repository documentation cleanup.
- Strict bundle validation: PASS for fevm; Mastercard configuration checked using validation values only, without testing customer access or production service-principal permissions.
- Setup run `55484292803864`: SUCCESS.
- Manual rate-refresh run `155009253420028`: SUCCESS; scheduled refresh remains paused.
- Direct calculator parity: PASS against original schema for five-engine fixture; missing runtime blocks totals; all eleven function signatures avoid DATE/TIMESTAMP certified arguments. Agent configuration read-back matches generated configuration.
- Agent Mode regression: **6 PASS / 1 FAIL**, covering all seven cases. Evidence: `benchmark/results/agent-regression/20261112T175611278568Z/` (Git-ignored).

**Unresolved rollout issue:** `unknown-area-state` returned processing dollar totals while DataSync remained `UNKNOWN`. It disclosed the omitted transfer area, but did not resolve scope before presenting costs. Conversation: `01f1dec2803012388502408c48b841fc`. The assertion remains enabled. Deployment success does not establish issue-free conversational behavior; Mastercard deployment has not been performed.

The translation, conflicting identifiers, negative runtime, missing EXPECTED runtime, conflicting scope, and revised-input confirmation cases passed in this run. Historical failures below remain relevant; a later pass does not erase them.

---

# Phase 1 regression results — November 5, 2026

Current local suite: **43 tests passed**. Expanded live validation: **11 PASS / 1 FAIL / 0 ERROR across 12 independent case executions covering seven cases**. The suite has not been weakened to hide the failure. No agent configuration, calculator source, function or pricing was changed by these tests. All three live runs recorded stable agent configuration.

| Case | Live samples | Outcome |
| --- | --- | --- |
| Translation, prepared-input confirmation, same-request validation/calculation and scenario-specific monthly dollar comparison | 2 | 2 PASS |
| Conflicting identifiers | 2 | 2 PASS |
| Negative expected runtime | 2 | 2 PASS |
| Missing EXPECTED runtime | 3 | 2 PASS / 1 FAIL |
| UNKNOWN area state | 1 | PASS |
| Conflicting alias/canonical area states | 1 | PASS |
| Change run count after prepared JSON, then request fresh confirmation | 1 | PASS |

The missing-runtime behavior remains intermittent. One extended run invoked `dcp_check_inputs` before asking for EXPECTED runtime; other samples asked without a query. The agent showed no estimate in the failing sample. This reproduces the previously recorded no-query-while-incomplete regression, not a pricing discrepancy. A later pass does not erase the failure, and the repeated run correctly exited nonzero.

New checks reject scenario-swapped dollar values, missing/extra scenarios, missing confidence, raw validation tables and nonzero dollar figures during intake. The strengthened no-query/no-figure assertion was also replayed successfully against seven saved intake/draft turns. Operational errors are classified separately from behavioral failures.

Live evidence (Git-ignored):

- `benchmark/results/agent-regression/20261105T235143267534Z/`: missing runtime, UNKNOWN area, conflicting scope; 3 PASS.
- `benchmark/results/agent-regression/20261105T235307619200Z/`: changed prepared inputs and fresh confirmation; PASS.
- `benchmark/results/agent-regression/20261105T235242071859Z/`: four original cases repeated twice; 7 PASS / 1 FAIL.

Each directory contains `results.json`, `REPORT.md`, `junit.xml`, `run.json`, agent configuration, streamed tool-call evidence and completed responses. Financial cases include a direct calculator comparison. No automatic retry-to-pass or deployment was performed. Phase 2 remained separate.

---

## Initial regression run — November 5, 2026

Target: `fevm-naren`, agent `01f1d6efcecd1620b04cf487817675b7`, Agent Mode. This run queried the agent; it did not change configuration or calculator functions.

| Check | Result |
| --- | --- |
| Local suite | 36 tests passed |
| JSON translation, prepared-request approval, same-request validation and calculation, dynamic totals comparison | PASS |
| Conflicting identifiers | PASS |
| Negative expected runtime | PASS |
| Missing expected runtime: text-only clarification before any query | FAIL |

The failed case supplied LOW=0.5 hours and HIGH=2 hours but omitted EXPECTED. The agent called `dcp_check_inputs` before asking for the missing EXPECTED runtime. It correctly asked for that value and produced no estimate, but the tool call violates the existing no-query-while-incomplete policy. The regression assertion remains enabled; the suite exited nonzero. This is a conversation-flow failure, not evidence of an incorrect dollar estimate.

Failing conversation: `01f1d97141c41d76b1cc8f9bb4d7ee2c`.

Raw local evidence: `benchmark/results/agent-regression/20261105T233129026065Z/`, including `results.json`, each streamed response/tool call, configuration and direct calculator totals. Raw artifacts are Git-ignored. The post-run topic assertion also passed on the saved first response: it did not re-ask questions about explicit inactive areas. Agent wording varies, so subsequent runs may produce different outcomes; this record describes this run only.

No automatic fix or retry was applied to conceal the failure. Review the saved SQL and instructions before addressing the agent behavior, then rerun the same suite.
