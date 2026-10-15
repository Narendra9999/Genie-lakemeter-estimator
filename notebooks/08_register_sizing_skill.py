# Databricks notebook source
# MAGIC %md
# MAGIC # 08 · Sizing guidance skill (estimate from workload sizes)
# MAGIC Lets Genie estimate from **workload sizes** and **ask for missing inputs first**. Three input modes:
# MAGIC * **DATA_VOLUME** - GB per run (batch), GB per day (streaming), GB queried (SQL)
# MAGIC * **TSHIRT** - XS / S / M / L / XL
# MAGIC * **CLUSTER_SPEC / WAREHOUSE_SPEC** - instance types + workers, or a SQL warehouse size (priced by the existing `estimate_*` functions)
# MAGIC
# MAGIC Creates `dcp_sizing_profiles` (governed T-shirt matrix - tune it to your own benchmarks), `dcp_sizing_inputs`
# MAGIC (required inputs + the question to ask), and the functions `dcp_sizing_questions`, `dcp_size_recommend`,
# MAGIC `dcp_estimate_sized`. Run after 05 (needs `dcp_resolve_rate`). Set `space_id` to register the skill on the Genie Space.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Target catalog")
dbutils.widgets.text("schema", "lakemeter", "Target schema")
dbutils.widgets.text("space_id", "", "Existing Genie Space id (optional)")
catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema").strip()
space_id = dbutils.widgets.get("space_id").strip()
FQ = f"{catalog}.{schema}"
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()

# COMMAND ----------

SIZING_SQL = r"""
CREATE OR REPLACE TABLE __FQ__.dcp_sizing_profiles (
  workload_type STRING, tshirt_size STRING, size_order INT, volume_metric STRING,
  volume_min DOUBLE, volume_max DOUBLE, dbu_per_hour DOUBLE,
  runtime_low_h DOUBLE, runtime_expected_h DOUBLE, runtime_high_h DOUBLE,
  sql_warehouse_size STRING, classic_equivalent STRING, notes STRING)
COMMENT 'Governed T-shirt sizing matrix (XS/S/M/L/XL) used by the sizing guidance skill. Maps a data volume band to a serverless DBU/hour, a per-run runtime range (batch) and a SQL warehouse size (SQL). Planning assumptions - tune to the customer''s own benchmarks. volume_metric: GB_PER_RUN (batch), GB_PER_DAY (streaming), GB_QUERIED (SQL). volume_max NULL = no upper bound.'
-- @@
INSERT INTO __FQ__.dcp_sizing_profiles VALUES
 ('EVENT_DRIVEN_BATCH','XS',1,'GB_PER_RUN',0,10,2,0.25,0.5,1,NULL,'1 driver + 2 x m5d.xlarge','Small extracts, lookups'),
 ('EVENT_DRIVEN_BATCH','S',2,'GB_PER_RUN',10,100,4,0.5,1,2,NULL,'1 driver + 4 x m5d.xlarge','Departmental ETL'),
 ('EVENT_DRIVEN_BATCH','M',3,'GB_PER_RUN',100,500,12,0.75,1.5,3,NULL,'1 driver + 4 x m5d.4xlarge','Typical domain pipeline'),
 ('EVENT_DRIVEN_BATCH','L',4,'GB_PER_RUN',500,2000,24,1,2,4,NULL,'1 driver + 8 x m5d.4xlarge','Large joins / history rebuilds'),
 ('EVENT_DRIVEN_BATCH','XL',5,'GB_PER_RUN',2000,NULL,48,1.5,3,6,NULL,'1 driver + 16 x m5d.4xlarge','Enterprise-scale batch; validate with a benchmark run'),
 ('CONTINUOUS_STREAMING','XS',1,'GB_PER_DAY',0,10,1,NULL,NULL,NULL,NULL,'1 driver + 1 x m5d.large','Low-rate events'),
 ('CONTINUOUS_STREAMING','S',2,'GB_PER_DAY',10,50,2,NULL,NULL,NULL,NULL,'1 driver + 2 x m5d.xlarge','Single-topic ingest'),
 ('CONTINUOUS_STREAMING','M',3,'GB_PER_DAY',50,250,4,NULL,NULL,NULL,NULL,'1 driver + 4 x m5d.xlarge','Multi-topic ingest'),
 ('CONTINUOUS_STREAMING','L',4,'GB_PER_DAY',250,1000,8,NULL,NULL,NULL,NULL,'1 driver + 4 x m5d.2xlarge','High-throughput, stateful'),
 ('CONTINUOUS_STREAMING','XL',5,'GB_PER_DAY',1000,NULL,16,NULL,NULL,NULL,NULL,'1 driver + 8 x m5d.2xlarge','Very high throughput; validate with a load test'),
 ('SQL_REPORTING','XS',1,'GB_QUERIED',0,100,4,NULL,NULL,NULL,'2X-Small',NULL,'Small marts'),
 ('SQL_REPORTING','S',2,'GB_QUERIED',100,1000,6,NULL,NULL,NULL,'X-Small',NULL,'Departmental BI'),
 ('SQL_REPORTING','M',3,'GB_QUERIED',1000,5000,12,NULL,NULL,NULL,'Small',NULL,'Domain BI'),
 ('SQL_REPORTING','L',4,'GB_QUERIED',5000,20000,24,NULL,NULL,NULL,'Medium',NULL,'Large fact tables'),
 ('SQL_REPORTING','XL',5,'GB_QUERIED',20000,NULL,40,NULL,NULL,NULL,'Large',NULL,'Enterprise BI; validate with query benchmarks')
-- @@
CREATE OR REPLACE TABLE __FQ__.dcp_sizing_inputs (
  input_mode STRING, workload_type STRING, ord INT, input_name STRING, required BOOLEAN, question STRING, example STRING, target_function STRING)
COMMENT 'Inputs the agent must collect before a sizing-based estimate, per input mode (DATA_VOLUME, TSHIRT, CLUSTER_SPEC, WAREHOUSE_SPEC) and workload type. required=true inputs must be asked for if missing; question is the wording to use.'
-- @@
INSERT INTO __FQ__.dcp_sizing_inputs VALUES
 ('DATA_VOLUME','EVENT_DRIVEN_BATCH',1,'data_gb',true,'How much data does each run process (GB)?','250 GB per run','dcp_estimate_sized'),
 ('DATA_VOLUME','EVENT_DRIVEN_BATCH',2,'runs_per_month',true,'How many times does the job run per month?','30 (daily)','dcp_estimate_sized'),
 ('DATA_VOLUME','EVENT_DRIVEN_BATCH',3,'quantity',false,'How many similar jobs are there? (default 1)','3','dcp_estimate_sized'),
 ('DATA_VOLUME','CONTINUOUS_STREAMING',1,'data_gb',true,'How much data is ingested per day (GB/day)?','100 GB/day','dcp_estimate_sized'),
 ('DATA_VOLUME','CONTINUOUS_STREAMING',2,'quantity',false,'How many similar streams are there? (default 1)','2','dcp_estimate_sized'),
 ('DATA_VOLUME','SQL_REPORTING',1,'data_gb',true,'How much data do the reports query (GB)?','2,000 GB','dcp_estimate_sized'),
 ('DATA_VOLUME','SQL_REPORTING',2,'concurrent_users',true,'How many concurrent report users are there at peak?','40','dcp_estimate_sized'),
 ('DATA_VOLUME','SQL_REPORTING',3,'active_hours_per_day',false,'How many hours a day is the warehouse busy? (default 0.5/1/2/4 by scenario)','8','dcp_estimate_sized'),
 ('DATA_VOLUME','SQL_REPORTING',4,'planning_days',false,'How many working days per month? (default 22)','22','dcp_estimate_sized'),
 ('TSHIRT','EVENT_DRIVEN_BATCH',1,'tshirt_size',true,'Which size is the job: XS, S, M, L or XL?','M','dcp_estimate_sized'),
 ('TSHIRT','EVENT_DRIVEN_BATCH',2,'runs_per_month',true,'How many times does the job run per month?','30 (daily)','dcp_estimate_sized'),
 ('TSHIRT','EVENT_DRIVEN_BATCH',3,'quantity',false,'How many similar jobs are there? (default 1)','3','dcp_estimate_sized'),
 ('TSHIRT','CONTINUOUS_STREAMING',1,'tshirt_size',true,'Which size is the stream: XS, S, M, L or XL?','S','dcp_estimate_sized'),
 ('TSHIRT','CONTINUOUS_STREAMING',2,'quantity',false,'How many similar streams are there? (default 1)','2','dcp_estimate_sized'),
 ('TSHIRT','SQL_REPORTING',1,'tshirt_size',true,'Which size is the reporting workload: XS, S, M, L or XL?','M','dcp_estimate_sized'),
 ('TSHIRT','SQL_REPORTING',2,'concurrent_users',true,'How many concurrent report users are there at peak?','40','dcp_estimate_sized'),
 ('TSHIRT','SQL_REPORTING',3,'active_hours_per_day',false,'How many hours a day is the warehouse busy? (default 0.5/1/2/4 by scenario)','8','dcp_estimate_sized'),
 ('CLUSTER_SPEC','JOBS_CLASSIC',1,'worker_node_type',true,'Which instance type do the workers use?','i3.xlarge','estimate_jobs_classic_cost'),
 ('CLUSTER_SPEC','JOBS_CLASSIC',2,'num_workers',true,'How many workers?','8','estimate_jobs_classic_cost'),
 ('CLUSTER_SPEC','JOBS_CLASSIC',3,'runs_per_day',true,'How many runs per day?','1','estimate_jobs_classic_cost'),
 ('CLUSTER_SPEC','JOBS_CLASSIC',4,'avg_runtime_minutes',true,'How long is each run (minutes)?','240','estimate_jobs_classic_cost'),
 ('CLUSTER_SPEC','JOBS_CLASSIC',5,'days_per_month',true,'How many days a month does it run?','22','estimate_jobs_classic_cost'),
 ('CLUSTER_SPEC','JOBS_CLASSIC',6,'driver_node_type',false,'Which instance type is the driver? (default: same as workers)','i3.xlarge','estimate_jobs_classic_cost'),
 ('CLUSTER_SPEC','JOBS_CLASSIC',7,'photon_enabled',false,'Is Photon enabled? (default no)','true','estimate_jobs_classic_cost'),
 ('WAREHOUSE_SPEC','SQL_WAREHOUSE',1,'warehouse_size',true,'Which warehouse size (2X-Small to 4X-Large)?','Medium','estimate_dbsql_cost'),
 ('WAREHOUSE_SPEC','SQL_WAREHOUSE',2,'hours_per_day',true,'How many hours a day does it run?','10','estimate_dbsql_cost'),
 ('WAREHOUSE_SPEC','SQL_WAREHOUSE',3,'days_per_month',true,'How many days a month?','22','estimate_dbsql_cost'),
 ('WAREHOUSE_SPEC','SQL_WAREHOUSE',4,'warehouse_type',false,'Serverless, Pro or Classic? (default serverless)','serverless','estimate_dbsql_cost'),
 ('WAREHOUSE_SPEC','SQL_WAREHOUSE',5,'num_clusters',false,'How many clusters at peak? (default 1)','2','estimate_dbsql_cost')
-- @@
CREATE OR REPLACE FUNCTION __FQ__.dcp_sizing_questions(p_input_mode STRING, p_workload_type STRING DEFAULT NULL)
RETURNS TABLE(input_mode STRING, workload_type STRING, ord INT, input_name STRING, required BOOLEAN, question STRING, example STRING, target_function STRING)
COMMENT 'Sizing guidance: the inputs to collect (and the question to ask) before estimating from workload sizes. p_input_mode: DATA_VOLUME | TSHIRT | CLUSTER_SPEC | WAREHOUSE_SPEC. p_workload_type: EVENT_DRIVEN_BATCH | CONTINUOUS_STREAMING | SQL_REPORTING | JOBS_CLASSIC | SQL_WAREHOUSE, or NULL for all. Ask the user every required input that is missing before running an estimate.'
RETURN
  SELECT i.input_mode, i.workload_type, i.ord, i.input_name, i.required, i.question, i.example, i.target_function
  FROM __FQ__.dcp_sizing_inputs i
  WHERE upper(i.input_mode) = upper(p_input_mode)
    AND (p_workload_type IS NULL OR upper(i.workload_type) = upper(p_workload_type))
  ORDER BY i.workload_type, i.ord
-- @@
CREATE OR REPLACE FUNCTION __FQ__.dcp_size_recommend(
  p_workload_type STRING, p_tshirt_size STRING DEFAULT NULL, p_data_gb DOUBLE DEFAULT NULL)
RETURNS TABLE(workload_type STRING, tshirt_size STRING, size_basis STRING, volume_metric STRING, volume_band STRING,
              dbu_per_hour DOUBLE, runtime_range_h STRING, sql_warehouse_size STRING, classic_equivalent STRING, notes STRING, input_status STRING)
COMMENT 'Sizing guidance: recommend a T-shirt size (XS/S/M/L/XL) from a data volume, or describe a given size. p_workload_type: EVENT_DRIVEN_BATCH (data_gb = GB per run) | CONTINUOUS_STREAMING (data_gb = GB per day) | SQL_REPORTING (data_gb = GB queried); BATCH/STREAMING/SQL/BI accepted. A given p_tshirt_size wins over p_data_gb. Returns DBU/hour, runtime range, warehouse size and classic cluster equivalent from dcp_sizing_profiles; input_status = MISSING_INPUT if neither size nor volume is given.'
RETURN
  WITH p AS (
    SELECT CASE WHEN upper(p_workload_type) LIKE '%STREAM%' THEN 'CONTINUOUS_STREAMING'
                WHEN upper(p_workload_type) LIKE '%SQL%' OR upper(p_workload_type) LIKE '%REPORT%' OR upper(p_workload_type) LIKE '%BI%' THEN 'SQL_REPORTING'
                ELSE 'EVENT_DRIVEN_BATCH' END AS wt,
           CASE WHEN upper(trim(p_tshirt_size)) IN ('', 'NULL', 'NONE', 'N/A') THEN NULL
                WHEN upper(trim(p_tshirt_size)) IN ('EXTRA SMALL', 'X-SMALL', 'XSMALL') THEN 'XS' WHEN upper(trim(p_tshirt_size)) = 'SMALL' THEN 'S'
                WHEN upper(trim(p_tshirt_size)) = 'MEDIUM' THEN 'M' WHEN upper(trim(p_tshirt_size)) = 'LARGE' THEN 'L'
                WHEN upper(trim(p_tshirt_size)) IN ('EXTRA LARGE', 'X-LARGE', 'XLARGE') THEN 'XL' ELSE upper(trim(p_tshirt_size)) END AS sz,
           p_data_gb AS gb),
  c AS (
    SELECT pr.*, CASE WHEN p.sz IS NOT NULL THEN 'GIVEN_TSHIRT' ELSE 'FROM_DATA_VOLUME' END AS basis
    FROM __FQ__.dcp_sizing_profiles pr JOIN p ON pr.workload_type = p.wt
    WHERE (p.sz IS NOT NULL AND pr.tshirt_size = p.sz)
       OR (p.sz IS NULL AND p.gb IS NOT NULL AND p.gb >= pr.volume_min AND (pr.volume_max IS NULL OR p.gb < pr.volume_max)))
  SELECT p.wt, c.tshirt_size, c.basis, c.volume_metric,
         CASE WHEN c.tshirt_size IS NULL THEN NULL
              ELSE CAST(c.volume_min AS STRING) || ' - ' || coalesce(CAST(c.volume_max AS STRING), 'no limit') END,
         c.dbu_per_hour,
         CASE WHEN c.runtime_expected_h IS NULL THEN NULL
              ELSE CAST(c.runtime_low_h AS STRING) || ' / ' || CAST(c.runtime_expected_h AS STRING) || ' / ' || CAST(c.runtime_high_h AS STRING) || ' h per run' END,
         c.sql_warehouse_size, c.classic_equivalent, c.notes,
         CASE WHEN c.tshirt_size IS NULL THEN 'MISSING_INPUT: give a T-shirt size (XS/S/M/L/XL) or a data volume in GB' ELSE 'OK' END
  FROM p LEFT JOIN c ON true
-- @@
CREATE OR REPLACE FUNCTION __FQ__.dcp_estimate_sized(
  p_workload_type STRING, p_tshirt_size STRING DEFAULT NULL, p_data_gb DOUBLE DEFAULT NULL,
  p_runs_per_month DOUBLE DEFAULT NULL, p_concurrent_users INT DEFAULT NULL, p_active_hours_per_day DOUBLE DEFAULT NULL,
  p_quantity INT DEFAULT 1, p_planning_days INT DEFAULT 22,
  p_tier STRING DEFAULT 'ENTERPRISE', p_region STRING DEFAULT 'us-east-1')
RETURNS TABLE(scenario STRING, workload_type STRING, tshirt_size STRING, size_basis STRING, compute STRING, dbu_per_hour DOUBLE,
              estimated_dbu DOUBLE, rate DOUBLE, estimated_usd DOUBLE, rate_source STRING, rate_sku STRING, assumptions STRING, input_status STRING)
COMMENT 'Sizing-based DCP estimate: LOW/EXPECTED/HIGH/STRESS monthly DBU + USD for ONE workload described by size. Size from p_tshirt_size (XS/S/M/L/XL) or p_data_gb (batch GB per run, streaming GB per day, SQL GB queried) via dcp_sizing_profiles. EVENT_DRIVEN_BATCH: runs_per_month x runtime(low/expected/high/high) x dbu_per_hour x quantity (REQUIRES p_runs_per_month). CONTINUOUS_STREAMING: 730 h x dbu_per_hour x intensity 0.5/1.0/1.5/2.0 x quantity. SQL_REPORTING: active hours (p_active_hours_per_day x 0.5/1/1.5/2, default 0.5/1/2/4) x planning_days x warehouse DBU/hour x concurrency bands ceil(ceil(users x 0.5/1/1.5/2)/10) (REQUIRES p_concurrent_users). Rate: list price first (dcp_list_price_rates), governed fallback. input_status names any missing required input.'
RETURN
  SELECT s.scenario, z.workload_type, z.tshirt_size, z.size_basis,
         CASE WHEN z.workload_type = 'SQL_REPORTING' THEN 'SQL_PRO' ELSE 'JOBS_SERVERLESS' END AS compute,
         z.dbu_per_hour,
         round(CASE
           WHEN z.tshirt_size IS NULL THEN NULL
           WHEN z.workload_type = 'CONTINUOUS_STREAMING' THEN 730.0 * z.dbu_per_hour * s.intensity * coalesce(p_quantity,1)
           WHEN z.workload_type = 'EVENT_DRIVEN_BATCH' THEN p_runs_per_month * coalesce(p_quantity,1) * z.dbu_per_hour
                * CASE s.scenario WHEN 'LOW' THEN pr.runtime_low_h WHEN 'EXPECTED' THEN pr.runtime_expected_h ELSE pr.runtime_high_h END
           ELSE coalesce(p_active_hours_per_day * s.intensity, s.sql_active_h) * coalesce(p_planning_days,22) * z.dbu_per_hour
                * greatest(1, CAST(ceil(ceil(p_concurrent_users * s.intensity)/10.0) AS INT)) END, 2) AS estimated_dbu,
         r.rate,
         round(CASE
           WHEN z.tshirt_size IS NULL THEN NULL
           WHEN z.workload_type = 'CONTINUOUS_STREAMING' THEN 730.0 * z.dbu_per_hour * s.intensity * coalesce(p_quantity,1)
           WHEN z.workload_type = 'EVENT_DRIVEN_BATCH' THEN p_runs_per_month * coalesce(p_quantity,1) * z.dbu_per_hour
                * CASE s.scenario WHEN 'LOW' THEN pr.runtime_low_h WHEN 'EXPECTED' THEN pr.runtime_expected_h ELSE pr.runtime_high_h END
           ELSE coalesce(p_active_hours_per_day * s.intensity, s.sql_active_h) * coalesce(p_planning_days,22) * z.dbu_per_hour
                * greatest(1, CAST(ceil(ceil(p_concurrent_users * s.intensity)/10.0) AS INT)) END * r.rate, 2) AS estimated_usd,
         r.rate_source, r.rate_sku,
         CASE z.workload_type
           WHEN 'CONTINUOUS_STREAMING' THEN 'Size ' || coalesce(z.tshirt_size,'?') || ' at ' || coalesce(CAST(z.dbu_per_hour AS STRING),'?') || ' DBU/h, 730 h/month, intensity 0.5/1.0/1.5/2.0, quantity ' || CAST(coalesce(p_quantity,1) AS STRING)
           WHEN 'EVENT_DRIVEN_BATCH' THEN 'Size ' || coalesce(z.tshirt_size,'?') || ' at ' || coalesce(CAST(z.dbu_per_hour AS STRING),'?') || ' DBU/h, runtime ' || coalesce(z.runtime_range_h,'?') || ', ' || coalesce(CAST(p_runs_per_month AS STRING),'?') || ' runs/month, quantity ' || CAST(coalesce(p_quantity,1) AS STRING)
           ELSE 'Size ' || coalesce(z.tshirt_size,'?') || ' (' || coalesce(z.sql_warehouse_size,'?') || ' warehouse, ' || coalesce(CAST(z.dbu_per_hour AS STRING),'?') || ' DBU/h per cluster), ' || coalesce(CAST(p_concurrent_users AS STRING),'?') || ' concurrent users, ' || CAST(coalesce(p_planning_days,22) AS STRING) || ' days, active hours ' || coalesce(CAST(p_active_hours_per_day AS STRING) || ' h/day x 0.5/1/1.5/2', '0.5/1/2/4 h/day') END AS assumptions,
         CASE WHEN z.tshirt_size IS NULL THEN 'MISSING_INPUT: tshirt_size or data_gb'
              WHEN z.workload_type = 'EVENT_DRIVEN_BATCH' AND p_runs_per_month IS NULL THEN 'MISSING_INPUT: runs_per_month'
              WHEN z.workload_type = 'SQL_REPORTING' AND p_concurrent_users IS NULL THEN 'MISSING_INPUT: concurrent_users'
              ELSE 'OK' END AS input_status
  FROM __FQ__.dcp_size_recommend(p_workload_type, p_tshirt_size, p_data_gb) z
  LEFT JOIN __FQ__.dcp_sizing_profiles pr
    ON pr.workload_type = z.workload_type AND pr.tshirt_size = z.tshirt_size
  CROSS JOIN (VALUES ('LOW',1,0.5,0.5),('EXPECTED',2,1.0,1.0),('HIGH',3,1.5,2.0),('STRESS',4,2.0,4.0)) AS s(scenario, ord, intensity, sql_active_h)
  CROSS JOIN __FQ__.dcp_resolve_rate(
      CASE WHEN upper(p_workload_type) LIKE '%SQL%' OR upper(p_workload_type) LIKE '%REPORT%' OR upper(p_workload_type) LIKE '%BI%' THEN 'SQL_PRO' ELSE 'JOBS_SERVERLESS' END,
      coalesce(p_tier,'ENTERPRISE'), coalesce(p_region,'us-east-1')) r
  ORDER BY s.ord
"""
for stmt in [s.strip() for s in SIZING_SQL.split("\n-- @@\n") if s.strip()]:
    spark.sql(stmt.replace("__FQ__", FQ))
print("created sizing tables + functions in", FQ)
display(spark.sql(f"SELECT scenario, tshirt_size, dbu_per_hour, estimated_dbu, rate, estimated_usd, rate_source, input_status FROM {FQ}.dcp_estimate_sized('EVENT_DRIVEN_BATCH', p_data_gb => 250, p_runs_per_month => 30)"))

# COMMAND ----------

import json, uuid, requests
def nid(): return uuid.uuid4().hex
SIZING_TEXT = "SIZING GUIDANCE (estimate from workload sizes): use this when the user describes a workload by SIZE rather than by runtimes or by a named use case. Three input modes: (1) DATA_VOLUME - the user gives data amounts (GB/TB per run, GB/day ingested, GB queried; convert TB to GB x 1024); (2) TSHIRT - the user gives XS/S/M/L/XL (or extra small/small/medium/large/extra large); (3) CLUSTER_SPEC or WAREHOUSE_SPEC - the user gives instance types + worker counts, or a SQL warehouse size. ASK FIRST: before running any estimate in these modes, check the required inputs with SELECT * FROM __FQ__.dcp_sizing_questions('<mode>','<workload_type>') (workload_type EVENT_DRIVEN_BATCH | CONTINUOUS_STREAMING | SQL_REPORTING | JOBS_CLASSIC | SQL_WAREHOUSE). If ANY input with required=true is not stated in the conversation, DO NOT estimate yet: reply with a short clarification that asks for every missing required input in one message, using the question text, and mention the optional inputs with their defaults. Do not invent values for required inputs. cloud, region and tier may default (AWS, us-east-1, ENTERPRISE). Once all required inputs are known: for DATA_VOLUME and TSHIRT call dcp_estimate_sized(workload_type, p_tshirt_size => ..., p_data_gb => ..., p_runs_per_month => ..., p_concurrent_users => ..., p_active_hours_per_day => ..., p_quantity => ...) using named arguments and only the ones that apply; data_gb means GB per run for EVENT_DRIVEN_BATCH, GB per day for CONTINUOUS_STREAMING and GB queried for SQL_REPORTING. Show scenario, tshirt_size, size_basis, dbu_per_hour, estimated_dbu, rate, estimated_usd, rate_source and assumptions. To explain or recommend a size without costing it, use dcp_size_recommend(workload_type, p_tshirt_size => ..., p_data_gb => ...), which also returns the SQL warehouse size and the classic cluster equivalent. For CLUSTER_SPEC the required inputs are worker_node_type, num_workers, runs_per_day, avg_runtime_minutes and days_per_month; driver_node_type is OPTIONAL and defaults to the worker_node_type (do not ask for it), photon_enabled defaults to true. When required inputs are missing, the SQL you run must be the dcp_sizing_questions call for that mode and workload (never SELECT 1 or a literal string), and your reply must ask the user for each missing required input using its question text. Pass real SQL NULL (not the string 'NULL') for unused arguments, or omit them. For CLUSTER_SPEC use estimate_jobs_classic_cost (or estimate_all_purpose_classic_cost / estimate_dlt_classic_cost if the user says so); for WAREHOUSE_SPEC use estimate_dbsql_cost with hours_per_month = hours_per_day x days_per_month. If a result row has input_status other than OK, ask the user for the input it names instead of presenting the numbers. The T-shirt matrix is in dcp_sizing_profiles."
EXAMPLES = [
 [
  "What would a jobs cluster with 8 i3.xlarge workers cost?",
  "SELECT input_name, required, question, example FROM __FQ__.dcp_sizing_questions('CLUSTER_SPEC','JOBS_CLASSIC')"
 ],
 [
  "What inputs do you need to size a batch job from data volumes?",
  "SELECT input_name, required, question, example FROM __FQ__.dcp_sizing_questions('DATA_VOLUME','EVENT_DRIVEN_BATCH')"
 ],
 [
  "How much is a Medium SQL warehouse?",
  "SELECT input_name, required, question, example FROM __FQ__.dcp_sizing_questions('WAREHOUSE_SPEC','SQL_WAREHOUSE')"
 ],
 [
  "Cost of a Medium streaming pipeline running 24x7.",
  "SELECT scenario, tshirt_size, size_basis, dbu_per_hour, estimated_dbu, rate, estimated_usd, rate_source, assumptions FROM __FQ__.dcp_estimate_sized('CONTINUOUS_STREAMING', p_tshirt_size => 'M')"
 ],
 [
  "SQL reporting over 2 TB of data with 40 concurrent users, busy 8 hours a day.",
  "SELECT scenario, tshirt_size, size_basis, dbu_per_hour, estimated_dbu, rate, estimated_usd, rate_source, assumptions FROM __FQ__.dcp_estimate_sized('SQL_REPORTING', p_data_gb => 2048, p_concurrent_users => 40, p_active_hours_per_day => 8)"
 ],
 [
  "Estimate a batch job that processes 250 GB per run and runs 30 times a month.",
  "SELECT scenario, tshirt_size, size_basis, dbu_per_hour, estimated_dbu, rate, estimated_usd, rate_source, assumptions FROM __FQ__.dcp_estimate_sized('EVENT_DRIVEN_BATCH', p_data_gb => 250, p_runs_per_month => 30)"
 ]
]
SAMPLES = ["I have a batch job that processes 500 GB per run. What would it cost?", "Estimate a Large streaming workload."]
if not space_id:
    print("space_id blank -> functions created but Genie Space not updated.")
else:
    host = "https://" + spark.conf.get("spark.databricks.workspaceUrl")
    H = {"Authorization": f"Bearer {ctx.apiToken().get()}"}
    cur = requests.get(f"{host}/api/2.0/genie/spaces/{space_id}?include_serialized_space=true", headers=H); cur.raise_for_status(); cur = cur.json()
    space = json.loads(cur["serialized_space"]); instr = space["instructions"]
    text = SIZING_TEXT.replace("__FQ__", FQ)
    # Genie keeps one text-instruction block: edit it in place, replacing any earlier SIZING GUIDANCE text
    if instr.get("text_instructions"):
        content = instr["text_instructions"][0].setdefault("content", [])
        joined = "".join(content)
        if "SIZING GUIDANCE" in joined: joined = joined[:joined.index("SIZING GUIDANCE")].rstrip()
        instr["text_instructions"][0]["content"] = [joined + " " + text]
    else:
        instr["text_instructions"] = [{"id": nid(), "content": [text]}]
    have = {f["identifier"] for f in instr.get("sql_functions", [])}
    for fn in ("dcp_estimate_sized", "dcp_size_recommend", "dcp_sizing_questions"):
        if f"{FQ}.{fn}" not in have: instr.setdefault("sql_functions", []).append({"id": nid(), "identifier": f"{FQ}.{fn}"})
    instr["sql_functions"].sort(key=lambda x: x["id"])
    tables = space.setdefault("data_sources", {}).setdefault("tables", [])
    for t, desc in (("dcp_sizing_profiles", "Governed T-shirt sizing matrix (XS-XL) per workload type: data volume band, DBU/hour, runtime range, SQL warehouse size, classic cluster equivalent. Planning assumptions."),
                    ("dcp_sizing_inputs", "Required and optional inputs (with the question to ask) per sizing input mode and workload type.")):
        if f"{FQ}.{t}" not in {x["identifier"] for x in tables}: tables.append({"identifier": f"{FQ}.{t}", "description": [desc]})
    tables.sort(key=lambda x: x["identifier"])
    have_q = {e["question"][0] for e in instr.get("example_question_sqls", [])}
    for q, sql in EXAMPLES:
        if q not in have_q: instr.setdefault("example_question_sqls", []).append({"id": nid(), "question": [q], "sql": [sql.replace("__FQ__", FQ)]})
    instr["example_question_sqls"].sort(key=lambda x: x["id"])
    cfg = space.setdefault("config", {}); have_s = {x["question"][0] for x in cfg.get("sample_questions", [])}
    for q in SAMPLES:
        if q not in have_s: cfg.setdefault("sample_questions", []).append({"id": nid(), "question": [q]})
    cfg["sample_questions"].sort(key=lambda x: x["id"])
    r = requests.patch(f"{host}/api/2.0/genie/spaces/{space_id}", headers=H, json={"serialized_space": json.dumps(space), "warehouse_id": cur.get("warehouse_id")}); r.raise_for_status()
    print("registered sizing skill in space", space_id)
