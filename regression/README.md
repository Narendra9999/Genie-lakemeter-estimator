# DCP estimator regression tests

## Local checks

From the repository root:

```bash
python3 -I -m unittest discover -s tests
```

These tests run without Databricks credentials or network access. Alongside existing pricing, breakdown and generated-artifact tests, `tests/test_agent_regression.py` verifies:

- Source-default precedence, generated identifiers, LOW confidence and explicit unknown preservation are covered in `tests/test_planning_defaults.py`.
- The raw alias-based JSON is still rejected by the strict calculator.
- The reviewed canonical request is accepted.
- Explicitly unknown required runtime (null), negative runtime and an unconfirmed area block calculation and produce no totals. Omitted quantities and scenario bounds use documented defaults with LOW confidence.
- Regression assertions detect changed identifiers, runtimes, DBU coverage and Not Applicable choices, including loss of the inactive SQL identifier.
- No function call is allowed before non-defaultable inputs and prepared-request confirmation are resolved. Omitted IDs/display names are generated; they no longer require a naming question.
- Input checking must precede totals and both must use identical JSON.
- Each scenario's displayed monthly amount must match its calculator result; swapped amounts, missing/extra scenarios, pending amounts and omitted confidence fail.
- Raw validation tables and nonzero dollar figures are rejected during intake, even without calculator calls. Explanations that pending does not mean $0 remain allowed.
- Behavioral regressions are reported separately from operational errors, and repeat results are retained.

The fixtures are `tests/fixtures/translation_request.json` (the supplied external format) and `translation_prepared.json` (the reviewed canonical equivalent). They are test data, not a new permissive calculator parser.

## Live Agent Mode checks

Requires the Databricks Python SDK in your Python environment and an authenticated, explicitly selected profile. Install with `python3 -m pip install databricks-sdk` if needed.

```bash
python3 regression/agent_regression.py \
  --profile fevm-naren \
  --agent-id 01f1d6efcecd1620b04cf487817675b7
```

Replace the profile and agent ID for another deployment. No profile or agent is selected automatically. This invokes the Agent Mode Responses API, not Chat mode. It creates test conversations and executes read-only calculation queries; it does not update instructions, functions, prices, tables or agent configuration. Workspace execution can incur normal usage charges.

Cases:

| Case | Required behavior |
| --- | --- |
| Translate, confirm, validate, calculate | Explain the supplied identifiers and missing names/DBU scenario scope; make no calls; after answers show canonical JSON and request approval; only after approval check inputs and calculate the identical request. Preserve every supplied scope choice and identifier, without re-asking explicit inactive-area questions. |
| Conflicting identifiers | Identify both request IDs and ask which applies; make no function call. |
| Negative expected runtime | Explain invalid runtime; no function call or estimate table. |
| Missing expected runtime | Ask for the required expected runtime; no function call or result table. |
| Unknown area state | Ask about the unresolved area; no function call or result table. |
| Conflicting area statuses | Resolve the alias/canonical scope conflict; no function call or result table. |
| Revised prepared inputs | When the user changes run count after a draft, show the revised canonical JSON and obtain fresh confirmation before checking/calculating it. |

Prepared JSON is compared structurally, not through exact wording. Tests allow additional calculator defaults but require unchanged user facts. Final monthly totals are compared by scenario to a fresh call to the deployed calculator using the confirmed request, so changing a price does not require hard-coding new expected dollar values. This checks consistency with the calculator, not an independent audit of its pricing formulas. Local pricing/formula tests cover those separately.

The live suite requires direct named `p_request` JSON in the check/totals calls, so it can prove they match the approved draft, including preserved metadata. A failure is not retried automatically. Use explicit independent repeats to measure intermittent behavior; every result is retained and any failure makes the run nonzero.

Select one or more cases, or omit `--case` to run all seven:

```bash
python3 regression/agent_regression.py \
  --profile fevm-naren \
  --agent-id 01f1d6efcecd1620b04cf487817675b7 \
  --case missing-expected-runtime \
  --case unknown-area-state \
  --repeat 3
```

Each case/repetition uses new conversations and distinct artifact filenames. These are independent samples, not retries until success.

Evidence is written to a timestamped directory under `benchmark/results/agent-regression/` (Git-ignored):

- Exported agent configuration, including its version metadata.
- Stream events and completed responses for every turn.
- Direct calculator totals used for comparison.
- `results.json`, with per-case/per-repeat PASS, FAIL (behavior), or ERROR (execution) and reasons/duration.
- `REPORT.md`, a readable result matrix, and `junit.xml` for CI test reports.
- `run.json`, with the explicit profile/agent, selected cases, repeat count, Git revision/dirty flag, runner/fixture source hashes and live configuration hash/update time. A detected configuration change during the run makes the run nonzero.

The process exits `0` only when every selected case/repetition passes and configuration remains stable; it exits `1` on a regression or recorded execution error. Operational errors also exit nonzero. Use `--timeout 300` to set the per-turn/SQL time limit and `--output PATH` to choose an evidence directory. Keep raw evidence private if you adapt fixtures to customer workloads.

## CI and release use

Run local tests for every code change. Run the live command explicitly against a test agent before promoting instruction or certified-function changes, and against the target agent after deployment. A CI job should provide its own authenticated Databricks profile, invoke the same command and publish the resulting `junit.xml`; the suite does not provision credentials or change your deployment. Review failures and saved SQL/answers before deciding whether behavior or a test expectation should change.

This is a targeted suite for the JSON-translation regression. It does not claim complete coverage of every engine, request shape, STRESS scenario, or presentation style. Add fixtures/cases when those behaviors change; never treat a single successful live run as a guarantee against model variability.

See [LATEST_RESULTS.md](LATEST_RESULTS.md) for the initial execution results, including the missing-runtime flow regression found by the live suite.

The live suite now includes `defaulted-quantities`: a canonical scheduled-batch request with omitted numeric inputs must calculate, disclose default values, report LOW confidence, and match direct calculator totals. The legacy `missing-expected-runtime` case uses explicit null under the current policy; omission is now defaultable.

Automatic-default conversation checks:

- `automatic-defaults-prose`: TBD runtime in prose is omitted, defaulted and calculated without an approval question; LOW confidence and defaults must be disclosed.
- `selected-reporting-defaults`: explicitly requested SQL reporting and supplied concurrent users are preserved and calculated without another inclusion/default approval question.

Defaults do not determine whether an unspecified service is used. Genuine service-selection questions remain valid; these tests explicitly settle the other areas so they isolate default opt-in behavior.
