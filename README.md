# DCP Cost Estimator

A standalone DCP calculator and Genie agent configuration. The calculator supports five engines:

| Engine | Workloads |
| --- | --- |
| Processing | Scheduled batch, event-driven batch, continuous streaming, ML training, batch scoring |
| SQL Reporting | Concurrent SQL users and reporting usage |
| DataSync Transfer | Recurring transfers and one-time migration |
| S3 and KMS | Retained storage, file requests and encryption |
| Model API | Recurring model calls and one-time token jobs |

Genie collects facts, asks for missing inputs and explicit area states, and returns monthly and one-time totals. The current agent policy presents totals; component and engine breakdown functions remain available through the calculator API. An omitted category is not automatically not applicable. Missing costs stay pending rather than becoming zero.

## Code layout

- `notebooks/dcp_estimator.py`: calculator core and named presets.
- `calculator/dcp_adapter.py`: request validation and SQL result adaptation.
- `calculator/build_sql.py`: generates the nine DCP UC functions and rate/request tables.
- `calculator/build_engine_inputs.py`: generates the input-schema reference for the five engines.
- `calculator/rate_refresh.py`: loads DCP Databricks rates from system billing.
- `genie/`: DCP-only instructions, SQL examples, function snapshots and benchmarks.
- `samples/requests/`: named DCP workload requests.
- `setup_genie_dcp_agent.py`: generated, self-contained installation notebook.
- `notebooks/09_load_rate_card.py`: generated DCP rate-refresh notebook.
- `tests/`: calculator, input schema, rates, function contracts and setup regression checks.

The local configuration is a DCP-only subset of the working agent described in [genie/SOURCE.md](genie/SOURCE.md). All `__FQ__` placeholders are replaced with the deployment catalog and schema.

## Build and test

```bash
python3 calculator/build_all.py
python3 -m unittest discover -s tests
```

Generators use Python's standard library. Running the installation notebooks additionally requires Databricks compute, Spark and the Databricks SDK. No external pricing CSVs are needed.

## Install

Import `setup_genie_dcp_agent.py` into your workspace and run it with:

| Parameter | Value |
| --- | --- |
| `catalog` | Target UC catalog |
| `schema` | Target schema; defaults to `dcp_estimator` |
| `warehouse_id` | Genie-compatible SQL warehouse ID |
| `rate_source` | `ACCOUNT_PRICES` for account pricing; `LIST_PRICES` for reference pricing |
| `sku_region` | Billing SKU suffix; defaults to `US_EAST_N_VIRGINIA` |
| `space_id` | Blank creates a new agent; provide the returned ID on subsequent runs to update it |
| `space_title` | Defaults to `DCP Cost Estimator` |

The installer creates the schema, DCP functions, rate card, input reference and named requests, then creates or updates the specified agent. The installer principal needs permission to create these UC objects, read the selected system billing table, and use the warehouse. Share the agent and grant intended users access to its catalog, schema, tables and functions through your workspace's normal access-management process.

Databricks prices come only from the selected billing table. Exactly one current USD/DBU price is required per SKU; missing or ambiguous prices remain `PENDING`. `LIST_PRICES` is labeled `REFERENCE_ONLY`. AWS prices retain the calculator registry's source/status. The provided rates and requests are planning inputs, not invoice guarantees.

## Deploy with a bundle

Use an explicitly selected Databricks profile:

```bash
databricks bundle validate --profile YOUR_PROFILE -t dev \
  --var catalog=YOUR_CATALOG,warehouse_id=YOUR_WAREHOUSE

databricks bundle deploy --profile YOUR_PROFILE -t dev \
  --var catalog=YOUR_CATALOG,warehouse_id=YOUR_WAREHOUSE

databricks bundle run setup_agent --profile YOUR_PROFILE -t dev \
  --var catalog=YOUR_CATALOG,warehouse_id=YOUR_WAREHOUSE
```

The bundle contains only `setup_agent` and `rate_refresh`. Rate refresh is paused by default; enable it with `schedule_pause_status=UNPAUSED` when wanted. For later setup runs, include `space_id=YOUR_AGENT_ID` so the same agent is updated. Production uses the `prod` target and requires `run_as_service_principal`.

## Calculator functions

Start new workloads with `dcp_check_inputs(p_request => '...')`; use `dcp_request(...)` to build JSON from plain-language facts. Answer blocking questions and explicitly confirm all six request sections: processing, SQL, transfer, storage, recurring model endpoints and one-time model jobs. Then use `dcp_calc_totals` with the completed request. Named presets use `p_use_case`.

`dcp_calc_line_items` and `dcp_calc_details` expose breakdowns and explanations for direct calculator use. `dcp_intake_questions` lists schema inputs. `dcp_calc`, `dcp_check` and `dcp_calc_run` are internal execution functions. Use named arguments, omit unused parameters, and never pass the string `'NULL'`.

Use Genie Agent mode for the guided conversation. Instructions guide the conversation; they are not a hard execution boundary, and single-query Chat mode may choose functions directly.
