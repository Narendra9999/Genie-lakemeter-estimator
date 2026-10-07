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

## Reproduce (e.g. in the customer workspace)

Prereqs: Databricks CLI profile, a Pro/Serverless SQL warehouse, Databricks Assistant
enabled. The pricing CSVs are bundled in this repo under `pricing_data/`.

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
