-- Databricks notebook source
-- MAGIC %md
-- MAGIC # 02 · Create cost-estimation SQL functions
-- MAGIC 15 scalar helpers + 14 Genie-facing estimate_* table functions. Set the catalog/schema widgets, then Run All.

-- COMMAND ----------

CREATE WIDGET TEXT catalog DEFAULT 'main';
CREATE WIDGET TEXT schema DEFAULT 'lakemeter'

-- COMMAND ----------

USE CATALOG ${catalog};
USE SCHEMA ${schema}

-- COMMAND ----------

CREATE OR REPLACE FUNCTION get_instance_dbu_rate(p_cloud STRING, p_instance_type STRING)
RETURNS DOUBLE
COMMENT 'DBU/hour rate for a VM instance type (classic compute sizing).'
RETURN COALESCE((SELECT dbu_rate FROM ref_instance_dbu_rates
  WHERE upper(cloud)=upper(p_cloud) AND upper(instance_type)=upper(p_instance_type) LIMIT 1), 0)

-- COMMAND ----------

CREATE OR REPLACE FUNCTION get_vm_cost_per_hour(p_cloud STRING, p_region STRING, p_instance_type STRING, p_pricing_tier STRING, p_payment_option STRING)
RETURNS DOUBLE
COMMENT 'Cloud VM $/hour for an instance in a region (on_demand/reserved/spot).'
RETURN COALESCE((SELECT cost_per_hour FROM ref_vm_costs
  WHERE upper(cloud)=upper(p_cloud) AND upper(region)=upper(p_region)
    AND upper(instance_type)=upper(p_instance_type)
    AND upper(pricing_tier)=upper(p_pricing_tier)
    AND upper(payment_option)=upper(COALESCE(p_payment_option,'NA')) LIMIT 1), 0)

-- COMMAND ----------

CREATE OR REPLACE FUNCTION get_dbu_price(p_cloud STRING, p_region STRING, p_tier STRING, p_product_type STRING)
RETURNS DOUBLE
COMMENT 'Price per DBU (USD) for a product/SKU in a cloud+region+tier.'
RETURN COALESCE((SELECT price_per_dbu FROM ref_dbu_rates
  WHERE upper(cloud)=upper(p_cloud) AND upper(region)=upper(p_region) AND upper(tier)=upper(p_tier)
    AND (upper(sku_name)=upper(p_product_type) OR upper(COALESCE(product_type,''))=upper(p_product_type)) LIMIT 1), 0)

-- COMMAND ----------

CREATE OR REPLACE FUNCTION get_photon_multiplier(p_cloud STRING, p_workload_type STRING, p_dlt_edition STRING, p_photon_enabled BOOLEAN, p_serverless_enabled BOOLEAN)
RETURNS DOUBLE
COMMENT 'DBU multiplier applied for Photon/serverless (1.0 if classic no-photon).'
RETURN CASE
  WHEN NOT COALESCE(p_photon_enabled,false) AND NOT COALESCE(p_serverless_enabled,false) THEN 1.0
  ELSE COALESCE((SELECT multiplier FROM ref_dbu_multipliers
    WHERE upper(cloud)=upper(p_cloud) AND feature='photon'
      AND upper(sku_type)=upper(CASE upper(p_workload_type)
        WHEN 'DLT' THEN 'DLT_'||upper(COALESCE(p_dlt_edition,'CORE'))||'_COMPUTE'
        WHEN 'JOBS' THEN 'JOBS_COMPUTE'
        WHEN 'ALL_PURPOSE' THEN 'ALL_PURPOSE_COMPUTE'
        ELSE 'JOBS_COMPUTE' END) LIMIT 1), 1.0)
END

-- COMMAND ----------

CREATE OR REPLACE FUNCTION get_product_type_for_pricing(p_workload_type STRING, p_serverless_enabled BOOLEAN, p_photon_enabled BOOLEAN, p_dlt_edition STRING, p_dbsql_warehouse_type STRING, p_fmapi_provider STRING)
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

-- COMMAND ----------

CREATE OR REPLACE FUNCTION calculate_hours_per_month(p_workload_type STRING, p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT, p_hours_per_month DOUBLE)
RETURNS DOUBLE
COMMENT 'Monthly runtime hours: explicit hours if given, else runs/day x runtime x days; 24x7 for always-on workloads.'
RETURN CASE
  WHEN p_hours_per_month IS NOT NULL THEN p_hours_per_month
  WHEN upper(p_workload_type) IN ('VECTOR_SEARCH','MODEL_SERVING','LAKEBASE') THEN 24.0*COALESCE(p_days_per_month,30)
  ELSE COALESCE(p_runs_per_day,0)*(COALESCE(p_avg_runtime_minutes,0)/60.0)*COALESCE(p_days_per_month,30)
END

-- COMMAND ----------

CREATE OR REPLACE FUNCTION calculate_classic_compute_dbu(p_cloud STRING, p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT, p_photon_enabled BOOLEAN, p_workload_type STRING, p_dlt_edition STRING)
RETURNS DOUBLE
COMMENT 'DBU/hour for classic JOBS/ALL_PURPOSE/DLT: (driver + worker x N) x photon_multiplier.'
RETURN (get_instance_dbu_rate(p_cloud,p_driver_node_type)
       + get_instance_dbu_rate(p_cloud,p_worker_node_type)*COALESCE(p_num_workers,0))
       * get_photon_multiplier(p_cloud,p_workload_type,p_dlt_edition,p_photon_enabled,false)

-- COMMAND ----------

CREATE OR REPLACE FUNCTION calculate_serverless_compute_dbu(p_cloud STRING, p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT, p_workload_type STRING, p_serverless_mode STRING)
RETURNS DOUBLE
COMMENT 'DBU/hour for serverless JOBS/ALL_PURPOSE: base x photon x mode multiplier (ALL_PURPOSE and performance mode = 2x).'
RETURN (get_instance_dbu_rate(p_cloud,p_driver_node_type)
       + get_instance_dbu_rate(p_cloud,p_worker_node_type)*COALESCE(p_num_workers,0))
       * get_photon_multiplier(p_cloud,p_workload_type,NULL,true,true)
       * CASE WHEN upper(COALESCE(p_workload_type,''))='ALL_PURPOSE' THEN 2.0
              WHEN lower(COALESCE(p_serverless_mode,'standard'))='performance' THEN 2.0 ELSE 1.0 END

-- COMMAND ----------

CREATE OR REPLACE FUNCTION calculate_dbsql_dbu(p_cloud STRING, p_dbsql_warehouse_type STRING, p_dbsql_warehouse_size STRING, p_dbsql_num_clusters INT)
RETURNS DOUBLE
COMMENT 'DBU/hour for a DBSQL warehouse size x number of clusters.'
RETURN COALESCE((SELECT dbu_per_hour FROM ref_dbsql_rates
  WHERE upper(cloud)=upper(p_cloud) AND warehouse_type=lower(p_dbsql_warehouse_type) AND upper(warehouse_size)=upper(p_dbsql_warehouse_size) LIMIT 1), 0)
  * COALESCE(p_dbsql_num_clusters,1)

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_jobs_classic_cost(
  p_cloud STRING, p_region STRING, p_tier STRING,
  p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT,
  p_photon_enabled BOOLEAN, p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost estimate for a classic Lakeflow Jobs (JOBS_COMPUTE) workload. Returns DBU + VM breakdown.'
RETURN
  WITH b AS (
    SELECT calculate_classic_compute_dbu(p_cloud,p_driver_node_type,p_worker_node_type,p_num_workers,p_photon_enabled,'JOBS',NULL) dbu_ph,
           calculate_hours_per_month('JOBS',p_runs_per_day,p_avg_runtime_minutes,p_days_per_month,NULL) hrs,
           get_dbu_price(p_cloud,p_region,p_tier,get_product_type_for_pricing('JOBS',false,p_photon_enabled,NULL,NULL,NULL)) price,
           get_vm_cost_per_hour(p_cloud,p_region,p_driver_node_type,'on_demand','NA') drv,
           get_vm_cost_per_hour(p_cloud,p_region,p_worker_node_type,'on_demand','NA') wrk)
  SELECT 'JOBS_CLASSIC', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2),
         round((drv + wrk*COALESCE(p_num_workers,0))*hrs,2),
         round(dbu_ph*hrs*price + (drv + wrk*COALESCE(p_num_workers,0))*hrs,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_all_purpose_classic_cost(
  p_cloud STRING, p_region STRING, p_tier STRING,
  p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT,
  p_photon_enabled BOOLEAN, p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost estimate for a classic All-Purpose (interactive) compute workload.'
RETURN
  WITH b AS (
    SELECT calculate_classic_compute_dbu(p_cloud,p_driver_node_type,p_worker_node_type,p_num_workers,p_photon_enabled,'ALL_PURPOSE',NULL) dbu_ph,
           calculate_hours_per_month('ALL_PURPOSE',p_runs_per_day,p_avg_runtime_minutes,p_days_per_month,NULL) hrs,
           get_dbu_price(p_cloud,p_region,p_tier,get_product_type_for_pricing('ALL_PURPOSE',false,p_photon_enabled,NULL,NULL,NULL)) price,
           get_vm_cost_per_hour(p_cloud,p_region,p_driver_node_type,'on_demand','NA') drv,
           get_vm_cost_per_hour(p_cloud,p_region,p_worker_node_type,'on_demand','NA') wrk)
  SELECT 'ALL_PURPOSE_CLASSIC', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2),
         round((drv + wrk*COALESCE(p_num_workers,0))*hrs,2),
         round(dbu_ph*hrs*price + (drv + wrk*COALESCE(p_num_workers,0))*hrs,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_dlt_classic_cost(
  p_cloud STRING, p_region STRING, p_tier STRING, p_dlt_edition STRING,
  p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT,
  p_photon_enabled BOOLEAN, p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost estimate for a classic Lakeflow Declarative Pipeline (DLT). p_dlt_edition in CORE/PRO/ADVANCED.'
RETURN
  WITH b AS (
    SELECT calculate_classic_compute_dbu(p_cloud,p_driver_node_type,p_worker_node_type,p_num_workers,p_photon_enabled,'DLT',p_dlt_edition) dbu_ph,
           calculate_hours_per_month('DLT',p_runs_per_day,p_avg_runtime_minutes,p_days_per_month,NULL) hrs,
           get_dbu_price(p_cloud,p_region,p_tier,get_product_type_for_pricing('DLT',false,p_photon_enabled,p_dlt_edition,NULL,NULL)) price,
           get_vm_cost_per_hour(p_cloud,p_region,p_driver_node_type,'on_demand','NA') drv,
           get_vm_cost_per_hour(p_cloud,p_region,p_worker_node_type,'on_demand','NA') wrk)
  SELECT 'DLT_'||upper(COALESCE(p_dlt_edition,'CORE')), dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2),
         round((drv + wrk*COALESCE(p_num_workers,0))*hrs,2),
         round(dbu_ph*hrs*price + (drv + wrk*COALESCE(p_num_workers,0))*hrs,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_serverless_compute_cost(
  p_cloud STRING, p_region STRING, p_tier STRING, p_workload_type STRING,
  p_driver_node_type STRING, p_worker_node_type STRING, p_num_workers INT, p_serverless_mode STRING,
  p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost estimate for serverless JOBS or ALL_PURPOSE compute (no separate VM cost; DBU price includes compute).'
RETURN
  WITH b AS (
    SELECT calculate_serverless_compute_dbu(p_cloud,p_driver_node_type,p_worker_node_type,p_num_workers,p_workload_type,p_serverless_mode) dbu_ph,
           calculate_hours_per_month(p_workload_type,p_runs_per_day,p_avg_runtime_minutes,p_days_per_month,NULL) hrs,
           get_dbu_price(p_cloud,p_region,p_tier,get_product_type_for_pricing(p_workload_type,true,true,NULL,NULL,NULL)) price)
  SELECT upper(p_workload_type)||'_SERVERLESS', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2),
         0.0, round(dbu_ph*hrs*price,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_dbsql_cost(
  p_cloud STRING, p_region STRING, p_tier STRING,
  p_warehouse_type STRING, p_warehouse_size STRING, p_num_clusters INT,
  p_runs_per_day INT, p_avg_runtime_minutes INT, p_days_per_month INT, p_hours_per_month DOUBLE)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost estimate for a Databricks SQL warehouse. p_warehouse_type in classic/pro/serverless; serverless has no separate VM cost.'
RETURN
  WITH whc AS (
    SELECT driver_instance_type di, worker_instance_type wi, worker_count wc
    FROM ref_dbsql_warehouse_config
    WHERE upper(cloud)=upper(p_cloud) AND lower(warehouse_size)=lower(p_warehouse_type) AND upper(warehouse_type)=upper(p_warehouse_size) LIMIT 1),
  calc AS (
    SELECT calculate_dbsql_dbu(p_cloud,p_warehouse_type,p_warehouse_size,p_num_clusters) dbu_ph,
           calculate_hours_per_month('DBSQL',p_runs_per_day,p_avg_runtime_minutes,p_days_per_month,p_hours_per_month) hrs,
           get_dbu_price(p_cloud,p_region,p_tier,get_product_type_for_pricing('DBSQL',false,false,NULL,p_warehouse_type,NULL)) price,
           CASE WHEN lower(p_warehouse_type)='serverless' THEN 0.0 ELSE
             get_vm_cost_per_hour(p_cloud,p_region,whc.di,'on_demand','NA')*COALESCE(p_num_clusters,1)
             + get_vm_cost_per_hour(p_cloud,p_region,whc.wi,'on_demand','NA')*COALESCE(whc.wc,0)*COALESCE(p_num_clusters,1)
           END vm_ph
    FROM (SELECT 1 one) d LEFT JOIN whc ON true)
  SELECT 'DBSQL_'||upper(p_warehouse_type), dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2),
         round(vm_ph*hrs,2), round(dbu_ph*hrs*price + vm_ph*hrs,2) FROM calc

-- COMMAND ----------

CREATE OR REPLACE FUNCTION get_serverless_rate(p_cloud STRING, p_product STRING, p_size_or_model STRING)
RETURNS DOUBLE
COMMENT 'DBU rate from ref_serverless_rates for a product (vector_search/model_serving) and size/model.'
RETURN COALESCE((SELECT dbu_rate FROM ref_serverless_rates
  WHERE upper(cloud)=upper(p_cloud) AND lower(product)=lower(p_product) AND upper(size_or_model)=upper(p_size_or_model) LIMIT 1), 0)

-- COMMAND ----------

CREATE OR REPLACE FUNCTION calculate_vector_search_dbu(p_cloud STRING, p_mode STRING, p_capacity_millions DOUBLE)
RETURNS DOUBLE
COMMENT 'Vector Search DBU/hour = rate x CEIL(capacity_millions / divisor); divisor standard=2M, storage_optimized=64M.'
RETURN get_serverless_rate(p_cloud,'vector_search',p_mode)
  * ceil(COALESCE(p_capacity_millions,0) / CASE WHEN lower(p_mode)='storage_optimized' THEN 64.0 ELSE 2.0 END)

-- COMMAND ----------

CREATE OR REPLACE FUNCTION calculate_model_serving_dbu(p_cloud STRING, p_serverless_size STRING, p_concurrency INT)
RETURNS DOUBLE
COMMENT 'Model Serving DBU/hour = rate x (concurrency for cpu* sizes, else concurrency/4 for GPU).'
RETURN get_serverless_rate(p_cloud,'model_serving',p_serverless_size)
  * CASE WHEN lower(COALESCE(p_serverless_size,'cpu')) LIKE 'cpu%' THEN COALESCE(p_concurrency,4) ELSE COALESCE(p_concurrency,4)/4.0 END

-- COMMAND ----------

CREATE OR REPLACE FUNCTION calculate_lakebase_dbu(p_lakebase_cu INT, p_lakebase_ha_nodes INT)
RETURNS DOUBLE
COMMENT 'Lakebase DBU/hour = capacity_units x HA_nodes.'
RETURN COALESCE(p_lakebase_cu,0) * COALESCE(p_lakebase_ha_nodes,1)

-- COMMAND ----------

CREATE OR REPLACE FUNCTION get_fmapi_databricks_dbu(p_cloud STRING, p_model STRING, p_rate_type STRING, p_quantity BIGINT)
RETURNS DOUBLE
COMMENT 'FM API (Databricks-hosted) DBU for a quantity: hourly rate x hours, else (tokens / input_divisor) x rate.'
RETURN COALESCE((SELECT CASE WHEN COALESCE(is_hourly,false) THEN p_quantity*dbu_rate
  ELSE (p_quantity / cast(COALESCE(input_divisor,1) as double)) * dbu_rate END
  FROM ref_fmapi_databricks_rates
  WHERE upper(cloud)=upper(p_cloud) AND upper(model)=upper(p_model) AND lower(rate_type)=lower(p_rate_type) LIMIT 1), 0)

-- COMMAND ----------

CREATE OR REPLACE FUNCTION get_fmapi_proprietary_dbu(p_cloud STRING, p_provider STRING, p_model STRING, p_endpoint_type STRING, p_context_length STRING, p_rate_type STRING, p_quantity BIGINT)
RETURNS DOUBLE
COMMENT 'FM API (proprietary models) DBU for a quantity: hourly rate x hours, else (tokens / input_divisor) x rate.'
RETURN COALESCE((SELECT CASE WHEN COALESCE(is_hourly,false) THEN p_quantity*dbu_rate
  ELSE (p_quantity / cast(COALESCE(input_divisor,1) as double)) * dbu_rate END
  FROM ref_fmapi_proprietary_rates
  WHERE upper(cloud)=upper(p_cloud) AND upper(provider)=upper(p_provider) AND upper(model)=upper(p_model)
    AND lower(endpoint_type)=lower(COALESCE(p_endpoint_type,'global')) AND lower(context_length)=lower(COALESCE(p_context_length,'all'))
    AND lower(rate_type)=lower(p_rate_type) LIMIT 1), 0)

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_vector_search_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_mode STRING, p_capacity_millions DOUBLE, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for a Vector Search endpoint (always-on). p_mode in standard/storage_optimized; p_capacity_millions = indexed vectors in millions.'
RETURN WITH b AS (
  SELECT calculate_vector_search_dbu(p_cloud,p_mode,p_capacity_millions) dbu_ph,
         24.0*COALESCE(p_days_per_month,30) hrs,
         get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'VECTOR_SEARCH', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2), 0.0, round(dbu_ph*hrs*price,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_model_serving_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_serverless_size STRING, p_concurrency INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for a Model Serving endpoint (always-on). p_serverless_size e.g. cpu, gpu_small_t4, gpu_medium_a10g_1x; p_concurrency = provisioned concurrency.'
RETURN WITH b AS (
  SELECT calculate_model_serving_dbu(p_cloud,p_serverless_size,p_concurrency) dbu_ph,
         24.0*COALESCE(p_days_per_month,30) hrs,
         get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'MODEL_SERVING', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2), 0.0, round(dbu_ph*hrs*price,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_lakebase_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_capacity_units INT, p_ha_nodes INT, p_days_per_month INT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for a Lakebase (managed Postgres) instance (always-on). p_capacity_units = CU, p_ha_nodes = HA node count.'
RETURN WITH b AS (
  SELECT calculate_lakebase_dbu(p_capacity_units,p_ha_nodes) dbu_ph,
         24.0*COALESCE(p_days_per_month,30) hrs,
         get_dbu_price(p_cloud,p_region,p_tier,'DATABASE_SERVERLESS_COMPUTE') price)
  SELECT 'LAKEBASE', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2), 0.0, round(dbu_ph*hrs*price,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_databricks_apps_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_app_size STRING, p_num_apps INT, p_hours_per_month DOUBLE)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for Databricks Apps. p_app_size in medium (0.5 DBU/app-hr) or large (1.0); p_hours_per_month default 730.'
RETURN WITH b AS (
  SELECT (CASE WHEN lower(p_app_size)='large' THEN 1.0 ELSE 0.5 END)*COALESCE(p_num_apps,1) dbu_ph,
         COALESCE(p_hours_per_month,730.0) hrs,
         get_dbu_price(p_cloud,p_region,p_tier,'ALL_PURPOSE_SERVERLESS_COMPUTE') price)
  SELECT 'DATABRICKS_APPS', dbu_ph, hrs, dbu_ph*hrs, price, round(dbu_ph*hrs*price,2), 0.0, round(dbu_ph*hrs*price,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_fmapi_databricks_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_model STRING, p_rate_type STRING, p_quantity BIGINT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for Databricks-hosted Foundation Model API. p_rate_type e.g. input_token/output_token (p_quantity=tokens) or a provisioned/hourly rate (p_quantity=hours).'
RETURN WITH b AS (
  SELECT get_fmapi_databricks_dbu(p_cloud,p_model,p_rate_type,p_quantity) dbu_m,
         get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'FMAPI_DATABRICKS', 0.0, 0.0, dbu_m, price, round(dbu_m*price,2), 0.0, round(dbu_m*price,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_fmapi_proprietary_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_provider STRING, p_model STRING, p_endpoint_type STRING, p_context_length STRING, p_rate_type STRING, p_quantity BIGINT)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for proprietary-model Foundation Model API. p_provider e.g. anthropic/openai/google; p_quantity=tokens (token rates) or hours (batch_inference).'
RETURN WITH b AS (
  SELECT get_fmapi_proprietary_dbu(p_cloud,p_provider,p_model,p_endpoint_type,p_context_length,p_rate_type,p_quantity) dbu_m,
         get_dbu_price(p_cloud,p_region,p_tier,
           CASE WHEN upper(p_provider)='GOOGLE' THEN 'GEMINI_MODEL_SERVING' ELSE upper(p_provider)||'_MODEL_SERVING' END) price)
  SELECT 'FMAPI_PROPRIETARY', 0.0, 0.0, dbu_m, price, round(dbu_m*price,2), 0.0, round(dbu_m*price,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_ai_parse_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_mode STRING, p_pages_thousands DOUBLE, p_complexity STRING, p_hours_per_month DOUBLE)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for AI Parse (Document AI). p_mode pages: dbu = pages_thousands x complexity rate (low_text 12.5, low_images 22.5, medium 62.5, high 87.5). p_mode dbu: dbu = hours_per_month.'
RETURN WITH b AS (
  SELECT CASE WHEN lower(COALESCE(p_mode,'pages'))='dbu' THEN COALESCE(p_hours_per_month,0)
         ELSE COALESCE(p_pages_thousands,0) * CASE lower(COALESCE(p_complexity,'medium'))
           WHEN 'low_text' THEN 12.5 WHEN 'low_images' THEN 22.5 WHEN 'high' THEN 87.5 ELSE 62.5 END END dbu_m,
         get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'AI_PARSE', 0.0, 0.0, dbu_m, price, round(dbu_m*price,2), 0.0, round(dbu_m*price,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_ai_classify_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_document_type STRING, p_num_docs BIGINT, p_custom_rate_per_1000 DOUBLE)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for ai_classify. dbu = (num_docs/1000) x rate; rates short_text 4.5, rental_contract 50; else p_custom_rate_per_1000.'
RETURN WITH b AS (
  SELECT (COALESCE(p_num_docs,0)/1000.0) * CASE lower(COALESCE(p_document_type,'custom'))
           WHEN 'short_text' THEN 4.5 WHEN 'rental_contract' THEN 50.0 ELSE COALESCE(p_custom_rate_per_1000,0) END dbu_m,
         get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'AI_CLASSIFY', 0.0, 0.0, dbu_m, price, round(dbu_m*price,2), 0.0, round(dbu_m*price,2) FROM b

-- COMMAND ----------

CREATE OR REPLACE FUNCTION estimate_ai_extract_cost(p_cloud STRING, p_region STRING, p_tier STRING, p_document_type STRING, p_num_inputs BIGINT, p_custom_rate_per_1000 DOUBLE)
RETURNS TABLE(workload STRING, dbu_per_hour DOUBLE, hours_per_month DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE, total_cost_per_month DOUBLE)
COMMENT 'Monthly cost for ai_extract. dbu = (num_inputs/1000) x rate; rates short_text 45, invoice 45, complex_reasoning 562.5, deep_nesting 537.5; else p_custom_rate_per_1000.'
RETURN WITH b AS (
  SELECT (COALESCE(p_num_inputs,0)/1000.0) * CASE lower(COALESCE(p_document_type,'custom'))
           WHEN 'short_text' THEN 45.0 WHEN 'invoice' THEN 45.0 WHEN 'complex_reasoning' THEN 562.5 WHEN 'deep_nesting' THEN 537.5 ELSE COALESCE(p_custom_rate_per_1000,0) END dbu_m,
         get_dbu_price(p_cloud,p_region,p_tier,'SERVERLESS_REAL_TIME_INFERENCE') price)
  SELECT 'AI_EXTRACT', 0.0, 0.0, dbu_m, price, round(dbu_m*price,2), 0.0, round(dbu_m*price,2) FROM b
