# DCP Calculator

A separate Genie agent for the five DCP engines. It reuses the installed calculator and rates; it does not change the existing mixed estimator agent or deploy Unity Catalog objects.

The conversation is: identify the engine → validate facts and scope → ask for missing inputs → validate the answers → calculate → explain or break down costs.

Five engines: Processing, SQL Reporting, DataSync Transfer, S3/KMS Storage, Model API. Model API has recurring and one-time request sections, so the validator confirms six sections.

`instructions.md` is the short conversational policy. `build.py` generates the portable `serialized_space.json` and an illustrative `sample_request.json`. One source table provides input schemas; named presets are passed directly to the calculator. Five public functions perform request building, validation, totals, line items and explanations; three internal dependencies are registered for trusted execution. No Lakemeter estimation functions or catalog joins are included.

## Deploy

```bash
python3 build.py
python3 deploy.py --profile fevm-naren \
  --schema fevm_catalog_naren.lakemeter_onebook \
  --warehouse 2b169603bdabc084 \
  --parent-path /Users/narendra.merla@databricks.com
```

The command creates a new agent. To update that agent later, explicitly add `--space-id` with its ID. The returned details are saved in `deployment.json`.

## Behavioral checks

Ask a vague estimate question: expect five engine choices and a discovery question. Give partial batch facts: expect validation and follow-up questions without figures. Explicitly resolve missing facts and all area states: expect calculation. Ask for an engine breakdown in the same conversation: expect the original request, with scenarios and recurrence kept separate. Unsupported products should receive a scope explanation.

Instructions govern conversational behavior; they are not a hard execution gate. Test both generated SQL and the user-facing answer. Existing calculator results can be partial because of missing rates or requested scenario bounds; report those gaps instead of treating them as zero.

## Text instruction justification

The exact policy is in `instructions.md`. Functions and examples encode calculation and validation query shapes. Text remains necessary for turn-by-turn clarification, respecting explicitly stated scope, withholding figures until scope is confirmed, and explaining pending costs. The main regression risk is Genie ignoring the validation sequence or asking again for known inputs. Live tests cover discovery, validation, confirmed calculation, and a breakdown follow-up.

`test_agent_mode.py --profile fevm-naren` checks the multi-step Agent API. `test_live.py` exercises the separate single-query Conversation API, whose function shortcuts may bypass instruction sequencing. Evidence is saved separately for each mode.
