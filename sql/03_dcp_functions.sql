CREATE OR REPLACE TABLE fevm_catalog_naren.lakemeter.dcp_list_price_rates
COMMENT 'Snapshot of current AWS Databricks list prices from system.billing.list_prices (DBU SKUs). Primary rate source for DCP estimates. Refresh from time to time by re-running this statement; refreshed_at shows the snapshot time.'
AS SELECT upper(sku_name) AS sku_name, usage_unit, currency_code,
          CAST(coalesce(pricing.effective_list.default, pricing.default) AS DOUBLE) AS rate,
          price_start_time, current_timestamp() AS refreshed_at
   FROM system.billing.list_prices
   WHERE upper(cloud)='AWS' AND usage_unit='DBU'
     AND price_start_time <= current_timestamp()
     AND (price_end_time IS NULL OR price_end_time > current_timestamp())
     AND coalesce(pricing.effective_list.default, pricing.default) IS NOT NULL
-- @@
CREATE OR REPLACE TABLE fevm_catalog_naren.lakemeter.dcp_governed_rates (
  service_key STRING, rate DOUBLE, currency STRING, note STRING)
COMMENT 'Governed (account-negotiated) DCP rates. FALLBACK only - used when no current list price exists for the service/tier/region.'
-- @@
INSERT INTO fevm_catalog_naren.lakemeter.dcp_governed_rates VALUES
 ('JOBS_SERVERLESS',0.3465,'USD','Governed account override (DCP calculator v3.8)'),
 ('SQL_PRO',0.4235,'USD','Governed account override (DCP calculator v3.8)')
-- @@
CREATE OR REPLACE FUNCTION fevm_catalog_naren.lakemeter.dcp_resolve_rate(p_service STRING, p_tier STRING, p_region STRING)
RETURNS TABLE(rate DOUBLE, rate_source STRING, rate_sku STRING)
COMMENT 'Resolve the DBU rate for a DCP service (JOBS_SERVERLESS / SQL_PRO). 1st: list price snapshot dcp_list_price_rates (refreshed from system.billing.list_prices) for {tier}_{service}_COMPUTE_{sku_region}; 2nd (fallback): dcp_governed_rates. Always returns exactly one row.'
RETURN
  WITH k AS (
    SELECT upper(coalesce(p_tier,'ENTERPRISE')) || '_' || upper(p_service) || '_COMPUTE_' ||
           upper(coalesce(max(m.sku_region),'US_EAST_N_VIRGINIA')) AS sku
    FROM fevm_catalog_naren.lakemeter.ref_sku_region_map m
    WHERE upper(m.cloud)='AWS' AND lower(m.region_code)=lower(coalesce(p_region,'us-east-1'))),
  lp AS (
    SELECT max_by(l.rate, l.price_start_time) AS rate
    FROM fevm_catalog_naren.lakemeter.dcp_list_price_rates l, k
    WHERE l.sku_name = k.sku),
  gv AS (
    SELECT max(rate) AS rate FROM fevm_catalog_naren.lakemeter.dcp_governed_rates
    WHERE upper(service_key)=upper(p_service))
  SELECT coalesce(lp.rate, gv.rate) AS rate,
         CASE WHEN lp.rate IS NOT NULL THEN 'LIST_PRICE' WHEN gv.rate IS NOT NULL THEN 'GOVERNED_FALLBACK' ELSE 'NO_RATE' END AS rate_source,
         k.sku AS rate_sku
  FROM k, lp, gv
-- @@
CREATE OR REPLACE FUNCTION fevm_catalog_naren.lakemeter.dcp_estimate(
  p_workload_type STRING, p_quantity INT,
  p_runtime_low DOUBLE, p_runtime_expected DOUBLE, p_runtime_high DOUBLE,
  p_events_per_month DOUBLE, p_concurrent_users INT, p_planning_days INT,
  p_tier STRING DEFAULT 'ENTERPRISE', p_region STRING DEFAULT 'us-east-1')
RETURNS TABLE(scenario STRING, workload_type STRING, compute STRING, estimated_dbu DOUBLE, rate DOUBLE, estimated_usd DOUBLE, basis STRING, rate_source STRING, rate_sku STRING)
COMMENT 'DCP Smart Estimation (planning-floor): LOW/EXPECTED/HIGH/STRESS monthly DBU + USD for ONE workload. RATE: first the list price from the dcp_list_price_rates snapshot (refreshed from system.billing.list_prices) ({tier}_{JOBS_SERVERLESS|SQL_PRO}_COMPUTE_{region}), falling back to governed rates (Jobs Serverless 0.3465, SQL Pro 0.4235); rate_source says which. p_tier default ENTERPRISE, p_region default us-east-1. CONTINUOUS_STREAMING uses quantity (730 hrs x intensity). EVENT_DRIVEN_BATCH uses runtime_low/expected/high per scenario x events/month at 1 DBU/h (default events LOW=0,EXPECTED=1,HIGH=1,STRESS=4). SQL_REPORTING uses concurrent_users + planning_days (concurrency bands). Pass NULL for params not relevant to the type.'
RETURN
  SELECT x.scenario, x.workload_type, x.compute, x.estimated_dbu, r.rate,
         round(x.estimated_dbu * r.rate, 2) AS estimated_usd, 'ARCHITECTURE_PLANNING_FLOOR' AS basis,
         r.rate_source, r.rate_sku
  FROM (
    SELECT s.scenario, s.ord,
      upper(p_workload_type) AS workload_type,
      CASE WHEN upper(p_workload_type) LIKE '%SQL%' THEN 'SQL_PRO' ELSE 'JOBS_SERVERLESS' END AS compute,
      CASE
        WHEN upper(p_workload_type) IN ('CONTINUOUS_STREAMING','STREAMING')
          THEN 730.0 * s.cont_dbu_h * COALESCE(p_quantity,1)
        WHEN upper(p_workload_type) IN ('EVENT_DRIVEN_BATCH','SCHEDULED_BATCH','BATCH')
          THEN 1.0 * COALESCE(p_events_per_month, s.events_default) * COALESCE(p_quantity,1)
               * (CASE s.scenario
                    WHEN 'LOW'      THEN COALESCE(p_runtime_low, p_runtime_expected, 0)
                    WHEN 'EXPECTED' THEN COALESCE(p_runtime_expected, p_runtime_low, 0)
                    ELSE                 COALESCE(p_runtime_high, p_runtime_expected, 0) END)
        WHEN upper(p_workload_type) LIKE '%SQL%'
          THEN s.sql_active_h * COALESCE(p_planning_days,22) * 4.0
               * greatest(1, CAST(ceil(ceil(COALESCE(p_concurrent_users,1) * s.sql_conc_f)/10.0) AS INT))
        ELSE NULL END AS estimated_dbu
    FROM (VALUES ('LOW',1,0.5,0.5,0.5,0.0),('EXPECTED',2,1.0,1.0,1.0,1.0),('HIGH',3,1.5,2.0,1.5,1.0),('STRESS',4,2.0,4.0,2.0,4.0))
         AS s(scenario, ord, cont_dbu_h, sql_active_h, sql_conc_f, events_default)
  ) x
  CROSS JOIN fevm_catalog_naren.lakemeter.dcp_resolve_rate(
      CASE WHEN upper(p_workload_type) LIKE '%SQL%' THEN 'SQL_PRO' ELSE 'JOBS_SERVERLESS' END,
      coalesce(p_tier,'ENTERPRISE'), coalesce(p_region,'us-east-1')) r
  ORDER BY x.ord
-- @@
CREATE OR REPLACE FUNCTION fevm_catalog_naren.lakemeter.dcp_estimate_storage(
  p_daily_low DOUBLE, p_daily_expected DOUBLE, p_daily_high DOUBLE, p_planning_days INT)
RETURNS TABLE(scenario STRING, new_gb_month DOUBLE, retained_gb DOUBLE, objects BIGINT, s3_sensitivity_usd DOUBLE, s3_treatment STRING, datasync_usd DOUBLE)
COMMENT 'DCP storage/transfer sensitivity by scenario: S3 retained GB + object costs (AWS S3 $0.023/GB-mo, PUT/LIST $0.005/1k, GET $0.0004/1k) and DataSync transfer ($0.0125/GB). S3 is a sensitivity (EXCLUDED_UNTIL_CONFIRMED), not added to the recurring total.'
RETURN
  SELECT scenario, new_gb_month, retained_gb, objects,
         round(retained_gb*0.023 + objects/1000.0*(0.005+0.0004), 2) AS s3_sensitivity_usd,
         'EXCLUDED_UNTIL_CONFIRMED' AS s3_treatment,
         round(new_gb_month*0.0125, 2) AS datasync_usd
  FROM (
    SELECT scenario, new_gb_month, (new_gb_month*rm*amp) AS retained_gb,
           greatest(1, CAST((new_gb_month*rm*amp)*1024/obj AS BIGINT)) AS objects
    FROM (
      SELECT s.scenario,
        (CASE s.scenario WHEN 'LOW' THEN COALESCE(p_daily_low,0) WHEN 'EXPECTED' THEN COALESCE(p_daily_expected,0) ELSE COALESCE(p_daily_high,0) END)
          * COALESCE(p_planning_days,22) AS new_gb_month,
        s.rm, s.amp, s.obj
      FROM (VALUES ('LOW',1.0,1.5,256.0),('EXPECTED',3.0,2.0,128.0),('HIGH',12.0,3.0,64.0),('STRESS',12.0,4.0,32.0))
           AS s(scenario, rm, amp, obj)
    ) a
  ) b
  ORDER BY CASE scenario WHEN 'LOW' THEN 1 WHEN 'EXPECTED' THEN 2 WHEN 'HIGH' THEN 3 ELSE 4 END
