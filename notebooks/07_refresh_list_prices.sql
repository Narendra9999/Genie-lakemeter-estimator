-- Databricks notebook source
-- MAGIC %md
-- MAGIC # 07 · Refresh DCP list-price snapshot
-- MAGIC Re-snapshots current AWS Databricks list prices from `system.billing.list_prices` into
-- MAGIC `dcp_list_price_rates` (the primary DCP rate source; governed rates remain the fallback).
-- MAGIC Run from time to time, or schedule it as a job. Needs SELECT on `system.billing.list_prices`;
-- MAGIC Genie users only need SELECT on the snapshot table.

-- COMMAND ----------

CREATE WIDGET TEXT catalog DEFAULT 'main';
CREATE WIDGET TEXT schema DEFAULT 'lakemeter'

-- COMMAND ----------

USE CATALOG ${catalog};
USE SCHEMA ${schema}

-- COMMAND ----------

CREATE OR REPLACE TABLE dcp_list_price_rates
COMMENT 'Snapshot of current AWS Databricks list prices from system.billing.list_prices (DBU SKUs). Primary rate source for DCP estimates. Refresh from time to time by re-running this statement; refreshed_at shows the snapshot time.'
AS SELECT upper(sku_name) AS sku_name, usage_unit, currency_code,
          CAST(coalesce(pricing.effective_list.default, pricing.default) AS DOUBLE) AS rate,
          price_start_time, current_timestamp() AS refreshed_at
   FROM system.billing.list_prices
   WHERE upper(cloud)='AWS' AND usage_unit='DBU'
     AND price_start_time <= current_timestamp()
     AND (price_end_time IS NULL OR price_end_time > current_timestamp())
     AND coalesce(pricing.effective_list.default, pricing.default) IS NOT NULL

-- COMMAND ----------

SELECT count(*) AS dbu_skus, max(refreshed_at) AS refreshed_at,
       max_by(rate, sku_name = 'ENTERPRISE_JOBS_SERVERLESS_COMPUTE_US_EAST_N_VIRGINIA') AS jobs_serverless_ent_use1
FROM dcp_list_price_rates
