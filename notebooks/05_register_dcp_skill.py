# Databricks notebook source
# MAGIC %md
# MAGIC # 05 · DCP Smart Estimation skill
# MAGIC Creates the DCP scenario-estimation functions (`dcp_estimate`, `dcp_estimate_storage`) and
# MAGIC registers them + routing instructions into an existing Genie Space, so "give a DCP estimation
# MAGIC for this workload" uses governed-rate (Jobs Serverless $0.3465, SQL Pro $0.4235) LOW/EXPECTED/
# MAGIC HIGH/STRESS planning math. Run after 03 (needs the Space id).

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Target catalog")
dbutils.widgets.text("schema", "lakemeter", "Target schema")
dbutils.widgets.text("space_id", "", "Existing Genie Space id")
catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema").strip()
space_id = dbutils.widgets.get("space_id").strip()
FQ = f"{catalog}.{schema}"
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()

# COMMAND ----------

DCP_SQL = r"""
CREATE OR REPLACE TABLE __FQ__.dcp_list_price_rates
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
CREATE OR REPLACE TABLE __FQ__.dcp_governed_rates (
  service_key STRING, rate DOUBLE, currency STRING, note STRING)
COMMENT 'Governed (account-negotiated) DCP rates. FALLBACK only - used when no current list price exists for the service/tier/region.'
-- @@
INSERT INTO __FQ__.dcp_governed_rates VALUES
 ('JOBS_SERVERLESS',0.3465,'USD','Governed account override (DCP calculator v3.8)'),
 ('SQL_PRO',0.4235,'USD','Governed account override (DCP calculator v3.8)')
-- @@
CREATE OR REPLACE FUNCTION __FQ__.dcp_resolve_rate(p_service STRING, p_tier STRING, p_region STRING)
RETURNS TABLE(rate DOUBLE, rate_source STRING, rate_sku STRING)
COMMENT 'Resolve the DBU rate for a DCP service (JOBS_SERVERLESS / SQL_PRO). 1st: list price snapshot dcp_list_price_rates (refreshed from system.billing.list_prices) for {tier}_{service}_COMPUTE_{sku_region}; 2nd (fallback): dcp_governed_rates. Always returns exactly one row.'
RETURN
  WITH k AS (
    SELECT upper(coalesce(p_tier,'ENTERPRISE')) || '_' || upper(p_service) || '_COMPUTE_' ||
           upper(coalesce(max(m.sku_region),'US_EAST_N_VIRGINIA')) AS sku
    FROM __FQ__.ref_sku_region_map m
    WHERE upper(m.cloud)='AWS' AND lower(m.region_code)=lower(coalesce(p_region,'us-east-1'))),
  lp AS (
    SELECT max_by(l.rate, l.price_start_time) AS rate
    FROM __FQ__.dcp_list_price_rates l, k
    WHERE l.sku_name = k.sku),
  gv AS (
    SELECT max(rate) AS rate FROM __FQ__.dcp_governed_rates
    WHERE upper(service_key)=upper(p_service))
  SELECT coalesce(lp.rate, gv.rate) AS rate,
         CASE WHEN lp.rate IS NOT NULL THEN 'LIST_PRICE' WHEN gv.rate IS NOT NULL THEN 'GOVERNED_FALLBACK' ELSE 'NO_RATE' END AS rate_source,
         k.sku AS rate_sku
  FROM k, lp, gv
-- @@
CREATE OR REPLACE FUNCTION __FQ__.dcp_estimate(
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
  CROSS JOIN __FQ__.dcp_resolve_rate(
      CASE WHEN upper(p_workload_type) LIKE '%SQL%' THEN 'SQL_PRO' ELSE 'JOBS_SERVERLESS' END,
      coalesce(p_tier,'ENTERPRISE'), coalesce(p_region,'us-east-1')) r
  ORDER BY x.ord
-- @@
CREATE OR REPLACE FUNCTION __FQ__.dcp_estimate_storage(
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
"""
for stmt in [s.strip() for s in DCP_SQL.split("\n-- @@\n") if s.strip()]:
    spark.sql(stmt.replace("__FQ__", FQ))
print("created dcp_estimate + dcp_estimate_storage in", FQ)

# COMMAND ----------

import json, uuid, requests
def nid(): return uuid.uuid4().hex
if not space_id:
    print("space_id blank -> functions created but Genie Space not updated. Set space_id to register the skill.")
else:
    host = "https://" + spark.conf.get("spark.databricks.workspaceUrl")
    H = {"Authorization": f"Bearer {ctx.apiToken().get()}"}
    cur = requests.get(f"{host}/api/2.0/genie/spaces/{space_id}?include_serialized_space=true", headers=H); cur.raise_for_status(); cur = cur.json()
    etag = cur["etag"]; space = json.loads(cur["serialized_space"]); instr = space["instructions"]
    have = {f["identifier"] for f in instr.get("sql_functions", [])}
    for fn in (f"{FQ}.dcp_estimate", f"{FQ}.dcp_estimate_storage", f"{FQ}.dcp_resolve_rate"):
        if fn not in have: instr.setdefault("sql_functions", []).append({"id": nid(), "identifier": fn})
    instr["sql_functions"] = sorted(instr["sql_functions"], key=lambda x: x["id"])
    dcp_text = [
      "DCP ESTIMATION (DCP Smart Estimation engine): when the user asks for a 'DCP estimation'/'DCP estimate' or a scenario-based consumption projection for a workload, use dcp_estimate(workload_type, quantity, runtime_low, runtime_expected, runtime_high, events_per_month, concurrent_users, planning_days[, tier, region]). It returns LOW/EXPECTED/HIGH/STRESS monthly DBU and USD. RATES: it FIRST uses the Databricks list price from the dcp_list_price_rates snapshot (refreshed periodically from system.billing.list_prices) (SKU {tier}_JOBS_SERVERLESS_COMPUTE_{region} or {tier}_SQL_PRO_COMPUTE_{region}); only if no list price exists does it FALL BACK to governed rates (Jobs Serverless $0.3465/DBU, SQL Pro $0.4235/DBU). Always show the rate and rate_source columns. tier defaults to ENTERPRISE and region to us-east-1; pass the user's tier/region when given. Prefer dcp_estimate whenever the user explicitly says DCP.",
      "workload_type: CONTINUOUS_STREAMING (uses quantity; 730 hrs/mo x intensity 0.5/1.0/1.5/2.0) | EVENT_DRIVEN_BATCH (runtime range runtime_low/runtime_expected/runtime_high applied per scenario at 1.0 DBU/h x events/month; default events LOW=0/EXPECTED=1/HIGH=1/STRESS=4) | SQL_REPORTING (concurrent_users and planning_days; bands=ceil(ceil(users x 0.5/1.0/1.5/2.0)/10), DBU=active_hours 0.5/1.0/2.0/4.0 x planning_days x 4 x bands).",
      f"S3+DataSync sensitivity: dcp_estimate_storage(daily_low, daily_expected, daily_high, planning_days); S3 is EXCLUDED_UNTIL_CONFIRMED (not in the recurring total). Defaults planning_days=22, quantity=1. Pass NULL for params not relevant to the type. Example: SELECT * FROM {FQ}.dcp_estimate('CONTINUOUS_STREAMING',1,NULL,NULL,NULL,NULL,NULL,NULL).",
    ]
    if instr.get("text_instructions"):
        keep = [c for c in instr["text_instructions"][0].get("content", []) if not c.startswith(("DCP ESTIMATION", "workload_type: CONTINUOUS_STREAMING", "S3+DataSync sensitivity"))]
        instr["text_instructions"][0]["content"] = keep + dcp_text
    else: instr["text_instructions"] = [{"id": nid(), "content": dcp_text}]
    for q, sql in [(["Give a DCP estimation for a continuously running streaming job."], [f"SELECT * FROM {FQ}.dcp_estimate('CONTINUOUS_STREAMING',1,NULL,NULL,NULL,NULL,NULL,NULL)"]),
                   (["DCP estimate for SQL reporting with 10 concurrent users over 22 working days."], [f"SELECT * FROM {FQ}.dcp_estimate('SQL_REPORTING',NULL,NULL,NULL,NULL,NULL,10,22)"]),
                   (["DCP estimation for an event-driven batch that runs 1h low / 3h expected / 5h high."], [f"SELECT * FROM {FQ}.dcp_estimate('EVENT_DRIVEN_BATCH',1,1.0,3.0,5.0,NULL,NULL,NULL)"])]:
        instr.setdefault("example_question_sqls", []).append({"id": nid(), "question": q, "sql": sql})
    instr["example_question_sqls"] = sorted(instr["example_question_sqls"], key=lambda x: x["id"])
    cfg = space.setdefault("config", {})
    cfg.setdefault("sample_questions", []).append({"id": nid(), "question": ["Give a DCP estimation for a continuous streaming workload."]})
    cfg["sample_questions"] = sorted(cfg["sample_questions"], key=lambda x: x["id"])
    r = requests.patch(f"{host}/api/2.0/genie/spaces/{space_id}", headers=H, json={"serialized_space": json.dumps(space), "etag": etag, "warehouse_id": cur.get("warehouse_id")}); r.raise_for_status()
    print("registered DCP skill in space", space_id)