# Lakemeter → Genie Cost Estimator

Exposes Lakemeter OSS's Databricks cost-estimation logic natively in Unity Catalog +
a Genie Space, so users can ask workload-cost questions in natural language — no app,
no Marketplace, no Lakebase required. Built for the SEG/FEVM simulation.

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

## Quickest path: run the setup notebook (recommended)

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
| `01_load_tables.py` | Python | Create schema + load the 10 pricing tables from the workspace folder (`file:/Workspace/...`) | `catalog`, `schema`, `pricing_path` |
| `02_create_functions.sql` | SQL | `USE CATALOG/SCHEMA` then create all 29 functions (unqualified) | `catalog`, `schema` |
| `03_create_genie_space.py` | Python | Build the Genie Space (tables + functions + synonyms + instructions) | `catalog`, `schema`, `warehouse_id`, `space_title` |
| `04_add_synonyms.py` | Python | Add/refresh column synonyms on an *existing* Space (GET → inject → PATCH) | `catalog`, `schema`, `space_id` |
| `05_register_dcp_skill.py` | Python | Create the DCP estimation functions + register them as a Genie "skill" on an existing Space | `catalog`, `schema`, `space_id` |

Run order: 01 → 02 → 03 → (05). (03 already includes synonyms; 04 is only for enriching a Space made elsewhere. 05 adds the DCP skill — run it after 03 with the Space id.)

### DCP Smart Estimation skill

`sql/03_dcp_functions.sql` + notebook `05` add the **DCP** (scenario-based consumption projection)
engine as Genie trusted-asset functions, ported from the *DCP Smart Estimation Calculator*:

- `dcp_estimate(workload_type, quantity, runtime_low, runtime_expected, runtime_high, events_per_month, concurrent_users, planning_days)`
  — returns LOW/EXPECTED/HIGH/STRESS monthly DBU + USD at **governed account rates**
  (Jobs Serverless `$0.3465`/DBU, SQL Pro `$0.4235`/DBU). `workload_type` ∈
  `CONTINUOUS_STREAMING` | `EVENT_DRIVEN_BATCH` | `SQL_REPORTING`.
- `dcp_estimate_storage(daily_low, daily_expected, daily_high, planning_days)` — S3 retained-GB +
  object costs and DataSync transfer sensitivity (S3 reported as a sensitivity, excluded from the total).

This is the **planning-floor** subset of the engine (governed-rate scenario math); the notebook's
historical-calibration pass (which reads `system.billing.usage`) is not part of the Genie skill.
Ask Genie: *"Give a DCP estimation for a continuous streaming workload."*
`setup_genie_lakemeter.py` at the repo root does all of 01–03 in a single notebook.

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
