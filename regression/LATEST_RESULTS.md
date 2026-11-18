# Original recurring RAG profile restored — November 18, 2026

User reverted the 85/15, one-time-only embedding/enrichment change. Restored RAG_REFERENCE_V1 weights 330:300:215:132, normalized to the supplied total API call volume. Recurring scope again includes Nano routing, GTE query/incremental embeddings, GPT enrichment and Claude final answers. Initial GPT/GTE full-corpus indexing remains a separate one-time operation. Removed RAG_REFERENCE_V2 and answer_call_share support; new regression checks prevent loss of recurring embeddings/enrichment and reject the withdrawn profile.

- Local tests: **66 PASS**; whitespace checks PASS.
- Restored fevm validation agent/schema and synchronized setup DAB; configuration read-back PASS. Evidence: `benchmark/results/first-estimate-deploy-20261118T184459Z/`.
- Live Agent Mode `rag-planning-row`: **PASS**, with monthly figures matching direct calculator results. Evidence: `benchmark/results/agent-regression/20261118T184524533867Z/`.
- For the 990-total-calls/day planning scenario, monthly LOW/EXPECTED/HIGH again $106.08 / $481.93 / $857.78, plus one-time $509.29 / $530.54 / $551.79. LOW confidence remains mandatory; model/token/corpus assumptions are disclosed and retrieval/vector search remains unpriced.

Earlier V2 results below are historical and superseded by this restoration. Original agent, source calculator pricing formulas and AWS source data unchanged.

---

# RAG V2: 85/15 recurring split and one-time setup — November 18, 2026

User-approved policy: recurring Model API calls are 85% claude-sonnet-4-6 answers and 15% gpt-5.4-nano routing/reranking. GPT enrichment (gpt-5-4) and GTE embeddings (gte-large-en) are one-time initial setup only. Indexing with unspecified frequency defaults to initial load; explicit recurring/both requests require explicit canonical components. Retrieval/vector search remains unpriced, not a model-call percentage. RAG_REFERENCE_V2 is the default for new scenarios; V1 stays executable for historical reproducibility.

- Local suite: **67 PASS**; whitespace checks PASS.
- Isolated fevm validation agent/schema updated; setup DAB synchronized. Original agent and pricing source data untouched.
- Live direct acceptance PASS: recurring LOW/EXPECTED/HIGH $493.00 / $2,050.49 / $3,607.98; initial setup $509.29 / $530.54 / $551.79; LOW confidence. Recurring line items contain only Claude/Nano; one-time line items contain only GPT/GTE. Evidence: `benchmark/results/rag-v2-direct-20261118T184046Z/`.
- Live Agent Mode `rag-planning-row`: **PASS**; returned monthly scenario figures match direct calculator results. Evidence: `benchmark/results/agent-regression/20261118T184032157525Z/`.
- The larger recurring estimate reflects 85% of 990 calls reaching Claude versus approximately 13.5% in the previous profile, with unchanged token assumptions and rates. These are total model API calls, not necessarily user requests. No retrieval/vector-search charge is included.

---

# Conditional RAG model planning — November 18, 2026

Reviewed the latest turn in fevm validation conversation `01f1e2d7289f1bb088ef8836c0a78bb2`: the agent withheld the estimate for the RAG intake row because no model was specified. Added `_model_planning` profile `RAG_REFERENCE_V1`, based on source-calculator model/token/corpus assumptions. It requires first-estimate mode, normalizes the source role split from 977 total API calls/day to the supplied 990, audits the hypothetical inputs, forces LOW confidence and prices initial indexing separately when selected. Explicit model components cannot be overwritten by this profile; invalid values and explicit scope conflicts still block. Other engines stay UNKNOWN and unpriced. Core calculator formulas unchanged.

- Local tests: **65 PASS**; diff whitespace check PASS.
- Deployed only to validation agent `01f1dec23c071fe68b29cab8c42f3fe8` and schema `fevm_catalog_naren.dcp_phase1_dab_validation`; DAB setup synchronized. Original agent and AWS source data unchanged.
- Direct live acceptance PASS: monthly LOW/EXPECTED/HIGH $106.08 / $481.93 / $857.78; one-time $509.29 / $530.54 / $551.79; confidence LOW; four infrastructure areas pending. Evidence: `benchmark/results/rag-planning-direct-20261118T182937Z/`.
- Agent Mode: **2 PASS / 0 FAIL** (`rag-planning-row`, `model-token-row`). The missing-model row produced scenario costs, per-role models/token ranges, full-corpus assumptions, a chart, generated labels and refinement questions. Monthly figures match direct calculator output. Evidence: `benchmark/results/agent-regression/20261118T182919716956Z/`.

These amounts differ from the user's sample because the original source scenario allocated only 977 calls/day; the new conditional scenario allocates all 990 supplied API calls. API calls are not assumed to mean 990 final-answer user requests. Initial indexing volume remains hypothetical, independent of daily call count. No claim of full conversational regression coverage or Mastercard deployment is made.

---

# First-estimate flow and automatic identities — November 17, 2026

Deployed only to fevm validation agent `01f1dec23c071fe68b29cab8c42f3fe8`, schema `fevm_catalog_naren.dcp_phase1_dab_validation`. Original agent, AWS source table and Mastercard environment unchanged. Missing/null/blank IDs and names are generated automatically; supplied identities are preserved. Never ask for identities, even as optional refinement questions.

- Local suite: **62 PASS**; `git diff --check` PASS.
- First-estimate mode prices supported identifiable areas, labels unknown/deferred areas unpriced, discloses numeric defaults and LOW confidence. Strict validation still rejects invalid quantities; calculator formulas unchanged.
- Updated four legacy-builder examples to canonical first-estimate JSON and revised function descriptions to retain audited default provenance. All four examples passed live EXPLAIN; configuration read-back passed. Generated setup synchronized via fevm DAB deploy.
- Direct live acceptance: **PASS** for automatic blank/null identities, pending scope, LOW confidence, chart output, strict scope validation and negative-input rejection. Evidence: `benchmark/results/first-estimate-acceptance-20261117T222918Z/`.
- Latest Agent Mode run: pasted spreadsheet, per-call model tokens and automatic-default prose **PASS**. Monthly token response displayed correct scenario-labelled amounts in a text chart, initially rejected by a table-only verifier. Corrected parser with positive and negative tests; saved-response replay **PASS** against direct calculator totals. Original run report remains unchanged: `benchmark/results/agent-regression/20261117T222915476243Z/`.
- Earlier run `20261117T222051369235Z` retains failures for advanced JSON translation and negative-runtime intake (function called before unresolved-input clarification), alongside legacy-builder failures fixed in this iteration. Those two conversational cases were not rerun after the structured-context update; full-suite compliance is not established.

User policy now explicitly permits a provisional priced-area subtotal while other areas remain UNKNOWN. Historical UNKNOWN-scope failures below reflect the previous stricter policy. No live replay was substituted for a new independent conversation, and no failed report was overwritten.

---

# Automatic-default clarification fix — November 17, 2026

Reviewed full Agent Mode conversation `01f1e2d15e8f1d6485671eccdab35786` on fevm validation agent `01f1dec23c071fe68b29cab8c42f3fe8`. The initial scheduled-batch request already defaulted runtime and calculated. Later business-table turns mixed unresolved service choices with default quantities and re-asked whether explicitly selected SQL reporting should be included.

Updated instructions only on the same fevm test agent: defaults apply automatically without opt-in; prose/table TBD quantities are eligible for defaults; explicitly selected reporting is not reconfirmed; unresolved service questions are separated from quantity defaults. Explicit JSON null, unsupported choices and conflicting facts retain their checks. No calculator functions or prices changed in this focused pass. The generated setup was rebuilt and uploaded to keep future deployments aligned. Instruction read-back matched after API content-chunk normalization, with all other agent configuration unchanged.

Validation: **53 local tests PASS**; **4/4 focused live cases PASS**: `automatic-defaults-prose`, `selected-reporting-defaults`, `defaulted-quantities`, and `translate-confirm-validate-calculate`. Each completed estimate was checked against direct scenario totals. Evidence: `benchmark/results/agent-regression/20261117T220233834419Z/`; config before/after under `benchmark/results/default-opt-in-fix-*/` (Git-ignored).

This pass does not resolve or erase the separately recorded UNKNOWN-area regression below. Defaults supply quantities; they do not establish whether an unspecified service is in scope.

---

# Defaults deployment validation — November 17, 2026

Target: `fevm-naren`, isolated schema `fevm_catalog_naren.dcp_phase1_dab_validation`, existing test agent `01f1dec23c071fe68b29cab8c42f3fe8`. The original working agent, original calculator schema and AWS pricing source were not modified.

- Source defaults reviewed: `ProcessingComponent`, `SqlReporting`, `DataTransfer`, `AwsStorage`, `EstimateRequest` and `PlanningAssumptions`. Dataclass numeric defaults and applicable engine planning defaults are read directly from the calculator; supplemental baseline quantities apply only where no usable source default exists. Explicit user values and assumption overrides retain precedence. Token EXPECTED midpoint fallback is preserved.
- Local tests: **53 PASS**; generated artifacts rebuilt; strict fevm bundle validation PASS.
- Setup run `755126810261917`: **SUCCESS**, updating the existing test agent. Rate refresh schedule remains configured PAUSED.
- Direct deployed acceptance: **PASS** for instruction read-back, generated/preserved IDs, source default disclosure, LOW confidence with omitted quantities, HIGH confidence when only identities default, explicit null/negative blocking, and local/live parity with the same live rates (including five-engine fixture).
- Agent Mode regression: **7 PASS / 1 FAIL** across eight cases. New `defaulted-quantities` case PASS: correct scenario costs, generated identity, disclosed runtime/run count/DBU assumptions, LOW confidence, and no insertion of default values as user-supplied facts.
- Default scheduled-batch example: LOW **$13.50**, EXPECTED **$27.00**, HIGH **$40.50** recurring/month at the validation workspace's list rates; these figures are validation evidence, not customer pricing.

**Known issue remains:** `unknown-area-state` produced processing figures while DataSync remained UNKNOWN. Conversation `01f1e2b268221306af6bad4da0275b18`. It disclosed the excluded unknown area, but did not ask to resolve scope before running functions and presenting figures. This failure remains enabled and is not a regression-test pass or a resolved rollout issue.

Evidence (Git-ignored):

- `benchmark/results/defaults-acceptance-20261117T181113Z/`: configuration, live query outputs, summary and parity assertions.
- `benchmark/results/agent-regression/20261117T181035041962Z/`: all eight case results, streams, requests, direct totals, report and JUnit XML.

The `missing-expected-runtime` case now uses an explicit null (unknown) to verify blocking. Omitted numeric fields intentionally use documented defaults under the new policy. Historical runs below tested their then-current policy and have not been rewritten. No Mastercard deployment was performed.

---

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
