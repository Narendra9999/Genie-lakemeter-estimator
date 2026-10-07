CREATE OR REPLACE FUNCTION fevm_catalog_naren.lakemeter.dcp_estimate(
  p_workload_type STRING, p_quantity INT,
  p_runtime_low DOUBLE, p_runtime_expected DOUBLE, p_runtime_high DOUBLE,
  p_events_per_month DOUBLE, p_concurrent_users INT, p_planning_days INT)
RETURNS TABLE(scenario STRING, workload_type STRING, compute STRING, estimated_dbu DOUBLE, rate DOUBLE, estimated_usd DOUBLE, basis STRING)
COMMENT 'DCP Smart Estimation (planning-floor): scenario LOW/EXPECTED/HIGH/STRESS monthly DBU + USD for ONE workload, at governed rates (Jobs Serverless 0.3465, SQL Pro 0.4235). CONTINUOUS_STREAMING uses quantity (730 hrs x intensity). EVENT_DRIVEN_BATCH uses a runtime range: LOW=runtime_low, EXPECTED=runtime_expected, HIGH/STRESS=runtime_high (x events/month at 1 DBU/h; default events LOW=0,EXPECTED=1,HIGH=1,STRESS=4). SQL_REPORTING uses concurrent_users + planning_days (concurrency bands). Pass NULL for params not relevant to the type.'
RETURN
  SELECT scenario, workload_type, compute, estimated_dbu, rate,
         round(estimated_dbu * rate, 2) AS estimated_usd, 'ARCHITECTURE_PLANNING_FLOOR' AS basis
  FROM (
    SELECT s.scenario,
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
        ELSE NULL END AS estimated_dbu,
      CASE WHEN upper(p_workload_type) LIKE '%SQL%' THEN 0.4235 ELSE 0.3465 END AS rate
    FROM (VALUES ('LOW',0.5,0.5,0.5,0.0),('EXPECTED',1.0,1.0,1.0,1.0),('HIGH',1.5,2.0,1.5,1.0),('STRESS',2.0,4.0,2.0,4.0))
         AS s(scenario, cont_dbu_h, sql_active_h, sql_conc_f, events_default)
  ) x
  ORDER BY CASE scenario WHEN 'LOW' THEN 1 WHEN 'EXPECTED' THEN 2 WHEN 'HIGH' THEN 3 ELSE 4 END
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
