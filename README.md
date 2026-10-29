# Lakemeter → Genie Cost Estimator

A Genie Agent that answers Databricks cost questions in natural language: DCP Smart Estimation (the DCP calculator,
embedded unchanged), workload sizing, and Lakemeter OSS cost estimates for configured workloads — exposed as Unity Catalog
functions using governed account price cards where available and explicit reference/PENDING statuses otherwise. No app,
Marketplace or Lakebase required.

## Install in one notebook (recommended)

`setup_genie_dcp_agent.py` builds the **complete Genie Cost Estimator Agent** in one run: pricing tables, list-price
functions, workload sizing, the DCP Smart Estimation Calculator v4.0.2 (as Unity Catalog functions), the DCP rate card, named use
cases, and the Genie agent itself (instructions, column synonyms, trusted functions, example and sample questions).
It is self-contained — the only other thing to upload is the `pricing_data/` folder.

1. Import `setup_genie_dcp_agent.py` and the `pricing_data/` folder into the same workspace folder
   (or a Git folder cloned from this repo).
2. Attach to a UC-enabled cluster (DBR 13.3 LTS+) or serverless and run the first cell: it creates the parameter
   widgets at the top of the notebook. Fill them in, then **Run all**:

| Widget | Value |
|---|---|
| `catalog` / `schema` | Target location, e.g. `<catalog>` / `lakemeter` |
| `warehouse_id` | Pro or Serverless SQL warehouse the agent runs on |
| `pricing_path` | Blank = `./pricing_data` next to the notebook (workspace folder or `/Volumes/...`) |
| `rate_source` | `ACCOUNT_PRICES` (governed, production) or `LIST_PRICES` (workspaces without account prices). Applies to **every** estimate: Lakemeter, sizing and DCP |
| `price_regions` | Regions to load into the Lakemeter price card (`us-east-1` default; comma list or `ALL`). Other regions return PENDING |
| `requests_path` | Optional folder of use-case request JSON files (format: `notebooks/new_workload_template.json`) |
| `space_id` | An existing id = update that agent in place (same URL and MCP endpoint). Blank = update the agent with the same `space_title`, or create one if none exists |
| `user_group` | Optional group to grant `USE`/`SELECT`/`EXECUTE`, `CAN RUN` on the agent and `CAN USE` on the warehouse |
| `smoke_test` | Asks the new agent a DCP question and prints the answer |

The notebook prints the agent URL and its MCP endpoint (`/api/2.0/mcp/genie/<space_id>`) for the Copilot connection.
Re-running is safe (every step is idempotent). After changing calculator code, product coverage, or agent instructions, regenerate all derived files:

```
python3 calculator/build_all.py
```

The product catalog is maintained in `genie/catalog/estimator_catalog.json` and exposed to Genie through the generated
`estimator_catalog()` function. It records implementation and validation separately: function availability does not mean
that a product path has been benchmarked. Catalog categories guide discovery; the user's request and supported capability
select the calculation path.

Run offline contract checks before rebuilding:

```
python3 -I -m unittest discover -s tests -v
python3 -I calculator/build_all.py
```

These checks do not deploy or update a live Genie Agent. Run the `setup_agent` job only after reviewing the generated agent definition.

## Deploy with a bundle (jobs for setup and maintenance)

`databricks.yml` is a Declarative Automation Bundle that deploys this repo and three jobs:

| Job (resource key) | Notebook | Schedule | Does |
|---|---|---|---|
| `setup_agent` | `setup_genie_dcp_agent.py`, then `notebooks/12_grant_access.py` | On demand | Builds or updates the whole agent, then grants the agent user groups access; run on install and after any change |
| `grant_access` | `notebooks/12_grant_access.py` | On demand | Grants (or re-grants) the agent user groups access; run after changing `user_groups` |
| `rate_refresh` | `notebooks/09_load_rate_card.py` | Daily 05:00 UTC | Reloads `dcp_rate_card` and `ref_dbu_price_card` from `system.billing` |
| `sizing_calibration` | `notebooks/11_calibrate_sizing.py` | Monthly, 1st 06:00 UTC | Calibrates sizing from job history (`DRY_RUN` by default; set `calibration_mode=APPLY` once the report looks right) |

Jobs run on serverless compute. Targets:

| Target | Use | Notes |
|---|---|---|
| `dev` (default) | Try it in your own folder | Job names are prefixed `[dev <user>]`; schedules are paused |
| `prod` | Production | Jobs run as `run_as_service_principal`; files go to the deployer's folder (deploy from CI as the service principal) |

**1. Set the parameters once in `dab-config.yml`.** Copy the example and fill it in. `dab-config.yml` is git-ignored, so
workspace hosts, catalogs, warehouse and service-principal ids stay out of the repo:

```bash
cp dab-config.example.yml dab-config.yml     # then edit: one block per target (dev, prod)
```

```yaml
targets:
  prod:
    workspace:
      host: https://<prod-workspace-host>     # or: profile: <CLI profile>
    variables:
      catalog: <catalog>
      warehouse_id: <SQL warehouse id>
      user_groups: "<agent users group>"     # one or more, comma-separated
      run_as_service_principal: <service principal application id>
      # ...every other variable is optional; the example file lists them with their defaults
```

**2. Validate, deploy and build the agent** — no `--var` needed:

```bash
databricks bundle validate --strict -t prod
databricks bundle deploy -t prod
databricks bundle run setup_agent -t prod
```

`databricks.yml` includes `dab-config.yml` automatically (it's optional, so the bundle also works with `--var` alone).
A `--var="name=value"` on the command line overrides the file for one run.

| Variable | Default | Meaning |
|---|---|---|
| `catalog`, `warehouse_id` | — (required) | Agent catalog; Pro or Serverless SQL warehouse |
| `schema` | `lakemeter` | Agent schema |
| `rate_source` | `ACCOUNT_PRICES` | `LIST_PRICES` for workspaces without account prices |
| `price_regions` / `sku_region` | `us-east-1` / `US_EAST_N_VIRGINIA` | Regions in the Lakemeter price card / DCP region suffix |
| `space_id`, `space_title` | blank / `Lakemeter Cost Estimator` | Which agent to update (blank id = the agent with this title, else create) |
| `user_groups` | blank | Agent user groups, comma-separated (see **Agent access** below) |
| `revoke_others` | `false` | `true` = groups not in `user_groups` lose their schema grants |
| `requests_path` | blank | Extra use-case request JSON folder |
| `load_pricing_tables` | `true` | `false` for faster setup re-runs |
| `calibration_mode`, `policy_id` | `DRY_RUN`, blank | Calibration mode; job cluster policy for the worker limit |
| `schedule_pause_status` | `UNPAUSED` | `PAUSED` to stop the schedules |
| `run_as_service_principal` | — (required for `prod`) | Application id the prod jobs run as |

**Agent access.** Every group in `user_groups` gets, from `notebooks/12_grant_access.py`:

| Object | Privilege | Why |
|---|---|---|
| Catalog | `USE CATALOG` | Reach the agent schema |
| Agent schema | `USE SCHEMA`, `SELECT`, `EXECUTE` | Read the agent's tables and run its functions; inherited by every table and function, including ones rebuilt later |
| Genie agent | `CAN RUN` | Ask the agent questions in the Genie UI and through Genie MCP / Copilot |
| SQL warehouse | `CAN USE` | Optional: the agent already lends its warehouse to users |

Use account-level groups (Unity Catalog can't grant to workspace-local groups; the all-users group is `account users`).
Users need no access to `system` tables. Who can view or run the bundle's *jobs* is separate: set `permissions` for the
target in `dab-config.yml` (see the example).

The service principal needs `SELECT` on `system.billing.account_prices` (and, for calibration, `system.lakeflow`,
`system.compute`, `system.billing.usage`, `system.access.table_lineage`), and create/modify on the agent schema.

## What it creates

In `<catalog>.<schema>`:

- **10 reference tables** (`ref_*`) loaded from the pricing CSVs bundled in this repo
  (`pricing_data/*.csv`, from Lakemeter OSS): DBU rates, instance DBU rates, VM costs (~111K rows),
  DBU multipliers, DBSQL rates + warehouse config, serverless rates, FM API rates,
  SKU↔region map.
- **15 scalar SQL functions** (helpers): `get_instance_dbu_rate`, `get_vm_cost_per_hour`,
  `get_dbu_price`, `get_photon_multiplier`, `get_product_type_for_pricing`,
  `calculate_hours_per_month`, `calculate_classic_compute_dbu`,
  `calculate_serverless_compute_dbu`, `calculate_dbsql_dbu`, `get_serverless_rate`,
  `calculate_vector_search_dbu`, `calculate_model_serving_dbu`, `calculate_lakebase_dbu`,
  `get_fmapi_databricks_dbu`, `get_fmapi_proprietary_dbu`.
- **14 Genie-facing table functions** returning a DBU + VM breakdown
  (`workload, dbu_per_hour, hours_per_month, dbu_per_month, dbu_price, dbu_cost_per_month,
  vm_cost_per_month, total_cost_per_month`):
  - Compute: `estimate_jobs_classic_cost`, `estimate_all_purpose_classic_cost`,
    `estimate_dlt_classic_cost`, `estimate_serverless_compute_cost`
  - SQL: `estimate_dbsql_cost`
  - AI/Serving: `estimate_model_serving_cost`, `estimate_vector_search_cost`,
    `estimate_fmapi_databricks_cost`, `estimate_fmapi_proprietary_cost`
  - Document AI: `estimate_ai_parse_cost`, `estimate_ai_classify_cost`, `estimate_ai_extract_cost`
  - Platform: `estimate_lakebase_cost`, `estimate_databricks_apps_cost`
- **DCP calculator** functions (`dcp_calc`, `dcp_calc_run`, `dcp_calc_totals`, `dcp_calc_line_items`,
  `dcp_calc_details`, `dcp_request`, `dcp_intake_questions`, plus the input check `dcp_check` / `dcp_check_inputs`), **workload sizing** (`size_cluster`, `estimate_sized_workload`) and
  `get_dbu_price_status`, with their tables (`dcp_requests`, `dcp_rate_card`, `ref_dbu_price_card`, `ref_sizing_*`).
- A **Genie agent** over the tables, with the functions registered as trusted assets, plus instructions, example SQL,
  sample questions and the 20 DCP workbook questions in its Benchmarks tab.

Cost model (ported from Lakemeter's Postgres engine):
`monthly_cost = dbu_per_hour × hours_per_month × dbu_price + vm_cost`. Serverless
compute and serverless SQL have no separate VM cost (DBU price includes compute).

### Governed prices (`sql/09_governed_prices.sql`)

Every Lakemeter DBU price goes through `get_dbu_price`, which reads **`ref_dbu_price_card`** — built from
`system.billing.account_prices` (`GOVERNED`) or, in test workspaces, `system.billing.list_prices` (`REFERENCE_ONLY`).
The DCP calculator's `dcp_rate_card` is loaded from the same table, so all skills quote the same rate.

- Billing SKU = `<TIER>_<PRODUCT>_<SKU REGION>` for regional SKUs (e.g. `ENTERPRISE_JOBS_SERVERLESS_COMPUTE_US_EAST_N_VIRGINIA`),
  else `<TIER>_<PRODUCT>` (e.g. `ENTERPRISE_JOBS_COMPUTE`). Exactly one current price is required.
- **No price → PENDING**: `get_dbu_price` returns NULL, so the DBU cost and total are NULL and the agent says PENDING with
  the reason (`get_dbu_price_status`). There is no list-price fallback (previously a missing price silently became $0).
- The Lakemeter CSV price is kept as `list_price_reference` for comparison only. DBU *consumption* (DBU/hour per
  instance, warehouse size, serverless size, tokens) still comes from the Lakemeter tables.
- **VM costs** for classic compute remain cloud on-demand list prices (`ref_vm_costs`) and are labelled as such.
- Refresh with `notebooks/09_load_rate_card.py` (both rate cards), e.g. daily.

### Workload sizing (when the user doesn't know the cluster)

`sql/08_workload_sizing.sql` turns a described workload (type, GB per day, runs per day, optional batch window) into a
cluster and prices it with the functions above. Example: *"10 GB of incremental ingestion daily"* → 1 driver + 1 ×
m5d.xlarge, 13 / 20 / 35 min per run → $4.73 / $7.28 / $12.74 per month on classic Jobs (Low / Expected / High, at list prices).

| Function / table | Does |
|---|---|
| `size_cluster(p_workload, p_data_gb, p_runs_per_day, p_window_minutes, p_cloud, p_node_type, p_max_workers, p_region)` | Node type, workers, autoscale range, runtime per run (low / expected / high), workers needed vs the worker limit, status (`FITS_WINDOW` / `EXCEEDS_WINDOW` / `EXCEEDS_POLICY`), options, throughput basis, assumptions, tips |
| `estimate_sized_workload(p_workload, p_data_gb, p_runs_per_day, p_days_per_month, p_window_minutes, ...)` | `size_cluster` → `estimate_jobs_classic_cost` / `estimate_serverless_compute_cost`: JOBS_CLASSIC and JOBS_SERVERLESS × LOW / EXPECTED / HIGH |
| `ref_sizing_profiles` | Per workload (`INGESTION`, `ETL`, `STREAMING`, `ML_TRAINING`, `ML_INFERENCE`): throughput range (GB per worker core-hour), memory working-set factor, default batch window, cluster start minutes, `calibration_status` |
| `ref_sizing_policy` | Worker limit and allowed node types per cloud (default guardrail 100 workers; load the job cluster policy with notebook 11) |
| `ref_sizing_node_families` | Node families allowed per cloud and category (AWS: m5d, r5d, c5d, i3, g5) |

- **Cores** = GB per run ÷ (expected GB per core-hour × usable window hours). **Memory** binds only where the data is held
  (training 4 GB per GB, ETL shuffle 0.5; ingestion streams through). The smallest fitting node within 32 workers is
  chosen, then the smallest within the worker limit.
- **Never clipped silently**: above the worker limit the result says `EXCEEDS_WINDOW` and lists the options (longer
  runtime on the capped cluster, N parallel jobs, serverless, or a higher limit). Serverless rows are sized without a limit.
- **Ranges**: the cluster is sized on EXPECTED throughput; LOW / HIGH throughput give the runtime and cost range.

### Calibrating sizing from job history (`notebooks/11_calibrate_sizing.py`)

Measures throughput and cluster start time per profile from the workspace's successful job runs: run window and
durations (`system.lakeflow`), worker core-hours and CPU (`system.compute.node_timeline`; single-node clusters use the
driver), GB written (Delta history via lineage, or the worker network-bytes proxy when history is not readable), DBUs
and cost (`system.billing.usage` × `account_prices` / `list_prices`). LOW / EXPECTED / HIGH = 25th / 50th / 75th
percentile; a hold-out backtest (older ⅔ → newest ⅓) reports the error against the current values. `DRY_RUN` reports;
`APPLY` writes `ref_sizing_profiles` (`CALIBRATED`) — the setup notebook keeps calibrated rows on re-run. Optional
`policy_id` loads the worker limit and allowed node types into `ref_sizing_policy`. Map jobs to profiles with a job tag
`sizing_profile`, or name rules (`profile_rules`). Run as a principal that can read the system tables and the written
tables; schedule monthly.

## Repository layout

| Path | What it is |
|---|---|
| `setup_genie_dcp_agent.py` | The one-notebook installer (generated; don't edit by hand) |
| `databricks.yml`, `resources/*.job.yml`, `dab-config.example.yml` | Deployment bundle: setup, grant, rate-refresh and calibration jobs |
| `notebooks/09_load_rate_card.py` | Rate-refresh job (generated) |
| `notebooks/11_calibrate_sizing.py` | Sizing calibration job |
| `notebooks/12_grant_access.py` | Agent access for user groups |
| `notebooks/dcp_estimator.py` | The DCP Smart Estimation Calculator — source of truth for all DCP math |
| `notebooks/new_workload_template.json` | Format of a use-case request file (`requests_path`) |
| `notebooks/reconcile_ai_rag.py`, `notebooks/ai_rag_model_api_request.json` | Calculator reconciliation for the AI_RAG_MODEL_API preset |
| `sql/02_functions.sql` | Lakemeter cost functions (29) |
| `sql/06_dcp_calculator.sql` | DCP calculator as UC functions (generated from `dcp_estimator.py`) |
| `sql/08_workload_sizing.sql` | Workload sizing tables and functions |
| `sql/09_governed_prices.sql` | Governed price card and `get_dbu_price` |
| `genie/space_definition.json`, `genie/instructions.md` | Agent configuration and concise routing policy |
| `genie/catalog/estimator_catalog.json` | Maintained product/category coverage, routing cues, pricing basis, and validation status |
| `sql/10_product_catalog.sql` | Generated `estimator_catalog()` function used during category discovery |
| `calculator/` | Builders (`build_all.py`, `build_sql.py`, `build_catalog.py`, `build_notebook.py`, `build_setup.py`), Python bundle manifest/loader, adapter and rate seed |
| `samples/requests/` | Bundled use cases (`SEND_US01`, `AI_RAG_MODEL_API`, `FIN_CLAIMS_02`) |
| `benchmark/` | 20-question benchmark harness and workbook filler |
| `pricing_data/` | Lakemeter OSS pricing CSVs (loaded by setup step 1) |

### DCP calculator (the only source of DCP numbers)

The DCP Smart Estimation Calculator (`notebooks/dcp_estimator.py`) does **all** DCP math. It runs, unchanged,
inside the Unity Catalog Python function `dcp_calc`, so Genie, the SQL API and the notebook return the same
numbers for the same request. Genie collects inputs, calls the calculator, and explains the result — it never
calculates, picks or changes a rate.

```
Genie / MCP / SQL ──► dcp_calc_totals | dcp_calc_line_items | dcp_calc_details   (what Genie calls)
                         └─► dcp_calc_run  (resolve use case or ad-hoc request, attach rate card)
                               └─► dcp_calc  (UC Python function: dcp_estimator.py verbatim + calculator/dcp_adapter.py)
                                     ├─ dcp_requests   named use cases: {"preset": "SEND_US01"} or a full EstimateRequest
                                     └─ dcp_rate_card  the only rates used (account_prices / list_prices + AWS registry)
```

| Function | Returns |
|---|---|
| `dcp_calc_totals(p_use_case \| p_request, p_overrides)` | Per scenario: all-in and core monthly, annual, one-time, first year, DBU/USD, AWS, pending count, status, confidence |
| `dcp_calc_line_items(..., p_scenario)` | Every priced or PENDING line: quantity, unit, rate, rate status/source, cost, treatment, evidence, formula |
| `dcp_calc_details(...)` | Summary, confidence, pending items, compute decisions, assumptions, warnings, scope, rates used, errors |
| `dcp_request(p_workload_type =>, p_runtime_hours_expected =>, p_concurrent_users =>, ..., p_not_applicable_areas =>)` | Builds an ad-hoc request from stated values, the user's identifiers, the requested scenarios (default LOW / EXPECTED / HIGH) and the state of each area |
| `dcp_intake_questions(p_area)` | Guided intake generated from the request schema: every input per area, `REQUIRED` / `DEFAULT` (with the default value) / `OPTIONAL`, and its question |
| `dcp_check_inputs(p_use_case \| p_request, p_overrides)` | The input check as rows (SQL / API use; not registered in the Genie agent) |

- **Named use cases** live in `dcp_requests`: a calculator preset (`SEND_US01`, `AI_RAG_MODEL_API`) or an
  `EstimateRequest` JSON (see `new_workload_template()` in the calculator; samples in `samples/requests/`).
- **What-ifs**: `p_overrides` = preset inputs (`{"retained_storage_tb": 8, "migration_gb": 8000}`) or dotted paths
  (`{"transfer.daily_gb_high": 12}`, `{"processing[C02].runs_per_month": 4}`).
- **Rates**: `dcp_rate_card` mirrors the calculator's rate registry (provider, SKU, unit, region). Notebook 09 replaces
  the Databricks DBU rows from `system.billing.account_prices` (`GOVERNED`) or, where account prices are not available,
  `system.billing.list_prices` (`REFERENCE_ONLY`). No price → `PENDING`; there is no fallback.
- **Input check before any math**: every `dcp_calc_*` call checks the request first (`calculator/dcp_adapter.py`
  `check()`; the calculator itself is unchanged). If anything is missing or wrong, nothing is priced: `request_status` is
  `NEEDS_INPUT` and `errors` holds one question per item — `MISSING` (an input with no calculator default, e.g. runs per
  month for a scheduled batch, concurrent SQL users, GB per day, retained TB, files per day, reads per file, model calls
  and tokens), `WRONG_TYPE` (e.g. `"30"` instead of `30`), `INVALID_VALUE` (negative, unknown workload type or model, or
  anything the calculator would reject, e.g. `transfer.daily_gb_high must be a non-negative number (got -1)`),
  `UNKNOWN_FIELD` (with a suggested spelling), `NOT_JSON`, `NO_WORKLOAD`. Inputs with a planning default (DBU/hour,
  event-driven runs, working / transfer days, streaming latency) are not asked for and are listed as assumptions; a
  missing low or high bound only leaves that scenario PENDING. Named use cases are validated but keep their presented scope.
- **Areas**: before an estimate is final every calculator area — processing, sql, transfer, storage, model_endpoints,
  one_time_model_jobs — must be `INCLUDED` (it has inputs), `EXCLUDED`, `UNKNOWN` or `NOT_APPLICABLE`. Unconfirmed areas
  return `AREA_UNCONFIRMED`. States go in the request as `"_areas": {"transfer": "NOT_APPLICABLE"}` (keys starting with
  `_` are ignored by the calculator), in `dcp_request(p_excluded_areas / p_unknown_areas / p_not_applicable_areas)`, or
  as overrides (`{"_areas.storage": "UNKNOWN"}`). Results report `area_states`; named use cases' missing areas are
  `NOT_APPLICABLE`.
- **Identifiers and scenarios**: a submitted `use_case_id`, `component_id` and `scenarios` list are kept as given.
- **Updating the calculator**: edit `notebooks/dcp_estimator.py`; if adding a helper module, add its unique flat import
  name and repository-relative path to `calculator/bundle_manifest.json`. List dependencies before modules that import them.
  Unlisted files are not shipped to the UC runtime. The builder validates source paths/syntax/delimiters and hashes every
  module name and source into the worker cache key. Run local contract tests and `python3 calculator/build_all.py`, review
  the generated SQL/notebook diff, then run the `setup_agent` job after deployment approval. Never edit generated SQL by hand.
- **Updating coverage or routing**: edit `genie/catalog/estimator_catalog.json` or `genie/instructions.md`, run
  `python3 calculator/build_all.py`, review the generated definition, then validate the targeted Genie flows.

### Benchmark

`benchmark/run_benchmark.py` retains the 20 numeric regression questions and adds routing/coverage acceptance cases
(Q21-Q27) for input-format parity, identifiers/scenarios, missing-input follow-up, DCP-first routing, catalog discovery,
unsupported coverage, and six-area guided intake. The numeric cases compare Genie with the deployed calculator and also
check the local engine against workbook v4.0.2 expectations. `benchmark/coverage_map.json` maps the V2 workbook's Green,
Yellow and Gray items to test targets; it does not imply unrun tests passed. Live cases require a selected Databricks
profile, warehouse and Genie Agent. The 51-case baseline and NEW_WORKLOAD_01 workbook fixtures are not in this repository,
so those exact historical cases remain unreproducible until their source inputs are added.

## Notes / gotchas

- **`ref_dbsql_warehouse_config` has swapped column names**: the `warehouse_size`
  column holds the *type* (classic/pro/serverless) and `warehouse_type` holds the
  *size* (2X-Large, …). `estimate_dbsql_cost` accounts for this; the upstream
  Lakemeter Postgres function does not, so classic/pro DBSQL VM cost is more accurate here.
- Photon is priced via a DBU **multiplier** (same per-DBU price as non-Photon), so there
  is no double counting.
- Token/usage-based workloads (FM API, ai_parse/classify/extract) report `dbu_per_hour=0`
  and `hours_per_month=0`; their monthly DBU is `dbu_per_month` and cost = DBU × price.
- Always-on workloads (Model Serving, Vector Search, Lakebase) bill `24 × days_per_month` hours.
- Not yet ported: AI Gateway, Agent Evaluation, AI Runtime (training), General Storage,
  Zerobus, Shutterstock ImageAI, Lakeflow Connect, and SKU-specific discount handling.
