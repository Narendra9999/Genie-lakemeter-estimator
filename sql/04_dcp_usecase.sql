CREATE TABLE IF NOT EXISTS fevm_catalog_naren.lakemeter.dcp_intake_components (
  use_case_id STRING, use_case_name STRING, component_id STRING, component_name STRING,
  workload_type STRING, quantity INT, runtime_low DOUBLE, runtime_expected DOUBLE, runtime_high DOUBLE,
  events_per_month DOUBLE, concurrent_users INT, planning_days INT)
COMMENT 'DCP named use cases and their workload components (one row per component), loaded from the intake spreadsheet.'
-- @@
CREATE OR REPLACE FUNCTION fevm_catalog_naren.lakemeter.dcp_estimate_usecase(p_use_case STRING)
RETURNS TABLE(use_case_id STRING, use_case_name STRING, scenario STRING, total_dbu DOUBLE, total_usd DOUBLE)
COMMENT 'DCP estimation for a NAMED use case in dcp_intake_components. p_use_case accepts EITHER the use_case_id OR the use_case_name (business name), case-insensitive. Rolls up all components per scenario (LOW/EXPECTED/HIGH/STRESS). Pass the user''s wording directly; no lookup needed.'
RETURN
  SELECT t.use_case_id, t.use_case_name, e.scenario, round(sum(e.estimated_dbu),2) AS total_dbu, round(sum(e.estimated_usd),2) AS total_usd
  FROM fevm_catalog_naren.lakemeter.dcp_intake_components t,
       LATERAL fevm_catalog_naren.lakemeter.dcp_estimate(t.workload_type,t.quantity,t.runtime_low,t.runtime_expected,t.runtime_high,t.events_per_month,t.concurrent_users,t.planning_days) e
  WHERE (upper(trim(t.use_case_id))=upper(trim(p_use_case)) OR upper(trim(t.use_case_name))=upper(trim(p_use_case)))
    AND e.estimated_dbu IS NOT NULL
  GROUP BY t.use_case_id, t.use_case_name, e.scenario
  ORDER BY t.use_case_id, CASE e.scenario WHEN 'LOW' THEN 1 WHEN 'EXPECTED' THEN 2 WHEN 'HIGH' THEN 3 ELSE 4 END
-- @@
CREATE OR REPLACE FUNCTION fevm_catalog_naren.lakemeter.dcp_estimate_usecase_components(p_use_case STRING)
RETURNS TABLE(use_case_id STRING, component_id STRING, component_name STRING, workload_type STRING, scenario STRING, estimated_dbu DOUBLE, estimated_usd DOUBLE)
COMMENT 'Per-component DCP breakdown for a NAMED use case. p_use_case accepts EITHER the use_case_id OR the use_case_name (business name), case-insensitive.'
RETURN
  SELECT t.use_case_id, t.component_id, t.component_name, t.workload_type, e.scenario, e.estimated_dbu, e.estimated_usd
  FROM fevm_catalog_naren.lakemeter.dcp_intake_components t,
       LATERAL fevm_catalog_naren.lakemeter.dcp_estimate(t.workload_type,t.quantity,t.runtime_low,t.runtime_expected,t.runtime_high,t.events_per_month,t.concurrent_users,t.planning_days) e
  WHERE upper(trim(t.use_case_id))=upper(trim(p_use_case)) OR upper(trim(t.use_case_name))=upper(trim(p_use_case))
  ORDER BY t.component_id, CASE e.scenario WHEN 'LOW' THEN 1 WHEN 'EXPECTED' THEN 2 WHEN 'HIGH' THEN 3 ELSE 4 END
