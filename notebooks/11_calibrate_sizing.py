# Databricks notebook source
# MAGIC %md
# MAGIC # 11 · Calibrate workload sizing from job history
# MAGIC `size_cluster` / `estimate_sized_workload` size clusters from **throughput** (GB per worker core-hour) and **cluster
# MAGIC start time** per workload profile. Out of the box these are planning assumptions (`calibration_status = ASSUMED`).
# MAGIC This notebook measures them from this workspace's own successful job runs and, in `APPLY` mode, writes them to
# MAGIC `ref_sizing_profiles` (`calibration_status = CALIBRATED`). It can also load the worker limit and allowed node
# MAGIC types from a job cluster policy into `ref_sizing_policy`.
# MAGIC
# MAGIC | Measure | Source |
# MAGIC |---|---|
# MAGIC | Runs, run window, cluster start (setup) and execution time | `system.lakeflow.job_run_timeline` (successful runs) |
# MAGIC | Job name / tags, workload profile | `system.lakeflow.jobs` (tag `sizing_profile` wins, else name rules) |
# MAGIC | Worker node type, worker core-hours, CPU and memory use | `system.compute.node_timeline` + `system.compute.node_types` (job clusters) |
# MAGIC | GB written per run | Delta table history (`operationMetrics`, `job.jobRunId`) of the tables the run wrote (`system.access.table_lineage`) |
# MAGIC | DBUs and cost per run | `system.billing.usage` × `system.billing.account_prices` (or `list_prices`) |
# MAGIC
# MAGIC **Throughput per run** = GB written ÷ (average worker cores × execution hours). Per profile: LOW = 25th percentile,
# MAGIC EXPECTED = median, HIGH = 75th percentile. Startup = median setup minutes. A **hold-out backtest** calibrates on the
# MAGIC older two-thirds of runs and measures the error on the newest third.
# MAGIC
# MAGIC **Data volume.** Preferred: *bytes written to Delta* (needs `SELECT` on the written tables). Fallback when the
# MAGIC history cannot be read: *bytes received by the worker nodes* (`node_timeline.network_received_bytes`) - roughly the
# MAGIC data read from storage, but it also counts shuffle traffic, so it is a proxy. `volume_source = AUTO` calibrates each
# MAGIC profile on Delta bytes when it has `min_runs` such runs, else on the network proxy; the basis is recorded.
# MAGIC
# MAGIC **Notes.** Delta GB is compressed. If users will describe workloads by raw source size, set
# MAGIC `source_to_delta_ratio` (e.g. 4 for JSON/CSV that compresses about 4×). Serverless runs have no node data: they are
# MAGIC reported (GB per DBU) but not used for core throughput. Needs `SELECT` on the system tables above and on the
# MAGIC written tables (for `DESCRIBE HISTORY`). Run on a schedule (e.g. monthly) to keep the profiles current.

# COMMAND ----------

dbutils.widgets.text("catalog", "", "1. Catalog")
dbutils.widgets.text("schema", "lakemeter", "2. Schema")
dbutils.widgets.text("lookback_days", "90", "3. Lookback days")
dbutils.widgets.text("min_runs", "10", "4. Min runs per profile to calibrate")
dbutils.widgets.text("min_gb_per_run", "1", "5. Ignore runs writing less than (GB)")
dbutils.widgets.text("job_name_filter", "", "6. Only jobs whose name matches (regex, optional)")
dbutils.widgets.text("profile_rules", "", "7. Profile rules JSON [[regex, profile], ...] (blank = defaults)")
dbutils.widgets.text("source_to_delta_ratio", "1", "8. Source GB per Delta GB written")
dbutils.widgets.dropdown("rate_source", "ACCOUNT_PRICES", ["ACCOUNT_PRICES", "LIST_PRICES"], "9. Prices for run cost")
dbutils.widgets.text("policy_id", "", "10. Job cluster policy id -> ref_sizing_policy (optional)")
dbutils.widgets.dropdown("cloud", "AWS", ["AWS", "AZURE", "GCP"], "11. Cloud")
dbutils.widgets.text("max_tables", "500", "12. Max tables to read history from")
dbutils.widgets.dropdown("mode", "DRY_RUN", ["DRY_RUN", "APPLY"], "13. Mode")
dbutils.widgets.dropdown("volume_source", "AUTO", ["AUTO", "DELTA_ONLY", "NETWORK_ONLY"], "14. Data volume source")

import json, re
W = {k: dbutils.widgets.get(k).strip() for k in ("catalog", "schema", "lookback_days", "min_runs", "min_gb_per_run", "job_name_filter",
                                                 "profile_rules", "source_to_delta_ratio", "rate_source", "policy_id", "cloud", "max_tables", "mode", "volume_source")}
if not W["catalog"]:
    raise ValueError("Set the catalog widget.")
FQ = f"{W['catalog']}.{W['schema']}"
LOOKBACK, MIN_RUNS, MIN_GB = int(W["lookback_days"]), int(W["min_runs"]), float(W["min_gb_per_run"])
RATIO = float(W["source_to_delta_ratio"] or 1)
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
def _workspace_id():
    for get in (lambda: ctx.workspaceId().get(), lambda: json.loads(ctx.safeToJson())["attributes"]["orgId"],
                lambda: json.loads(ctx.toJson())["tags"]["orgId"], lambda: spark.conf.get("spark.databricks.clusterUsageTags.orgId")):
        try:
            v = get()
            if v: return str(v)
        except Exception:
            pass
    raise ValueError("Could not determine the workspace id.")
WS = _workspace_id()
DEFAULT_RULES = [
    [r"stream|kafka|kinesis|real.?time|continuous", "STREAMING"],
    [r"ingest|auto.?loader|autoloader|copy.?into|landing|bronze|raw", "INGESTION"],
    [r"train", "ML_TRAINING"],
    [r"scor|inference|predict|batch.?infer", "ML_INFERENCE"],
    [r"etl|silver|gold|transform|merge|aggregat|curat|enrich", "ETL"],
]
RULES = json.loads(W["profile_rules"]) if W["profile_rules"] else DEFAULT_RULES
print(f"workspace {WS} | target {FQ} | lookback {LOOKBACK} d | min runs {MIN_RUNS} | mode {W['mode']}")
spark.sql(f"SELECT profile, gb_per_core_hour_low, gb_per_core_hour_expected, gb_per_core_hour_high, startup_minutes, calibration_status FROM {FQ}.ref_sizing_profiles ORDER BY profile").display()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Successful job runs, their profile and cluster usage

# COMMAND ----------

def q(s): return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"
profile_case = "CASE " + " ".join(f"WHEN lower(nm) RLIKE {q(rx)} THEN {q(p)}" for rx, p in RULES) + " END"
name_filter = f"AND nm RLIKE {q(W['job_name_filter'])}" if W["job_name_filter"] else ""

spark.sql(f"""
CREATE OR REPLACE TEMP VIEW calib_runs AS
WITH r AS (
  SELECT workspace_id, job_id, run_id AS job_run_id, min(period_start_time) AS start_t, max(period_end_time) AS end_t,
         max_by(result_state, period_end_time) AS result_state, max(run_name) AS run_name,
         max(setup_duration_seconds) AS setup_s, max(execution_duration_seconds) AS exec_s,
         max(exists(compute, c -> c.type = 'SERVERLESS_COMPUTE')) AS serverless
  FROM system.lakeflow.job_run_timeline
  WHERE workspace_id = '{WS}' AND period_start_time >= current_timestamp() - INTERVAL {LOOKBACK} DAYS
  GROUP BY ALL),
j AS (
  SELECT job_id, name, tags FROM (
    SELECT job_id, name, tags, delete_time, row_number() OVER (PARTITION BY job_id ORDER BY change_time DESC) AS rn
    FROM system.lakeflow.jobs WHERE workspace_id = '{WS}') WHERE rn = 1),
t AS (
  -- task-level durations: run-level setup/execution are 0 for some run types (e.g. one-time submitted runs)
  SELECT job_run_id, max(setup_duration_seconds) AS t_setup_s, sum(execution_duration_seconds) AS t_exec_s,
         max(t_serverless) AS t_serverless, max(t_classic) AS t_classic
  FROM (SELECT job_run_id, run_id, max(setup_duration_seconds) AS setup_duration_seconds, max(execution_duration_seconds) AS execution_duration_seconds,
               max(exists(compute, c -> c.type = 'SERVERLESS_COMPUTE')) AS t_serverless, max(exists(compute, c -> c.type = 'CLASSIC_COMPUTE')) AS t_classic
        FROM system.lakeflow.job_task_run_timeline
        WHERE workspace_id = '{WS}' AND period_start_time >= current_timestamp() - INTERVAL {LOOKBACK} DAYS GROUP BY ALL)
  GROUP BY job_run_id),
x AS (
  SELECT r.* EXCEPT (setup_s, exec_s, serverless),
         coalesce(r.serverless, false) OR (coalesce(t.t_serverless, false) AND NOT coalesce(t.t_classic, false)) AS serverless,
         coalesce(nullif(r.setup_s, 0), t.t_setup_s, 0) AS setup_s,
         coalesce(nullif(r.exec_s, 0), nullif(t.t_exec_s, 0)) AS exec_s,
         coalesce(j.name, r.run_name) AS nm, j.tags
  FROM r LEFT JOIN j USING (job_id) LEFT JOIN t USING (job_run_id)
  WHERE r.result_state = 'SUCCEEDED')
SELECT x.*, coalesce(upper(x.tags['sizing_profile']), {profile_case}) AS profile FROM x WHERE x.exec_s > 0 {name_filter}
""")

spark.sql(f"""
CREATE OR REPLACE TEMP VIEW calib_nodes AS
WITH tc AS (
  SELECT DISTINCT t.job_run_id, cid AS cluster_id
  FROM system.lakeflow.job_task_run_timeline t LATERAL VIEW explode(t.compute_ids) e AS cid
  WHERE t.workspace_id = '{WS}' AND t.period_start_time >= current_timestamp() - INTERVAL {LOOKBACK} DAYS
    AND t.job_run_id IN (SELECT job_run_id FROM calib_runs WHERE profile IS NOT NULL AND NOT serverless)),
cl AS (
  SELECT cluster_id, cluster_source FROM (
    SELECT cluster_id, cluster_source, row_number() OVER (PARTITION BY cluster_id ORDER BY change_time DESC) AS rn
    FROM system.compute.clusters WHERE workspace_id = '{WS}') WHERE rn = 1),
nt AS (SELECT node_type, max(core_count) AS cores, max(memory_mb) / 1024.0 AS mem_gb FROM system.compute.node_types GROUP BY node_type),
nn AS (
  SELECT tc.job_run_id, tc.cluster_id, n.driver, n.node_type, n.instance_id, nt.cores, n.cpu_user_percent + n.cpu_system_percent AS cpu,
         n.mem_used_percent AS mem, n.network_received_bytes AS net,
         (unix_timestamp(least(n.end_time, r.end_t)) - unix_timestamp(greatest(n.start_time, r.start_t))) / 3600.0 AS h
  FROM tc JOIN calib_runs r ON r.job_run_id = tc.job_run_id
  JOIN system.compute.node_timeline n
    ON n.cluster_id = tc.cluster_id AND n.workspace_id = '{WS}'
   AND n.start_time >= current_timestamp() - INTERVAL {LOOKBACK + 1} DAYS AND n.start_time < r.end_t AND n.end_time > r.start_t
  JOIN nt ON nt.node_type = n.node_type)
-- workers do the work; a single-node cluster (no worker rows) runs on its driver
SELECT nn.job_run_id, max(cl.cluster_source) AS cluster_source,
       CASE WHEN count_if(NOT driver) > 0 THEN max(CASE WHEN NOT driver THEN node_type END) ELSE max(node_type) END AS worker_node_type,
       max(cores) AS cores_per_worker,
       count(DISTINCT CASE WHEN NOT driver THEN instance_id END) AS workers_seen,
       count_if(NOT driver) = 0 AS single_node,
       CASE WHEN count_if(NOT driver) > 0 THEN sum(CASE WHEN NOT driver THEN h * cores END) ELSE sum(h * cores) END AS worker_core_hours,
       CASE WHEN count_if(NOT driver) > 0 THEN avg(CASE WHEN NOT driver THEN cpu END) ELSE avg(cpu) END AS cpu_pct,
       CASE WHEN count_if(NOT driver) > 0 THEN avg(CASE WHEN NOT driver THEN mem END) ELSE avg(mem) END AS mem_pct,
       CASE WHEN count_if(NOT driver) > 0 THEN sum(CASE WHEN NOT driver THEN net END) ELSE sum(net) END / 1e9 AS gb_network
FROM nn LEFT JOIN cl ON cl.cluster_id = nn.cluster_id
GROUP BY nn.job_run_id
""")
display(spark.sql("SELECT coalesce(profile, '(no profile)') AS profile, serverless, count(*) AS successful_runs FROM calib_runs GROUP BY ALL ORDER BY 1, 2"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. GB written per run (Delta history of the tables each run wrote)

# COMMAND ----------

from pyspark.sql import functions as F
tables = [r.t for r in spark.sql(f"""
  SELECT target_table_full_name AS t, count(*) AS n FROM system.access.table_lineage
  WHERE workspace_id = '{WS}' AND entity_type = 'JOB' AND event_date >= current_date() - {LOOKBACK}
    AND target_table_full_name IS NOT NULL
    AND entity_run_id IN (SELECT job_run_id FROM calib_runs WHERE profile IS NOT NULL)
  GROUP BY 1 ORDER BY n DESC LIMIT {int(W['max_tables'])}""").collect()]
WRITE_OPS = ["WRITE", "STREAMING UPDATE", "MERGE", "COPY INTO", "CREATE TABLE AS SELECT", "REPLACE TABLE AS SELECT",
             "CREATE OR REPLACE TABLE AS SELECT", "UPDATE"]
parts, skipped = [], []
for t in tables:
    try:
        hd = spark.sql(f"DESCRIBE HISTORY {t}")
        job_fields = hd.schema["job"].dataType.fieldNames()
        run_ref = F.coalesce(F.col("job.jobRunId"), F.col("job.runId")) if "jobRunId" in job_fields else F.col("job.runId")
        h = (hd
             .where(F.col("timestamp") >= F.expr(f"current_timestamp() - INTERVAL {LOOKBACK} DAYS"))
             .where(F.col("operation").isin(WRITE_OPS) & F.col("job").isNotNull())
             .select(run_ref.alias("run_ref"), F.col("job.runId").alias("task_run_id"),
                     F.coalesce(F.col("operationMetrics")["numOutputBytes"], F.col("operationMetrics")["numTargetBytesAdded"],
                                F.col("operationMetrics")["numAddedBytes"]).cast("double").alias("bytes"),
                     F.lit(t).alias("table_name")))
        parts.append(h)
    except Exception as e:
        skipped.append((t, str(e)[:120]))
if parts:
    from functools import reduce
    reduce(lambda a, b: a.unionByName(b), parts).createOrReplaceTempView("calib_hist")
else:
    spark.createDataFrame([], "run_ref string, task_run_id string, bytes double, table_name string").createOrReplaceTempView("calib_hist")
spark.sql(f"""
CREATE OR REPLACE TEMP VIEW calib_bytes AS
WITH task_map AS (
  SELECT DISTINCT run_id AS task_run_id, job_run_id FROM system.lakeflow.job_task_run_timeline
  WHERE workspace_id = '{WS}' AND period_start_time >= current_timestamp() - INTERVAL {LOOKBACK} DAYS)
SELECT coalesce(r.job_run_id, m.job_run_id) AS job_run_id, sum(h.bytes) / 1e9 AS gb_written, count(DISTINCT h.table_name) AS tables_written
FROM calib_hist h
LEFT JOIN (SELECT job_run_id FROM calib_runs) r ON r.job_run_id = h.run_ref
LEFT JOIN task_map m ON m.task_run_id = h.task_run_id
GROUP BY 1
""")
print(f"{len(tables)} tables read, {len(skipped)} skipped (no access or not Delta)")
for t, e in skipped[:10]: print("  skipped", t, "-", e)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. DBUs and cost per run

# COMMAND ----------

price_tbl = "system.billing.account_prices" if W["rate_source"] == "ACCOUNT_PRICES" else "system.billing.list_prices"
spark.sql(f"""
CREATE OR REPLACE TEMP VIEW calib_usage AS
SELECT u.usage_metadata.job_run_id AS job_run_id, sum(u.usage_quantity) AS dbus,
       sum(u.usage_quantity * CAST(p.pricing.default AS DOUBLE)) AS dbu_cost_usd, count_if(p.sku_name IS NULL) AS unpriced_rows,
       array_join(collect_set(u.sku_name), ', ') AS skus
FROM system.billing.usage u
LEFT JOIN {price_tbl} p
  ON p.sku_name = u.sku_name AND p.cloud = u.cloud AND p.currency_code = 'USD' AND p.usage_unit = u.usage_unit
 AND u.usage_start_time >= p.price_start_time AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time)
WHERE u.workspace_id = '{WS}' AND u.usage_date >= current_date() - {LOOKBACK} AND u.usage_unit = 'DBU'
  AND u.usage_metadata.job_run_id IN (SELECT job_run_id FROM calib_runs WHERE profile IS NOT NULL)
GROUP BY 1
""")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Per-run evidence

# COMMAND ----------

ev = spark.sql(f"""
SELECT r.workspace_id, r.job_id, r.job_run_id, r.nm AS job_name, r.profile, r.start_t, r.end_t,
       CASE WHEN r.serverless THEN 'SERVERLESS' ELSE 'CLASSIC' END AS compute_type, n.cluster_source,
       n.worker_node_type, n.cores_per_worker, n.workers_seen, n.single_node,
       round(r.setup_s / 60.0, 2) AS setup_minutes, round(r.exec_s / 60.0, 2) AS exec_minutes,
       round(b.gb_written, 4) AS gb_written, round(n.gb_network, 4) AS gb_network_received, b.tables_written,
       CASE WHEN '{W["volume_source"]}' <> 'NETWORK_ONLY' AND b.gb_written * {RATIO} >= {MIN_GB} THEN 'DELTA_WRITTEN'
            WHEN '{W["volume_source"]}' <> 'DELTA_ONLY' AND n.gb_network >= {MIN_GB} THEN 'NETWORK_RECEIVED_PROXY' END AS volume_basis,
       round(CASE WHEN '{W["volume_source"]}' <> 'NETWORK_ONLY' AND b.gb_written * {RATIO} >= {MIN_GB} THEN b.gb_written * {RATIO}
                  WHEN '{W["volume_source"]}' <> 'DELTA_ONLY' AND n.gb_network >= {MIN_GB} THEN n.gb_network END, 4) AS gb_sized,
       round(n.worker_core_hours, 4) AS worker_core_hours,
       round(n.worker_core_hours / ((unix_timestamp(r.end_t) - unix_timestamp(r.start_t)) / 3600.0), 2) AS avg_worker_cores,
       round(CASE WHEN '{W["volume_source"]}' <> 'NETWORK_ONLY' AND b.gb_written * {RATIO} >= {MIN_GB} THEN b.gb_written * {RATIO}
                  WHEN '{W["volume_source"]}' <> 'DELTA_ONLY' AND n.gb_network >= {MIN_GB} THEN n.gb_network END
             / (n.worker_core_hours / ((unix_timestamp(r.end_t) - unix_timestamp(r.start_t)) / 3600.0) * r.exec_s / 3600.0), 4) AS gb_per_core_hour,
       round(n.cpu_pct, 1) AS cpu_pct, round(n.mem_pct, 1) AS mem_pct,
       round(u.dbus, 4) AS dbus, round(u.dbu_cost_usd, 4) AS dbu_cost_usd, u.unpriced_rows, u.skus,
       round(coalesce(b.gb_written * {RATIO}, n.gb_network) / u.dbus, 4) AS gb_per_dbu,
       CASE WHEN r.profile IS NULL THEN 'no profile (name or tag)'
            WHEN r.serverless THEN 'serverless (reported, not used for core throughput)'
            WHEN n.worker_core_hours IS NULL OR n.worker_core_hours <= 0 THEN 'no worker node data'
            WHEN b.gb_written IS NULL AND '{W["volume_source"]}' = 'DELTA_ONLY' THEN 'no readable Delta writes'
            WHEN coalesce(b.gb_written * {RATIO}, 0) < {MIN_GB} AND ('{W["volume_source"]}' = 'DELTA_ONLY' OR coalesce(n.gb_network, 0) < {MIN_GB}) THEN 'below min_gb_per_run'
            WHEN coalesce(n.cluster_source, 'JOB') <> 'JOB' THEN 'all-purpose cluster (shared, excluded)'
            ELSE 'USED' END AS calibration_use,
       current_timestamp() AS calibrated_at
FROM calib_runs r
LEFT JOIN calib_nodes n USING (job_run_id)
LEFT JOIN calib_bytes b USING (job_run_id)
LEFT JOIN calib_usage u USING (job_run_id)
WHERE r.profile IS NOT NULL
""")
ev.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{FQ}.sizing_calibration_runs")
display(spark.sql(f"SELECT profile, calibration_use, count(*) AS runs FROM {FQ}.sizing_calibration_runs GROUP BY ALL ORDER BY 1, 2"))
display(spark.sql(f"SELECT * FROM {FQ}.sizing_calibration_runs WHERE calibration_use = 'USED' ORDER BY start_t DESC LIMIT 50"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Calibrated values per profile, and hold-out backtest
# MAGIC Backtest: throughput from the older two-thirds of each profile's runs predicts the execution core-hours of the
# MAGIC newest third (`GB ÷ expected throughput`). MAPE = mean absolute % error; compare it with the current values.

# COMMAND ----------

summary = spark.sql(f"""
WITH u0 AS (SELECT * FROM {FQ}.sizing_calibration_runs WHERE calibration_use = 'USED' AND gb_per_core_hour > 0),
pick AS (SELECT profile, CASE WHEN count_if(volume_basis = 'DELTA_WRITTEN') >= {MIN_RUNS} OR count_if(volume_basis = 'NETWORK_RECEIVED_PROXY') = 0
                              THEN 'DELTA_WRITTEN' ELSE 'NETWORK_RECEIVED_PROXY' END AS basis FROM u0 GROUP BY profile),
u AS (SELECT u0.* FROM u0 JOIN pick ON u0.profile = pick.profile AND u0.volume_basis = pick.basis),
ranked AS (SELECT u.*, percent_rank() OVER (PARTITION BY profile ORDER BY start_t) AS pr FROM u),
cal AS (
  SELECT profile, count(*) AS runs,
         percentile_approx(gb_per_core_hour, 0.25) AS cal_low, percentile_approx(gb_per_core_hour, 0.5) AS cal_expected,
         percentile_approx(gb_per_core_hour, 0.75) AS cal_high, percentile_approx(setup_minutes, 0.5) AS cal_startup,
         percentile_approx(cpu_pct, 0.5) AS median_cpu_pct, count(DISTINCT job_id) AS jobs,
         sum(dbu_cost_usd) / sum(gb_sized) AS usd_per_gb
  FROM u GROUP BY profile),
train AS (SELECT profile, percentile_approx(gb_per_core_hour, 0.5) AS t_expected FROM ranked WHERE pr < 0.67 GROUP BY profile),
test AS (SELECT r.*, t.t_expected, p.gb_per_core_hour_expected AS cur_expected FROM ranked r JOIN train t USING (profile)
         JOIN {FQ}.ref_sizing_profiles p USING (profile) WHERE r.pr >= 0.67),
bt AS (
  SELECT profile, count(*) AS test_runs,
         avg(abs(gb_sized / t_expected - avg_worker_cores * exec_minutes / 60.0) / (avg_worker_cores * exec_minutes / 60.0)) AS mape_calibrated,
         avg(abs(gb_sized / cur_expected - avg_worker_cores * exec_minutes / 60.0) / (avg_worker_cores * exec_minutes / 60.0)) AS mape_current
  FROM test GROUP BY profile)
SELECT p.profile, p.calibration_status AS current_status, coalesce(c.runs, 0) AS usable_runs, c.jobs, pick.basis AS volume_basis,
       p.gb_per_core_hour_low AS cur_low, p.gb_per_core_hour_expected AS cur_expected, p.gb_per_core_hour_high AS cur_high, p.startup_minutes AS cur_startup,
       round(c.cal_low, 3) AS cal_low, round(c.cal_expected, 3) AS cal_expected, round(c.cal_high, 3) AS cal_high, round(c.cal_startup, 1) AS cal_startup,
       round(c.median_cpu_pct, 1) AS median_cpu_pct, round(c.usd_per_gb, 4) AS usd_per_gb,
       bt.test_runs, round(bt.mape_calibrated * 100, 1) AS backtest_mape_pct_calibrated, round(bt.mape_current * 100, 1) AS backtest_mape_pct_current,
       CASE WHEN coalesce(c.runs, 0) < {MIN_RUNS} THEN concat('keep: fewer than {MIN_RUNS} usable runs')
            WHEN c.median_cpu_pct < 30 THEN 'calibrate (note: median CPU below 30% - clusters look oversized, throughput is conservative)'
            ELSE 'calibrate' END AS action
FROM {FQ}.ref_sizing_profiles p LEFT JOIN cal c USING (profile) LEFT JOIN bt USING (profile) LEFT JOIN pick USING (profile)
ORDER BY p.profile
""")
summary.createOrReplaceTempView("calib_summary")
display(summary)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Apply (mode = APPLY)

# COMMAND ----------

hist_cols = "current_timestamp() AS run_at, '" + W["mode"] + "' AS mode, '" + WS + "' AS workspace_id, " + str(LOOKBACK) + " AS lookback_days, *"
spark.sql(f"SELECT {hist_cols} FROM calib_summary").write.mode("append").option("mergeSchema", "true").saveAsTable(f"{FQ}.sizing_calibration_history")
if W["mode"] == "APPLY":
    n = spark.sql(f"""
    MERGE INTO {FQ}.ref_sizing_profiles t
    USING (SELECT * FROM calib_summary WHERE action LIKE 'calibrate%' AND cal_low > 0 AND cal_expected > 0 AND cal_high > 0) s
    ON t.profile = s.profile
    WHEN MATCHED THEN UPDATE SET
      gb_per_core_hour_low = s.cal_low, gb_per_core_hour_expected = s.cal_expected, gb_per_core_hour_high = greatest(s.cal_high, s.cal_expected),
      startup_minutes = cast(greatest(round(s.cal_startup), 0) AS INT),
      calibration_status = 'CALIBRATED', calibration_runs = s.usable_runs, calibrated_at = current_timestamp(),
      source = concat('Calibrated from ', s.usable_runs, ' successful job runs (', s.jobs, ' jobs, last {LOOKBACK} days, workspace {WS}, ', s.volume_basis, ')'),
      calibration_notes = concat('Backtest MAPE ', coalesce(cast(s.backtest_mape_pct_calibrated AS STRING), 'n/a'), '% (current values ',
                                 coalesce(cast(s.backtest_mape_pct_current AS STRING), 'n/a'), '%); median CPU ', coalesce(cast(s.median_cpu_pct AS STRING), 'n/a'),
                                 '%; GB basis ', CASE WHEN s.volume_basis = 'DELTA_WRITTEN' THEN 'Delta bytes written x {RATIO}' ELSE 'bytes received by workers (proxy, includes shuffle)' END)
    """).first()
    print("profiles calibrated:", n["num_updated_rows"])
else:
    print("DRY_RUN: nothing written to ref_sizing_profiles. Review the table above, then re-run with mode = APPLY.")
display(spark.sql(f"SELECT profile, gb_per_core_hour_low, gb_per_core_hour_expected, gb_per_core_hour_high, startup_minutes, calibration_status, calibration_runs, source, calibration_notes FROM {FQ}.ref_sizing_profiles ORDER BY profile"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Worker limit and allowed node types from a job cluster policy (optional)

# COMMAND ----------

if W["policy_id"]:
    from databricks.sdk import WorkspaceClient
    pol = WorkspaceClient().cluster_policies.get(W["policy_id"])
    d = json.loads(pol.definition or "{}")
    lim = [x.get("maxValue", x.get("value")) for k, x in d.items() if k in ("num_workers", "autoscale.max_workers") and isinstance(x, dict)]
    lim = [int(v) for v in lim if v is not None]
    node = d.get("node_type_id", {})
    nodes = node.get("values") or ([node["value"]] if "value" in node else [])
    pattern = ("^(" + "|".join(re.escape(v) for v in nodes) + ")$") if node.get("type") in ("allowlist", "fixed") and nodes else None
    max_workers = min(lim) if lim else None
    spark.sql(f"""
      MERGE INTO {FQ}.ref_sizing_policy t
      USING (SELECT :cloud AS cloud, CAST(nullif(:mw, -1) AS INT) AS max_workers, :pat AS allowed_node_pattern, :name AS policy_name, :pid AS policy_id) s
      ON t.cloud = s.cloud
      WHEN MATCHED THEN UPDATE SET max_workers = coalesce(s.max_workers, t.max_workers), allowed_node_pattern = nullif(s.allowed_node_pattern, ''),
        policy_name = s.policy_name, policy_id = s.policy_id, source = concat('cluster policy ', s.policy_name), updated_at = current_timestamp()
      WHEN NOT MATCHED THEN INSERT (cloud, max_workers, allowed_node_pattern, policy_name, policy_id, source, updated_at)
        VALUES (s.cloud, coalesce(s.max_workers, 100), nullif(s.allowed_node_pattern, ''), s.policy_name, s.policy_id, concat('cluster policy ', s.policy_name), current_timestamp())""",
      args={"cloud": W["cloud"], "mw": max_workers if max_workers is not None else -1, "pat": pattern or "", "name": pol.name, "pid": W["policy_id"]})
    print(f"policy '{pol.name}': max workers {max_workers}, allowed nodes {nodes or 'any'}")
display(spark.sql(f"SELECT * FROM {FQ}.ref_sizing_policy ORDER BY cloud"))
