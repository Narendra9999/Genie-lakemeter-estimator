# Databricks notebook source
# MAGIC %md
# MAGIC # Lakemeter → Genie Cost Estimator — one-shot setup
# MAGIC
# MAGIC Creates the Unity Catalog pricing tables, the cost-estimation SQL functions, and the
# MAGIC **Genie Space** (with column synonyms) in one run. Parameterized via the widgets below.
# MAGIC
# MAGIC **Prerequisites**
# MAGIC - A Pro/Serverless SQL warehouse (for the Genie Space) and Databricks Assistant enabled.
# MAGIC - The 11 Lakemeter pricing CSVs uploaded to a **workspace folder** (not a volume).
# MAGIC   Default = a `pricing_data/` folder next to this notebook.
# MAGIC - Permission to create a schema + functions in the target catalog.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Parameters

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Target catalog")
dbutils.widgets.text("schema", "lakemeter", "Target schema")
dbutils.widgets.text("warehouse_id", "", "SQL warehouse id (for Genie Space)")
dbutils.widgets.text("pricing_path", "", "Workspace folder with pricing CSVs (blank = ./pricing_data next to this notebook)")
dbutils.widgets.text("space_title", "Lakemeter Cost Estimator", "Genie Space title")
dbutils.widgets.dropdown("create_genie_space", "true", ["true", "false"], "Create the Genie Space?")
dbutils.widgets.dropdown("load_tables", "true", ["true", "false"], "Load/refresh the pricing tables?")

catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema").strip()
warehouse_id = dbutils.widgets.get("warehouse_id").strip()
pricing_path = dbutils.widgets.get("pricing_path").strip()
space_title = dbutils.widgets.get("space_title").strip()
create_genie_space = dbutils.widgets.get("create_genie_space") == "true"
load_tables = dbutils.widgets.get("load_tables") == "true"
FQ = f"{catalog}.{schema}"

# Default pricing_path = sibling ./pricing_data of this notebook (as a Workspace Files local path)
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
nb_path = ctx.notebookPath().get()  # e.g. /Users/me@co/setup_genie_lakemeter
if not pricing_path:
    pricing_path = "/Workspace" + nb_path.rsplit("/", 1)[0] + "/pricing_data"

print("catalog.schema :", FQ)
print("pricing_path   :", pricing_path)
print("warehouse_id   :", warehouse_id or "(none - Genie Space will be skipped)")
print("create_space   :", create_genie_space)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Schema

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {FQ} COMMENT 'Lakemeter cost-estimation pricing data and SQL functions for Genie'")
print("schema ready:", FQ)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Load pricing tables from the workspace folder
# MAGIC Reads each CSV with Spark via the `file:` scheme (Workspace Files on the driver).

# COMMAND ----------

TABLES = {
    "ref_dbu_rates": "dbu-rates.csv",
    "ref_instance_dbu_rates": "instance-dbu-rates.csv",
    "ref_dbu_multipliers": "dbu-multipliers.csv",
    "ref_dbsql_rates": "dbsql-rates.csv",
    "ref_dbsql_warehouse_config": "dbsql-warehouse-config.csv",
    "ref_serverless_rates": "serverless-rates.csv",
    "ref_sku_region_map": "sku-region-map.csv",
    "ref_fmapi_databricks_rates": "fmapi-databricks-rates.csv",
    "ref_fmapi_proprietary_rates": "fmapi-proprietary-rates.csv",
    "ref_vm_costs": "vm-costs_part*.csv",  # glob: part1 + part2
}

if load_tables:
    for tbl, pattern in TABLES.items():
        src = f"file:{pricing_path}/{pattern}"
        df = (spark.read.option("header", "true").option("inferSchema", "true")
              .option("mode", "PERMISSIVE").csv(src))
        df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{FQ}.{tbl}")
        print(f"  {tbl:32s} {df.count():>8d} rows")
    print("tables loaded.")
else:
    print("load_tables=false -> skipping table load")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Create the cost-estimation SQL functions
# MAGIC 15 scalar helpers + 14 Genie-facing `estimate_*` table functions.

# COMMAND ----------

FUNCTIONS_SQL = r"""
CREATE OR REPLACE FUNCTION __FQ__.get_instance_dbu_rate(p_cloud STRING, p_instance_type STRING)
RETURNS DOUBLE
COMMENT 'DBU/hour rate for a VM instance type (classic compute sizing).'
RETURN COALESCE((SELECT dbu_rate FROM __FQ__.ref_instance_dbu_rates
  WHERE upper(cloud)=upper(p_cloud) AND upper(instance_type)=upper(p_instance_type) LIMIT 1), 0)
-- @@
CREATE OR REPLACE FUNCTION __FQ__.get_vm_cost_per_hour(p_cloud STRING, p_region STRING, p_instance_type STRING, p_pricing_tier STRING, p_payment_option STRING)
RETURNS DOUBLE
COMMENT 'Cloud VM $/hour for an instance in a region (on_demand/reserved/spot).'
RETURN COALESCE((SELECT cost_per_hour FROM __FQ__.ref_vm_costs
  WHERE upper(cloud)=upper(p_cloud) AND upper(region)=upper(p_region)
    AND upper(instance_type)=upper(p_instance_type)
    AND upper(pricing_tier)=upper(p_pricing_tier)
    AND upper(payment_option)=upper(COALESCE(p_payment_option,'NA')) LIMIT 1), 0)
-- @@
CREATE OR REPLACE FUNCTION __FQ__.get_dbu_price(p_cloud STRING, p_region STRING, p_tier STRING, p_product_type STRING)
RETURNS DOUBLE
COMMENT 'Price per DBU (USD) for a product/SKU in a cloud+region+tier.'
RETURN COALESCE((SELECT price_per_dbu FROM __FQ__.ref_dbu_rates
  WHERE upper(cloud)=upper(p_cloud) AND upper(region)=upper(p_region) AND upper(tier)=upper(p_tier)
    AND (upper(sku_name)=upper(p_product_type) OR upper(COALESCE(product_type,''))=upper(p_product_type)) LIMIT 1), 0)
-- @@
CREATE OR REPLACE FUNCTION __FQ__.get_photon_multiplier(p_cloud STRING, p_workload_type STRING, p_dlt_edition STRING, p_photon_enabled BOOLEAN, p_serverless_enabled BOOLEAN)
RETURNS DOUBLE
COMMENT 'DBU multiplier applied for Photon/serverless (1.0 if classic no-photon).'
RETURN CASE
  WHEN NOT COALESCE(p_photon_enabled,false) AND NOT COALESCE(p_serverless_enabled,false) THEN 1.0
  ELSE COALESCE((SELECT multiplier FROM __FQ__.ref_dbu_multipliers
    WHERE upper(cloud)=upper(p_cloud) AND feature='photon'
      AND upper(sku_type)=upper(CASE upper(p_workload_type)
        WHEN 'DLT' THEN 'DLT_'||upper(COALESCE(p_dlt_edition,'CORE'))||'_COMPUTE'
        WHEN 'JOBS' THEN 'JOBS_COMPUTE'
        WHEN 'ALL_PURPOSE' THEN 'ALL_PURPOSE_COMPUTE'
        ELSE 'JOBS_COMPUTE' END) LIMIT 1), 1.0)
END
-- @@
CREATE OR REPLACE FUNCTION __FQ__.get_product_type_for_pricing(p_workload_type STRING, p_serverless_enabled BOOLEAN, p_photon_enabled BOOLEAN, p_dlt_edition STRING, p_dbsql_warehouse_type STRING, p_fmapi_provider STRING)
RETURNS STRING
COMMENT 'Maps a workload config to the DBU pricing SKU name in ref_dbu_rates.'
RETURN CASE upper(p_workload_type)
  WHEN 'JOBS' THEN CASE WHEN COALESCE(p_serverless_enabled,false) THEN 'JOBS_SERVERLESS_COMPUTE'
                        WHEN COALESCE(p_photon_enabled,false) THEN 'JOBS_COMPUTE_(PHOTON)' ELSE 'JOBS_COMPUTE' END
  WHEN 'ALL_PURPOSE' THEN CASE WHEN COALESCE(p_serverless_enabled,false) THEN 'ALL_PURPOSE_SERVERLESS_COMPUTE'
                        WHEN COALESCE(p_photon_enabled,false) THEN 'ALL_PURPOSE_COMPUTE_(PHOTON)' ELSE 'ALL_PURPOSE_COMPUTE' END
  WHEN 'DLT' THEN CASE WHEN COALESCE(p_serverless_enabled,false) THEN 'JOBS_SERVERLESS_COMPUTE'
                        ELSE 'DLT_'||upper(COALESCE(p_dlt_edition,'CORE'))||'_COMPUTE'||CASE WHEN COALESCE(p_photon_enabled,false) THEN '_(PHOTON)' ELSE '' END END
  WHEN 'DBSQL' THEN CASE upper(COALESCE(p_dbsql_warehouse_type,'')) WHEN 'SERVERLESS' THEN 'SERVERLESS_SQL_COMPUTE' WHEN 'PRO' THEN 'SQL_PRO_COMPUTE' ELSE 'SQL_COMPUTE' END
  ELSE upper(p_workload_type)||'_COMPUTE' END
-- @@
CREATE OR REPLACE FUNCTION __FQ__.calculate_hours_per_month(p_workload_type STRING, p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT, p_hours_per_month DOUBLE)
RETURNS DOUBLE
COMMENT 'Monthly runtime hours: explicit hours if given, else runs/day x runtime x days; 24x7 for always-on workloads.'
RETURN CASE
  WHEN p_hours_per_month IS NOT NULL THEN p_hours_per_month
  WHEN upper(p_workload_type) IN ('VECTOR_SEARCH','MODEL_SERVING','LAKEBASE') THEN 24.0*COALESCE(p_days_per_month,30)
  ELSE COALESCE(p_runs_per_day,0)*(COALESCE(p_avg_runtime_minutes,0)/60.0)*COALESCE(p_days_per_month,30)
END
-- @@
CREATE OR REPLACE FUNCTION __FQ__.calculate_classic_compute_dbu(p_cloud STRING, p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT, p_photon_enabled BOOLEAN, p_workload_type STRING, p_dlt_edition STRING)
RETURNS DOUBLE
COMMENT 'DBU/hour for classic JOBS/ALL_PURPOSE/DLT: (driver + worker x N) x photon_multiplier.'
RETURN (__FQ__.get_instance_dbu_rate(p_cloud,p_driver_node_type)
       + __FQ__.get_instance_dbu_rate(p_cloud,p_worker_node_type)*COALESCE(p_num_workers,0))
       * __FQ__.get_photon_multiplier(p_cloud,p_workload_type,p_dlt_edition,p_photon_enabled,false)
-- @@
CREATE OR REPLACE FUNCTION __FQ__.calculate_serverless_compute_dbu(p_cloud STRING, p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT, p_workload_type STRING, p_serverless_mode STRING)
RETURNS DOUBLE
COMMENT 'DBU/hour for serverless JOBS/ALL_PURPOSE: base x photon x mode multiplier (ALL_PURPOSE and performance mode = 2x).'
RETURN (__FQ__.get_instance_dbu_rate(p_cloud,p_driver_node_type)
       + __FQ__.get_instance_dbu_rate(p_cloud,p_worker_node_type)*COALESCE(p_num_workers,0))
       * __FQ__.get_photon_multiplier(p_cloud,p_workload_type,NULL,true,true)
       * CASE WHEN upper(COALESCE(p_workload_type,''))='ALL_PURPOSE' THEN 2.0
              WHEN lower(COALESCE(p_serverless_mode,'standard'))='performance' THEN 2.0 ELSE 1.0 END
-- @@
CREATE OR REPLACE FUNCTION __FQ__.calculate_dbsql_dbu(p_cloud STRING, p_dbsql_warehouse_type STRING, p_dbsql_warehouse_size STRING, p_dbsql_num_clusters INT)
RETURNS DOUBLE
COMMENT 'DBU/hour for a DBSQL warehouse size x number of clusters.'
RETURN COALESCE((SELECT dbu_per_hour FROM __FQ__.ref_dbsql_rates
  WHERE upper(cloud)=upper(p_cloud) AND warehouse_type=lower(p_dbsql_warehouse_type) AND upper(warehouse_size)=upper(p_dbsql_warehouse_size) LIMIT 1), 0)
  * COALESCE(p_dbsql_num_clusters,1)
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_jobs_classic_cost(
  p_cloud STRING, p_region STRING, p_tier STRING,
  p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT,
  p_photon_enabled BOOLEAN, p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost estimate for a classic Lakeflow Jobs (JOBS_COMPUTE) workload. Returns DBU + VM breakdown.'
RETURN
  WITH b AS (
    SELECT __FQ__.calculate_classic_compute_dbu(p_cloud,p_driver_node_type,p_worker_node_type,p_num_workers,p_photon_enabled,'JOBS',NULL) dbu_ph,
           __FQ__.calculate_hours_per_month('JOBS',p_runs_per_day,p_avg_runtime_minutes,p_days_per_month,NULL) hrs,
           __FQ__.get_dbu_price(p_cloud,p_region,p_tier,__FQ__.get_product_type_for_pricing('JOBS',false,p_photon_enabled,NULL,NULL,NULL)) price,
           __FQ__.get_vm_cost_per_hour(p_cloud,p_region,p_driver_node_type,'on_demand','NA') drv,
           __FQ__.get_vm_cost_per_hour(p_cloud,p_region,p_worker_node_type,'on_demand','NA') wrk)
  SELECT 'JOBS_CLASSIC', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2),
         round((drv + wrk*COALESCE(p_num_workers,0))*hrs,2),
         round(dbu_ph*hrs*price + (drv + wrk*COALESCE(p_num_workers,0))*hrs,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_all_purpose_classic_cost(
  p_cloud STRING, p_region STRING, p_tier STRING,
  p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT,
  p_photon_enabled BOOLEAN, p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost estimate for a classic All-Purpose (interactive) compute workload.'
RETURN
  WITH b AS (
    SELECT __FQ__.calculate_classic_compute_dbu(p_cloud,p_driver_node_type,p_worker_node_type,p_num_workers,p_photon_enabled,'ALL_PURPOSE',NULL) dbu_ph,
           __FQ__.calculate_hours_per_month('ALL_PURPOSE',p_runs_per_day,p_avg_runtime_minutes,p_days_per_month,NULL) hrs,
           __FQ__.get_dbu_price(p_cloud,p_region,p_tier,__FQ__.get_product_type_for_pricing('ALL_PURPOSE',false,p_photon_enabled,NULL,NULL,NULL)) price,
           __FQ__.get_vm_cost_per_hour(p_cloud,p_region,p_driver_node_type,'on_demand','NA') drv,
           __FQ__.get_vm_cost_per_hour(p_cloud,p_region,p_worker_node_type,'on_demand','NA') wrk)
  SELECT 'ALL_PURPOSE_CLASSIC', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2),
         round((drv + wrk*COALESCE(p_num_workers,0))*hrs,2),
         round(dbu_ph*hrs*price + (drv + wrk*COALESCE(p_num_workers,0))*hrs,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_dlt_classic_cost(
  p_cloud STRING, p_region STRING, p_tier STRING, p_dlt_edition STRING,
  p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT,
  p_photon_enabled BOOLEAN, p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost estimate for a classic Lakeflow Declarative Pipeline (DLT). p_dlt_edition in CORE/PRO/ADVANCED.'
RETURN
  WITH b AS (
    SELECT __FQ__.calculate_classic_compute_dbu(p_cloud,p_driver_node_type,p_worker_node_type,p_num_workers,p_photon_enabled,'DLT',p_dlt_edition) dbu_ph,
           __FQ__.calculate_hours_per_month('DLT',p_runs_per_day,p_avg_runtime_minutes,p_days_per_month,NULL) hrs,
           __FQ__.get_dbu_price(p_cloud,p_region,p_tier,__FQ__.get_product_type_for_pricing('DLT',false,p_photon_enabled,p_dlt_edition,NULL,NULL)) price,
           __FQ__.get_vm_cost_per_hour(p_cloud,p_region,p_driver_node_type,'on_demand','NA') drv,
           __FQ__.get_vm_cost_per_hour(p_cloud,p_region,p_worker_node_type,'on_demand','NA') wrk)
  SELECT 'DLT_'||upper(COALESCE(p_dlt_edition,'CORE')), dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2),
         round((drv + wrk*COALESCE(p_num_workers,0))*hrs,2),
         round(dbu_ph*hrs*price + (drv + wrk*COALESCE(p_num_workers,0))*hrs,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_serverless_compute_cost(
  p_cloud STRING, p_region STRING, p_tier STRING, p_workload_type STRING,
  p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT, p_serverless_mode STRING,
  p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost estimate for serverless JOBS or ALL_PURPOSE compute (no separate VM cost; DBU price includes compute).'
RETURN
  WITH b AS (
    SELECT __FQ__.calculate_serverless_compute_dbu(p_cloud,p_driver_node_type,p_worker_node_type,p_num_workers,p_workload_type,p_serverless_mode) dbu_ph,
           __FQ__.calculate_hours_per_month(p_workload_type,p_runs_per_day,p_avg_runtime_minutes,p_days_per_month,NULL) hrs,
           __FQ__.get_dbu_price(p_cloud,p_region,p_tier,__FQ__.get_product_type_for_pricing(p_workload_type,true,true,NULL,NULL,NULL)) price)
  SELECT upper(p_workload_type)||'_SERVERLESS', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2),
         0.0, round(dbu_ph*hrs*price,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_dbsql_cost(
  p_cloud STRING, p_region STRING, p_tier STRING,
  p_warehouse_type STRING, p_warehouse_size STRING, p_num_clusters INT,
  p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT, p_hours_per_month DOUBLE)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost estimate for a Databricks SQL warehouse. p_warehouse_type in classic/pro/serverless; serverless has no separate VM cost.'
RETURN
  WITH whc AS (
    SELECT driver_instance_type di, worker_instance_type wi, worker_count wc
    FROM __FQ__.ref_dbsql_warehouse_config
    WHERE upper(cloud)=upper(p_cloud) AND lower(warehouse_size)=lower(p_warehouse_type) AND upper(warehouse_type)=upper(p_warehouse_size) LIMIT 1),
  calc AS (
    SELECT __FQ__.calculate_dbsql_dbu(p_cloud,p_warehouse_type,p_warehouse_size,p_num_clusters) dbu_ph,
           __FQ__.calculate_hours_per_month('DBSQL',p_runs_per_day,p_avg_runtime_minutes,p_days_per_month,p_hours_per_month) hrs,
           __FQ__.get_dbu_price(p_cloud,p_region,p_tier,__FQ__.get_product_type_for_pricing('DBSQL',false,false,NULL,p_warehouse_type,NULL)) price,
           CASE WHEN lower(p_warehouse_type)='serverless' THEN 0.0 ELSE
             __FQ__.get_vm_cost_per_hour(p_cloud,p_region,whc.di,'on_demand','NA')*COALESCE(p_num_clusters,1)
             + __FQ__.get_vm_cost_per_hour(p_cloud,p_region,whc.wi,'on_demand','NA')*COALESCE(whc.wc,0)*COALESCE(p_num_clusters,1)
           END vm_ph
    FROM (SELECT 1 one) d LEFT JOIN whc ON true)
  SELECT 'DBSQL_'||upper(p_warehouse_type), dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2),
         round(vm_ph*hrs,2), round(dbu_ph*hrs*price + vm_ph*hrs,2) FROM calc

-- @@
CREATE OR REPLACE FUNCTION __FQ__.get_serverless_rate(p_cloud STRING, p_product STRING, p_size_or_model STRING)
RETURNS DOUBLE
COMMENT 'DBU rate from ref_serverless_rates for a product (vector_search/model_serving) and size/model.'
RETURN COALESCE((SELECT dbu_rate FROM __FQ__.ref_serverless_rates
  WHERE upper(cloud)=upper(p_cloud) AND lower(product)=lower(p_product) AND upper(size_or_model)=upper(p_size_or_model) LIMIT 1), 0)
-- @@
CREATE OR REPLACE FUNCTION __FQ__.calculate_vector_search_dbu(p_cloud STRING, p_mode STRING, p_capacity_millions DOUBLE)
RETURNS DOUBLE
COMMENT 'Vector Search DBU/hour = rate x CEIL(capacity_millions / divisor); divisor standard=2M, storage_optimized=64M.'
RETURN __FQ__.get_serverless_rate(p_cloud,'vector_search',p_mode)
  * ceil(COALESCE(p_capacity_millions,0) / CASE WHEN lower(p_mode)='storage_optimized' THEN 64.0 ELSE 2.0 END)
-- @@
CREATE OR REPLACE FUNCTION __FQ__.calculate_model_serving_dbu(p_cloud STRING, p_serverless_size STRING, p_concurrency INT)
RETURNS DOUBLE
COMMENT 'Model Serving DBU/hour = rate x (concurrency for cpu* sizes, else concurrency/4 for GPU).'
RETURN __FQ__.get_serverless_rate(p_cloud,'model_serving',p_serverless_size)
  * CASE WHEN lower(COALESCE(p_serverless_size,'cpu')) LIKE 'cpu%' THEN COALESCE(p_concurrency,4) ELSE COALESCE(p_concurrency,4)/4.0 END
-- @@
CREATE OR REPLACE FUNCTION __FQ__.calculate_lakebase_dbu(p_lakebase_cu INT, p_lakebase_ha_nodes INT)
RETURNS DOUBLE
COMMENT 'Lakebase DBU/hour = capacity_units x HA_nodes.'
RETURN COALESCE(p_lakebase_cu,0) * COALESCE(p_lakebase_ha_nodes,1)
-- @@
CREATE OR REPLACE FUNCTION __FQ__.get_fmapi_databricks_dbu(p_cloud STRING, p_model STRING, p_rate_type STRING, p_quantity BIGINT)
RETURNS DOUBLE
COMMENT 'FM API (Databricks-hosted) DBU for a quantity: hourly rate x hours, else (tokens / input_divisor) x rate.'
RETURN COALESCE((SELECT CASE WHEN COALESCE(is_hourly,false) THEN p_quantity*dbu_rate
  ELSE (p_quantity / cast(COALESCE(input_divisor,1) as double)) * dbu_rate END
  FROM __FQ__.ref_fmapi_databricks_rates
  WHERE upper(cloud)=upper(p_cloud) AND upper(model)=upper(p_model) AND lower(rate_type)=lower(p_rate_type) LIMIT 1), 0)
-- @@
CREATE OR REPLACE FUNCTION __FQ__.get_fmapi_proprietary_dbu(p_cloud STRING, p_provider STRING, p_model STRING, p_endpoint_type STRING, p_context_length STRING, p_rate_type STRING, p_quantity BIGINT)
RETURNS DOUBLE
COMMENT 'FM API (proprietary models) DBU for a quantity: hourly rate x hours, else (tokens / input_divisor) x rate.'
RETURN COALESCE((SELECT CASE WHEN COALESCE(is_hourly,false) THEN p_quantity*dbu_rate
  ELSE (p_quantity / cast(COALESCE(input_divisor,1) as double)) * dbu_rate END
  FROM __FQ__.ref_fmapi_proprietary_rates
  WHERE upper(cloud)=upper(p_cloud) AND upper(provider)=upper(p_provider) AND upper(model)=upper(p_model)
    AND lower(endpoint_type)=lower(COALESCE(p_endpoint_type,'global')) AND lower(context_length)=lower(COALESCE(p_context_length,'all'))
    AND lower(rate_type)=lower(p_rate_type) LIMIT 1), 0)
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_vector_search_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_mode STRING, p_capacity_millions DOUBLE, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for a Vector Search endpoint (always-on). p_mode in standard/storage_optimized; p_capacity_millions = indexed vectors in millions.'
RETURN WITH b AS (
  SELECT __FQ__.calculate_vector_search_dbu(p_cloud,p_mode,p_capacity_millions) dbu_ph,
         24.0*COALESCE(p_days_per_month,30) hrs,
         __FQ__.get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'VECTOR_SEARCH', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2), 0.0, round(dbu_ph*hrs*price,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_model_serving_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_serverless_size STRING, p_concurrency INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for a Model Serving endpoint (always-on). p_serverless_size e.g. cpu, gpu_small_t4, gpu_medium_a10g_1x; p_concurrency = provisioned concurrency.'
RETURN WITH b AS (
  SELECT __FQ__.calculate_model_serving_dbu(p_cloud,p_serverless_size,p_concurrency) dbu_ph,
         24.0*COALESCE(p_days_per_month,30) hrs,
         __FQ__.get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'MODEL_SERVING', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2), 0.0, round(dbu_ph*hrs*price,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_lakebase_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_capacity_units INT, p_ha_nodes INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for a Lakebase (managed Postgres) instance (always-on). p_capacity_units = CU, p_ha_nodes = HA node count.'
RETURN WITH b AS (
  SELECT __FQ__.calculate_lakebase_dbu(p_capacity_units,p_ha_nodes) dbu_ph,
         24.0*COALESCE(p_days_per_month,30) hrs,
         __FQ__.get_dbu_price(p_cloud,p_region,p_tier,'DATABASE_SERVERLESS_COMPUTE') price)
  SELECT 'LAKEBASE', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2), 0.0, round(dbu_ph*hrs*price,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_databricks_apps_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_app_size STRING, p_num_apps INT, p_hours_per_month DOUBLE)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for Databricks Apps. p_app_size in medium (0.5 DBU/app-hr) or large (1.0); p_hours_per_month default 730.'
RETURN WITH b AS (
  SELECT (CASE WHEN lower(p_app_size)='large' THEN 1.0 ELSE 0.5 END)*COALESCE(p_num_apps,1) dbu_ph,
         COALESCE(p_hours_per_month,730.0) hrs,
         __FQ__.get_dbu_price(p_cloud,p_region,p_tier,'ALL_PURPOSE_SERVERLESS_COMPUTE') price)
  SELECT 'DATABRICKS_APPS', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2), 0.0, round(dbu_ph*hrs*price,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_fmapi_databricks_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_model STRING, p_rate_type STRING, p_quantity BIGINT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for Databricks-hosted Foundation Model API. p_rate_type e.g. input_token/output_token (p_quantity=tokens) or a provisioned/hourly rate (p_quantity=hours).'
RETURN WITH b AS (
  SELECT __FQ__.get_fmapi_databricks_dbu(p_cloud,p_model,p_rate_type,p_quantity) dbu_m,
         __FQ__.get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'FMAPI_DATABRICKS', 0.0, 0.0, dbu_m, price, round(dbu_m*price,2), 0.0, round(dbu_m*price,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_fmapi_proprietary_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_provider STRING, p_model STRING, p_endpoint_type STRING, p_context_length STRING, p_rate_type STRING, p_quantity BIGINT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for proprietary-model Foundation Model API. p_provider e.g. anthropic/openai/google; p_quantity=tokens (token rates) or hours (batch_inference).'
RETURN WITH b AS (
  SELECT __FQ__.get_fmapi_proprietary_dbu(p_cloud,p_provider,p_model,p_endpoint_type,p_context_length,p_rate_type,p_quantity) dbu_m,
         __FQ__.get_dbu_price(p_cloud,p_region,p_tier,
           CASE WHEN upper(p_provider)='GOOGLE' THEN 'GEMINI_MODEL_SERVING' ELSE upper(p_provider)||'_MODEL_SERVING' END) price)
  SELECT 'FMAPI_PROPRIETARY', 0.0, 0.0, dbu_m, price, round(dbu_m*price,2), 0.0, round(dbu_m*price,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_ai_parse_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_mode STRING, p_pages_thousands DOUBLE, p_complexity STRING, p_hours_per_month DOUBLE)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for AI Parse (Document AI). p_mode pages: dbu = pages_thousands x complexity rate (low_text 12.5, low_images 22.5, medium 62.5, high 87.5). p_mode dbu: dbu = hours_per_month.'
RETURN WITH b AS (
  SELECT CASE WHEN lower(COALESCE(p_mode,'pages'))='dbu' THEN COALESCE(p_hours_per_month,0)
         ELSE COALESCE(p_pages_thousands,0) * CASE lower(COALESCE(p_complexity,'medium'))
           WHEN 'low_text' THEN 12.5 WHEN 'low_images' THEN 22.5 WHEN 'high' THEN 87.5 ELSE 62.5 END END dbu_m,
         __FQ__.get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'AI_PARSE', 0.0, 0.0, dbu_m, price, round(dbu_m*price,2), 0.0, round(dbu_m*price,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_ai_classify_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_document_type STRING, p_num_docs BIGINT, p_custom_rate_per_1000 DOUBLE)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for ai_classify. dbu = (num_docs/1000) x rate; rates short_text 4.5, rental_contract 50; else p_custom_rate_per_1000.'
RETURN WITH b AS (
  SELECT (COALESCE(p_num_docs,0)/1000.0) * CASE lower(COALESCE(p_document_type,'custom'))
           WHEN 'short_text' THEN 4.5 WHEN 'rental_contract' THEN 50.0 ELSE COALESCE(p_custom_rate_per_1000,0) END dbu_m,
         __FQ__.get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'AI_CLASSIFY', 0.0, 0.0, dbu_m, price, round(dbu_m*price,2), 0.0, round(dbu_m*price,2) FROM b
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_ai_extract_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_document_type STRING, p_num_inputs BIGINT, p_custom_rate_per_1000 DOUBLE)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for ai_extract. dbu = (num_inputs/1000) x rate; rates short_text 45, invoice 45, complex_reasoning 562.5, deep_nesting 537.5; else p_custom_rate_per_1000.'
RETURN WITH b AS (
  SELECT (COALESCE(p_num_inputs,0)/1000.0) * CASE lower(COALESCE(p_document_type,'custom'))
           WHEN 'short_text' THEN 45.0 WHEN 'invoice' THEN 45.0 WHEN 'complex_reasoning' THEN 562.5 WHEN 'deep_nesting' THEN 537.5 ELSE COALESCE(p_custom_rate_per_1000,0) END dbu_m,
         __FQ__.get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'AI_EXTRACT', 0.0, 0.0, dbu_m, price, round(dbu_m*price,2), 0.0, round(dbu_m*price,2) FROM b
"""

stmts = [s.strip() for s in FUNCTIONS_SQL.split("\n-- @@\n") if s.strip()]
ok = 0
for stmt in stmts:
    spark.sql(stmt.replace("__FQ__", FQ))
    ok += 1
print(f"created {ok} functions in {FQ}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Create the Genie Space (tables + functions + synonyms + instructions)

# COMMAND ----------

import json, uuid, requests

def nid():
    return uuid.uuid4().hex

def col(name, desc, syn=None, entity=False):
    c = {"column_name": name, "description": [desc]}
    if syn:
        c["synonyms"] = syn
    if entity:
        c["enable_entity_matching"] = True
    return c

# --- tables + descriptions ---
tbl_desc = [
    (f"{FQ}.ref_dbsql_rates", "DBU/hour rates per DBSQL warehouse type (classic/pro/serverless) and size; includes_compute flags whether VM is bundled."),
    (f"{FQ}.ref_dbsql_warehouse_config", "DBSQL warehouse instance configs. IMPORTANT: 'warehouse_size' column holds the TYPE (classic/pro/serverless) and 'warehouse_type' holds the SIZE (e.g. 2X-Large)."),
    (f"{FQ}.ref_dbu_multipliers", "DBU multipliers (e.g. photon) by cloud and sku_type."),
    (f"{FQ}.ref_dbu_rates", "Price per DBU (USD) by cloud, region, tier and SKU (sku_name)."),
    (f"{FQ}.ref_fmapi_databricks_rates", "Foundation Model API (Databricks-hosted) token/hourly DBU rates per model; input_divisor, is_hourly."),
    (f"{FQ}.ref_fmapi_proprietary_rates", "Foundation Model API (proprietary provider models) DBU rates per provider/model/endpoint_type/context_length."),
    (f"{FQ}.ref_instance_dbu_rates", "DBU/hour per VM instance type (classic/serverless compute sizing)."),
    (f"{FQ}.ref_serverless_rates", "Serverless product DBU rates: product in model_serving (sizes cpu/gpu_*), vector_search (standard/storage_optimized)."),
    (f"{FQ}.ref_sku_region_map", "Mapping between SKU region names and cloud region codes (e.g. US_EAST_N_VIRGINIA -> us-east-1)."),
    (f"{FQ}.ref_vm_costs", "Cloud VM $/hour by cloud, region, instance_type, pricing_tier, payment_option."),
]
tbl_desc.sort(key=lambda t: t[0])

COLS = {
    f"{FQ}.ref_dbu_rates": [
        col("cloud", "Cloud provider (AWS/AZURE/GCP).", ["provider", "csp"], True),
        col("price_per_dbu", "Price per DBU in USD.", ["dbu price", "dbu rate", "rate", "price"]),
        col("region", "Cloud region code (e.g. us-east-1).", ["region"], True),
        col("sku_name", "Billing SKU / product type, e.g. JOBS_COMPUTE, ALL_PURPOSE_COMPUTE, SQL_COMPUTE.", ["sku", "product", "product type"], True),
        col("tier", "Pricing tier: STANDARD, PREMIUM, ENTERPRISE.", ["pricing tier", "plan", "edition"], True),
    ],
    f"{FQ}.ref_vm_costs": [
        col("cloud", "Cloud provider.", ["provider", "csp"], True),
        col("cost_per_hour", "Cloud VM cost per hour in USD.", ["vm cost", "vm price", "hourly cost", "compute cost", "instance cost"]),
        col("instance_type", "VM / node instance type (e.g. m5.xlarge).", ["vm", "node", "node type", "machine type", "instance"], True),
        col("payment_option", "Payment option (e.g. NA, 1yr, 3yr).", ["commitment"]),
        col("pricing_tier", "VM pricing tier: on_demand, reserved, spot.", ["on demand", "reserved", "spot", "purchase option"], True),
        col("region", "Cloud region code (e.g. us-east-1).", ["region"], True),
    ],
    f"{FQ}.ref_instance_dbu_rates": [
        col("dbu_rate", "DBU per hour for this instance type.", ["dbu per hour", "dbu rate"]),
        col("instance_type", "VM / node instance type.", ["vm", "node", "node type", "machine type"], True),
        col("memory_gb", "Memory in GB.", ["ram", "memory"]),
        col("vcpus", "Virtual CPUs.", ["cores", "cpus", "vcpu"]),
    ],
    f"{FQ}.ref_dbsql_rates": [
        col("dbu_per_hour", "DBU per hour for the SQL warehouse size.", ["dbu rate"]),
        col("includes_compute", "Whether VM/compute is bundled in the DBU price.", ["compute bundled", "vm included"]),
        col("warehouse_size", "SQL warehouse size (e.g. Small, Medium, Large, 2X-Large).", ["sql warehouse size", "t-shirt size"], True),
        col("warehouse_type", "SQL warehouse type: classic, pro, serverless.", ["sql warehouse type"], True),
    ],
    f"{FQ}.ref_dbsql_warehouse_config": [
        col("driver_instance_type", "Driver VM instance type for the warehouse.", ["driver vm"]),
        col("warehouse_size", "NOTE: holds the warehouse TYPE (classic/pro/serverless) despite the name.", ["warehouse type"], True),
        col("warehouse_type", "NOTE: holds the warehouse SIZE (e.g. 2X-Large) despite the name.", ["warehouse size"], True),
        col("worker_count", "Number of worker nodes.", ["workers", "num workers"]),
        col("worker_instance_type", "Worker VM instance type for the warehouse.", ["worker vm"]),
    ],
    f"{FQ}.ref_serverless_rates": [
        col("dbu_rate", "DBU rate for the serverless product/size.", ["dbu rate"]),
        col("product", "Serverless product: model_serving, vector_search.", ["serverless product"], True),
        col("size_or_model", "GPU/CPU size for model serving, or vector search mode (standard/storage_optimized).", ["gpu type", "mode", "vector search mode"], True),
    ],
    f"{FQ}.ref_sku_region_map": [
        col("region_code", "Cloud region code (e.g. us-east-1).", ["region", "cloud region"], True),
        col("sku_region", "SKU region name (e.g. US_EAST_N_VIRGINIA).", ["sku region"], True),
    ],
    f"{FQ}.ref_fmapi_databricks_rates": [
        col("dbu_rate", "DBU rate per unit (per input_divisor tokens, or per hour).", ["rate", "dbu rate"]),
        col("input_divisor", "Token divisor the rate applies per (usually 1,000,000).", ["token divisor"]),
        col("is_hourly", "Whether the rate is hourly (provisioned) vs token-based.", ["hourly"]),
        col("model", "Databricks-hosted foundation model name (e.g. bge-large).", ["foundation model", "llm", "model name"], True),
        col("rate_type", "Rate type: input_token, output_token, provisioned, etc.", ["token type"], True),
    ],
    f"{FQ}.ref_fmapi_proprietary_rates": [
        col("context_length", "Context length bucket (e.g. all).", ["context window"]),
        col("dbu_rate", "DBU rate per unit (per input_divisor tokens, or per hour).", ["rate", "dbu rate"]),
        col("endpoint_type", "Endpoint type (e.g. global).", ["endpoint"]),
        col("model", "Proprietary model name (e.g. claude-haiku-4-5).", ["foundation model", "llm", "model name"], True),
        col("provider", "Model provider: anthropic, openai, google.", ["model provider", "vendor"], True),
        col("rate_type", "Rate type: input_token, output_token, batch_inference, etc.", ["token type"], True),
    ],
    f"{FQ}.ref_dbu_multipliers": [
        col("feature", "Feature the multiplier applies to (e.g. photon).", ["feature"], True),
        col("multiplier", "DBU multiplier value.", ["dbu multiplier", "factor"]),
        col("sku_type", "SKU type the multiplier applies to.", ["sku", "product type"], True),
    ],
}

data_sources = {"tables": [
    {"identifier": i, "description": [d], "column_configs": sorted(COLS.get(i, []), key=lambda c: c["column_name"])}
    for i, d in tbl_desc
]}

est = ["estimate_jobs_classic_cost", "estimate_all_purpose_classic_cost", "estimate_dlt_classic_cost",
       "estimate_serverless_compute_cost", "estimate_dbsql_cost", "estimate_vector_search_cost",
       "estimate_model_serving_cost", "estimate_lakebase_cost", "estimate_databricks_apps_cost",
       "estimate_fmapi_databricks_cost", "estimate_fmapi_proprietary_cost", "estimate_ai_parse_cost",
       "estimate_ai_classify_cost", "estimate_ai_extract_cost", "get_dbu_price", "get_vm_cost_per_hour", "get_serverless_rate"]
sql_functions = sorted([{"id": nid(), "identifier": f"{FQ}.{n}"} for n in est], key=lambda x: x["id"])

text = [
    f"This space estimates Databricks workload monthly costs, ported from the Lakemeter OSS cost engine. ALWAYS prefer the estimate_* SQL table functions below for cost math instead of deriving formulas from the raw ref_ tables. Call them as table functions, e.g. SELECT * FROM {FQ}.estimate_jobs_classic_cost(...).",
    "Every estimate_* function returns one row: workload, dbu_per_hour, hours_per_month, dbu_per_month, dbu_price, dbu_cost_per_month, vm_cost_per_month, total_cost_per_month. total = dbu_cost + vm_cost. Serverless and token/usage-based workloads have vm_cost_per_month = 0.",
    "COMPUTE: estimate_jobs_classic_cost(cloud,region,tier,driver_node_type,worker_node_type,num_workers,photon_enabled,runs_per_day,avg_runtime_minutes,days_per_month); estimate_all_purpose_classic_cost(same); estimate_dlt_classic_cost(cloud,region,tier,dlt_edition[CORE/PRO/ADVANCED],driver,worker,num_workers,photon_enabled,runs_per_day,avg_runtime_minutes,days_per_month); estimate_serverless_compute_cost(cloud,region,tier,workload_type[JOBS/ALL_PURPOSE],driver,worker,num_workers,serverless_mode[standard/performance],runs_per_day,avg_runtime_minutes,days_per_month).",
    "SQL: estimate_dbsql_cost(cloud,region,tier,warehouse_type[classic/pro/serverless],warehouse_size,num_clusters,runs_per_day,avg_runtime_minutes,days_per_month,hours_per_month). Pass hours_per_month directly if known, else NULL.",
    "AI/SERVING: estimate_model_serving_cost(cloud,region,tier,serverless_size[cpu,gpu_small_t4,...],concurrency,days_per_month); estimate_vector_search_cost(cloud,region,tier,mode[standard/storage_optimized],capacity_millions,days_per_month); estimate_fmapi_databricks_cost(cloud,region,tier,model,rate_type,quantity); estimate_fmapi_proprietary_cost(cloud,region,tier,provider,model,endpoint_type,context_length,rate_type,quantity).",
    "DOCUMENT AI (dbu per 1000 units): estimate_ai_parse_cost(cloud,region,tier,mode[pages/dbu],pages_thousands,complexity[low_text/low_images/medium/high],hours_per_month); estimate_ai_classify_cost(cloud,region,tier,document_type[short_text/rental_contract/custom],num_docs,custom_rate_per_1000); estimate_ai_extract_cost(cloud,region,tier,document_type[short_text/invoice/complex_reasoning/deep_nesting/custom],num_inputs,custom_rate_per_1000).",
    "PLATFORM: estimate_lakebase_cost(cloud,region,tier,capacity_units,ha_nodes,days_per_month) [DBU/hr = CU x HA]; estimate_databricks_apps_cost(cloud,region,tier,app_size[medium/large],num_apps,hours_per_month[default 730]).",
    "Always-on workloads (Model Serving, Vector Search, Lakebase) bill 24 x days_per_month hours. FM API and Document AI are usage-based, so dbu_per_hour/hours are 0 in the output.",
    "Defaults when unspecified: cloud='AWS', region='us-east-1', tier='ENTERPRISE', days_per_month=30, num_clusters=1, concurrency=4, photon_enabled=true for Jobs/DLT. tier in STANDARD/PREMIUM/ENTERPRISE; region is a cloud region code. Round money to 2 decimals.",
]
text_instructions = sorted([{"id": nid(), "content": text}], key=lambda x: x["id"])

examples = [
    (["Estimate the monthly cost of a classic Jobs cluster on AWS us-east-1 with an m5.xlarge driver and 10 m5.xlarge workers, Photon on, running 1 hour per day."],
     [f"SELECT * FROM {FQ}.estimate_jobs_classic_cost('AWS','us-east-1','ENTERPRISE','m5.xlarge','m5.xlarge',10,true,1,60,30)"]),
    (["How much does a serverless SQL Medium warehouse cost if it runs about 160 hours a month?"],
     [f"SELECT * FROM {FQ}.estimate_dbsql_cost('AWS','us-east-1','ENTERPRISE','serverless','Medium',1,NULL,NULL,NULL,160)"]),
    (["What does a Vector Search standard endpoint with 10 million vectors cost per month?"],
     [f"SELECT * FROM {FQ}.estimate_vector_search_cost('AWS','us-east-1','ENTERPRISE','standard',10,30)"]),
    (["Estimate a GPU model serving endpoint (gpu_small_t4) with concurrency 4."],
     [f"SELECT * FROM {FQ}.estimate_model_serving_cost('AWS','us-east-1','ENTERPRISE','gpu_small_t4',4,30)"]),
    (["Monthly cost of a Lakebase instance with 4 capacity units and 2 HA nodes?"],
     [f"SELECT * FROM {FQ}.estimate_lakebase_cost('AWS','us-east-1','ENTERPRISE',4,2,30)"]),
    (["Estimate FM API cost for 1 billion input tokens to the Databricks bge-large model."],
     [f"SELECT * FROM {FQ}.estimate_fmapi_databricks_cost('AWS','us-east-1','ENTERPRISE','bge-large','input_token',1000000000)"]),
]
example_question_sqls = sorted([{"id": nid(), "question": q, "sql": s} for q, s in examples], key=lambda x: x["id"])

sample_q = [
    "Estimate the monthly cost of a classic Jobs cluster: m5.xlarge driver + 10 m5.xlarge workers, Photon, 1 hour/day.",
    "How much does a serverless SQL Medium warehouse cost for 160 hours a month?",
    "What does a Vector Search endpoint with 10 million vectors cost per month?",
    "Monthly cost of a Lakebase instance with 4 capacity units and 2 HA nodes?",
    "Cost to extract fields from 500,000 invoices with ai_extract?",
    "Compare serverless Jobs vs classic Jobs monthly cost for the same cluster.",
]
sample_questions = sorted([{"id": nid(), "question": [q]} for q in sample_q], key=lambda x: x["id"])

space = {"version": 2, "config": {"sample_questions": sample_questions}, "data_sources": data_sources,
         "instructions": {"text_instructions": text_instructions, "example_question_sqls": example_question_sqls, "sql_functions": sql_functions}}

if create_genie_space:
    if not warehouse_id:
        raise ValueError("warehouse_id widget is required to create the Genie Space")
    host = "https://" + spark.conf.get("spark.databricks.workspaceUrl")
    token = ctx.apiToken().get()
    body = {"warehouse_id": warehouse_id, "title": space_title,
            "description": "Estimate Databricks workload costs across compute, SQL, AI/serving, FM API, Document AI, Lakebase and Apps using natural language.",
            "serialized_space": json.dumps(space)}
    r = requests.post(f"{host}/api/2.0/genie/spaces", headers={"Authorization": f"Bearer {token}"}, json=body)
    r.raise_for_status()
    sid = r.json()["space_id"]
    print("Genie Space created:", sid)
    print("Open:", f"{host}/genie/rooms/{sid}")
else:
    print("create_genie_space=false -> tables + functions created; skipping Genie Space.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Done
# MAGIC Tables + 29 functions are in the target schema; the Genie Space (if created) is linked above.
# MAGIC Try asking it: *"Estimate a classic Jobs cluster: m5.xlarge driver + 10 m5.xlarge workers, Photon, 1 hour/day."*