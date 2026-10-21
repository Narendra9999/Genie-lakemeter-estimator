# Lakemeter → Genie Cost Estimator

Exposes Lakemeter OSS's Databricks cost-estimation logic natively in Unity Catalog +
a Genie Space, so users can ask workload-cost questions in natural language — no app,
no Marketplace, no Lakebase required. Built for the SEG/FEVM simulation.

## Install in one notebook (recommended)

`setup_genie_dcp_agent.py` builds the **complete Genie Cost Estimator Agent** in one run: pricing tables, list-price
functions, workload sizing, the DCP Smart Estimation Calculator v4.0.2 (as Unity Catalog functions), the DCP rate card, named use
cases, and the Genie agent itself (instructions, column synonyms, trusted functions, example and sample questions).
It is self-contained — the only other thing to upload is the `pricing_data/` folder.

1. Import `setup_genie_dcp_agent.py` and the `pricing_data/` folder into the same workspace folder
   (or a Git folder cloned from this repo).
2. Attach to a UC-enabled cluster (DBR 13.3 LTS+) or serverless, set the widgets, **Run all**:

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
Re-running is safe (every step is idempotent). The notebook is generated — after changing the calculator, the SQL
or the agent definition, rebuild it:

```
python3 calculator/build_sql.py && python3 calculator/build_notebook.py && python3 calculator/build_setup.py
```

## Deploy with a bundle (jobs for setup and maintenance)

`databricks.yml` is a Declarative Automation Bundle that deploys this repo and three jobs:

| Job (resource key) | Notebook | Schedule | Does |
|---|---|---|---|
| `setup_agent` | `setup_genie_dcp_agent.py` | On demand | Builds or updates the whole agent; run on install and after any change |
| `rate_refresh` | `notebooks/09_load_rate_card.py` | Daily 05:00 UTC | Reloads `dcp_rate_card` and `ref_dbu_price_card` from `system.billing` |
| `sizing_calibration` | `notebooks/11_calibrate_sizing.py` | Monthly, 1st 06:00 UTC | Calibrates sizing from job history (`DRY_RUN` by default; set `calibration_mode=APPLY` once the report looks right) |

Jobs run on serverless compute. Targets:

| Target | Use | Notes |
|---|---|---|
| `dev` (default) | Try it in your own folder | Job names are prefixed `[dev <user>]`; schedules are paused |
| `prod` | Production | Jobs run as `run_as_service_principal`; files go to the deployer's folder (deploy from CI as the service principal) |

```bash
# validate, deploy, then build the agent
databricks bundle validate --strict -t prod --var="catalog=<catalog>" --var="warehouse_id=<id>" \
  --var="run_as_service_principal=<application id>" --var="user_group=<agent users group>"
databricks bundle deploy -t prod  --var=... (same values)
databricks bundle run setup_agent -t prod --var=... (same values)
```

To avoid repeating `--var`, put the values in `.databricks/bundle/<target>/variable-overrides.json`
(e.g. `{"catalog": "dcp_finops", "warehouse_id": "...", "user_group": "..."}`).

| Variable | Default | Meaning |
|---|---|---|
| `catalog`, `warehouse_id` | — (required) | Agent catalog; Pro or Serverless SQL warehouse |
| `schema` | `lakemeter` | Agent schema |
| `rate_source` | `ACCOUNT_PRICES` | `LIST_PRICES` for workspaces without account prices |
| `price_regions` / `sku_region` | `us-east-1` / `US_EAST_N_VIRGINIA` | Regions in the Lakemeter price card / DCP region suffix |
| `space_id`, `space_title` | blank / `Lakemeter Cost Estimator` | Which agent to update (blank id = the agent with this title, else create) |
| `user_group` | blank | Group granted access to the agent and schema |
| `requests_path` | blank | Extra use-case request JSON folder |
| `load_pricing_tables` | `true` | `false` for faster setup re-runs |
| `calibration_mode`, `policy_id` | `DRY_RUN`, blank | Calibration mode; job cluster policy for the worker limit |
| `schedule_pause_status` | `UNPAUSED` | `PAUSED` to stop the schedules |
| `run_as_service_principal` | — (required for `prod`) | Application id the prod jobs run as |

The service principal needs `SELECT` on `system.billing.account_prices` (and, for calibration, `system.lakeflow`,
`system.compute`, `system.billing.usage`, `system.access.table_lineage`), and create/modify on the agent schema.

## What it creates

In `fevm_catalog_naren.lakemeter` (change the catalog/schema to retarget):

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
- A **Genie Space** ("Lakemeter Cost Estimator") over the tables, with the functions
  registered as trusted assets, plus instructions, example SQL, and sample questions.

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
m5d.xlarge, 13 / 20 / 35 min per run → $4.73 / $7.28 / $12.74 per month on classic Jobs (Low / Expected / High; FEVM list prices).

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

## Lakemeter list-price agent only (legacy all-in-one)

`setup_genie_lakemeter.py` is a Databricks notebook that does **everything** in one run —
creates the schema, loads the pricing tables, creates all 29 functions, and builds the
Genie Space (with synonyms) — all parameterized via widgets:

| Widget | Meaning |
|---|---|
| `catalog` / `schema` | where to create tables + functions (e.g. `main` / `lakemeter`) |
| `warehouse_id` | Pro/Serverless SQL warehouse id for the Genie Space |
| `pricing_path` | **workspace folder** holding the pricing CSVs (blank = `./pricing_data` next to the notebook) |
| `space_title` | Genie Space title |
| `create_genie_space` / `load_tables` | toggles |

**Steps:** import this repo folder into the customer workspace (so `setup_genie_lakemeter.py`
and `pricing_data/` sit together), open the notebook, set the widgets, and **Run All**. It
reads the CSVs from the workspace folder via the `file:/Workspace/...` path (no UC volume
needed), then creates the Space and prints its URL. Uses the notebook's own auth for the
Genie API — no tokens to configure.

## Modular notebooks (run on a dedicated cluster)

If you prefer step-by-step notebooks over the all-in-one, `notebooks/` has one per stage —
import the folder and attach to any dedicated (or serverless) cluster, set the widgets, Run All:

| Notebook | Lang | Does | Key widgets |
|---|---|---|---|
| `02_create_functions.sql` | SQL | `USE CATALOG/SCHEMA` then create all 29 list-price functions (unqualified) | `catalog`, `schema` |
| `03_create_genie_space.py` | Python | Build the Genie Space (tables + functions + synonyms + instructions) | `catalog`, `schema`, `warehouse_id`, `space_title` |
| `04_add_synonyms.py` | Python | Add/refresh column synonyms on an *existing* Space (GET → inject → PATCH) | `catalog`, `schema`, `space_id` |
| `10_deploy_dcp_calculator.py` | Python | **DCP calculator:** deploy `dcp_estimator.py` as Unity Catalog functions, load named use cases, register the DCP skill on the Space | `catalog`, `schema`, `requests_path`, `space_id` |
| `09_load_rate_card.py` | Python | **Rates:** refresh `ref_dbu_price_card` (Lakemeter + sizing) and `dcp_rate_card` (DCP) from `system.billing.account_prices` (governed) or `list_prices` (test) | `catalog`, `schema`, `rate_source`, `price_regions` |
| `07_refresh_list_prices.sql` | SQL | Refresh the `dcp_list_price_rates` list-price snapshot (test workspaces) | `catalog`, `schema` |
| `11_calibrate_sizing.py` | Python | **Sizing calibration:** measure throughput and start time per workload profile from job history; optional cluster-policy limits | `catalog`, `schema`, `lookback_days`, `mode`, `policy_id` |
| `dcp_estimator.py` | Python module | The DCP Smart Estimation Calculator (v4.0.2). Source of truth for all DCP math | — |
| `05_register_dcp_skill.py`, `06_load_dcp_intake.py`, `08_register_sizing_skill.py` | Python | *Superseded* SQL ports of the DCP math (v3.8) and sizing guidance. Notebook 10 retires them from the agent; kept for reference | — |

Pricing tables: load them with `setup_genie_dcp_agent.py` (step 1) or `sql/01_load_tables.sql`. Then run 02 → 03 → 10 → 09
(09 again whenever rates change; schedule it). The modular notebooks do not install workload sizing or the governed
Lakemeter price card — use `setup_genie_dcp_agent.py` for the complete agent.

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
| `dcp_request(p_workload_type =>, p_runtime_hours_expected =>, p_concurrent_users =>, ...)` | Builds an ad-hoc request from stated values (blanks stay PENDING) |

- **Named use cases** live in `dcp_requests`: a calculator preset (`SEND_US01`, `AI_RAG_MODEL_API`) or an
  `EstimateRequest` JSON (see `new_workload_template()` in the calculator; samples in `samples/requests/`).
- **What-ifs**: `p_overrides` = preset inputs (`{"retained_storage_tb": 8, "migration_gb": 8000}`) or dotted paths
  (`{"transfer.daily_gb_high": 12}`, `{"processing[C02].runs_per_month": 4}`).
- **Rates**: `dcp_rate_card` mirrors the calculator's rate registry (provider, SKU, unit, region). Notebook 09 replaces
  the Databricks DBU rows from `system.billing.account_prices` (`GOVERNED`) or, where account prices are not available,
  `system.billing.list_prices` (`REFERENCE_ONLY`). No price → `PENDING`; there is no fallback.
- **Validation**: bad input is rejected before any math with the calculator's field-level message
  (e.g. `transfer.daily_gb_high must be a non-negative number (got -1)`).
- **Upgrading the calculator**: replace `notebooks/dcp_estimator.py`, run `python3 calculator/build_sql.py` and
  `python3 calculator/build_notebook.py`, re-run notebook 10, then the benchmark.

### Benchmark

`benchmark/run_benchmark.py` asks the agent the 20 benchmark questions (DCP workbook tab C) as written, compares
each answer with the deployed calculator's output (agent pass = calculator called, figures reproduced to the cent,
PENDING shown as PENDING), and separately checks the calculator engine at governed rates against the workbook's
expected answers. `benchmark/fill_workbook.py` writes the results into a copy of the workbook.

## Reproduce manually (CLI / SQL editor)

Prereqs: Databricks CLI profile, a Pro/Serverless SQL warehouse, Databricks Assistant
enabled. The pricing CSVs are bundled in this repo under `pricing_data/`. (The manual
path below uses a UC volume; the notebook above uses a workspace folder instead.)

```bash
CATALOG=<catalog>; SCHEMA=lakemeter; PROFILE=<profile>; WID=<sql_warehouse_id>

# 1. Schema + volume, then upload the pricing CSVs
databricks ... CREATE SCHEMA $CATALOG.$SCHEMA ; CREATE VOLUME $CATALOG.$SCHEMA.raw
databricks fs cp pricing_data/ dbfs:/Volumes/$CATALOG/$SCHEMA/raw/ --recursive

# 2. Load tables + create functions (sql/01_load_tables.sql, sql/02_functions.sql)
#    Run each statement via the SQL Statement Execution API / DBSQL editor.
#    (Statements are separated by a line: -- @@ )

# 3. Create the Genie Space
python3 genie/create_space.py      # edit CAT/SCH/WID at the top first
python3 genie/add_synonyms.py    # optional: add column synonyms + entity matching to the Space
```

> Retargeting: the SQL files and `create_space.py` hard-code
> `fevm_catalog_naren.lakemeter` and the FEVM warehouse id — search/replace the
> catalog, schema, warehouse id, and volume path for another workspace.

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
