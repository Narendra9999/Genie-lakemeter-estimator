-- Workload sizing: describe a workload (type + data volume + frequency [+ batch window]) -> recommended cluster -> Lakemeter price.
-- Started from the agentops cluster_sizing tool; redesigned for planning at any volume:
--   * THROUGHPUT + WINDOW sizing: cores = GB per run / (GB per core-hour x usable window hours). Memory is sized only
--     where it really binds (working_set_factor: ML training caches data, ETL shuffles part of it; ingestion streams).
--   * The worker limit comes from ref_sizing_policy (load it from the customer's cluster policy with notebook 11),
--     not a hard-coded cap. A workload that needs more is never silently clipped: status EXCEEDS_WINDOW plus options
--     (run longer on the capped cluster, split into N parallel jobs, or serverless with no worker limit).
--   * Throughput is a LOW / EXPECTED / HIGH range. The cluster is sized on EXPECTED; LOW / HIGH give the runtime and
--     cost range. ref_sizing_profiles.calibration_status says ASSUMED (planning values) or CALIBRATED (notebook 11,
--     measured from the customer's own job runs).
--   * Node specs and DBU/hour come from ref_instance_dbu_rates; the smallest node in the allowed family that fits
--     within preferred_max_workers is chosen, then the smallest within the policy limit.
-- __FQ__ = catalog.schema.

CREATE TABLE IF NOT EXISTS __FQ__.ref_sizing_profiles (
  profile STRING, description STRING, aliases STRING, node_category STRING,
  gb_per_core_hour_low DOUBLE, gb_per_core_hour_expected DOUBLE, gb_per_core_hour_high DOUBLE,
  working_set_factor DOUBLE, volume_basis STRING, peak_factor DOUBLE,
  min_workers INT, preferred_max_workers INT, default_window_minutes INT,
  startup_minutes INT, min_runtime_minutes INT, always_on BOOLEAN, photon_default BOOLEAN, serverless_ok BOOLEAN,
  tips STRING, source STRING, calibration_status STRING, calibration_runs INT, calibrated_at TIMESTAMP, calibration_notes STRING)
COMMENT 'Workload sizing profiles used by size_cluster / estimate_sized_workload. gb_per_core_hour_low/expected/high: data processed per worker core-hour (low = pessimistic). working_set_factor: GB of cluster memory needed per GB sized (0 = streams through, memory does not bind). volume_basis PER_RUN = daily GB / runs per day; PEAK_HOUR = daily GB / 24 x peak_factor (always-on streaming). default_window_minutes: batch window when the user gives none. calibration_status ASSUMED (planning values) or CALIBRATED (measured by notebook 11 from job history).'
-- @@
MERGE INTO __FQ__.ref_sizing_profiles t USING (
  SELECT * FROM VALUES
   ('INGESTION', 'Incremental ingestion (Auto Loader / COPY INTO into Delta, scheduled or triggered)',
    'INGEST,INCREMENTAL_INGESTION,AUTO_LOADER,AUTOLOADER,COPY_INTO,LANDING,BRONZE', 'GENERAL',
    5.0, 10.0, 20.0, 0.0, 'PER_RUN', 1.0, 1, 32, 60, 5, 5, false, false, true,
    'Use Auto Loader with availableNow triggers on a schedule; enable optimized writes; serverless jobs avoid cluster start time'),
   ('ETL', 'ETL / data engineering transformations (joins, aggregations, merges)',
    'DATA_ENGINEERING,TRANSFORMATION,TRANSFORM,BATCH,BATCH_ETL,SILVER,GOLD,MERGE', 'GENERAL',
    2.0, 4.0, 8.0, 0.5, 'PER_RUN', 1.0, 2, 32, 120, 5, 10, false, false, true,
    'Use autoscaling; enable optimized writes; prefer fewer, larger workers for shuffle-heavy joins; i3 / storage-optimized nodes if shuffles spill'),
   ('STREAMING', 'Always-on streaming / real-time processing',
    'STREAM,CONTINUOUS_STREAMING,REAL_TIME,REALTIME,KAFKA,KINESIS', 'COMPUTE',
    5.0, 10.0, 20.0, 0.5, 'PEAK_HOUR', 3.0, 2, 32, 60, 0, 0, true, false, true,
    'Min 2 workers for availability; autoscale with tight bounds; if minutes of latency are acceptable, a scheduled INGESTION job is far cheaper'),
   ('ML_TRAINING', 'ML model training',
    'TRAINING,MODEL_TRAINING,TRAIN,DEEP_LEARNING', 'GPU',
    0.5, 1.0, 2.0, 4.0, 'PER_RUN', 1.0, 1, 16, 240, 8, 30, false, false, false,
    'Memory-optimized nodes for classical ML (r5 / E-series); GPU nodes (g5 / NC) for deep learning'),
   ('ML_INFERENCE', 'Batch scoring / ML inference',
    'INFERENCE,BATCH_SCORING,SCORING,BATCH_INFERENCE', 'COMPUTE',
    2.5, 5.0, 10.0, 0.0, 'PER_RUN', 1.0, 2, 32, 60, 5, 10, false, false, true,
    'Use Model Serving for real-time inference (estimate_model_serving_cost); for batch, partition the input and use mapInPandas')
  AS v(profile, description, aliases, node_category, gb_per_core_hour_low, gb_per_core_hour_expected, gb_per_core_hour_high,
       working_set_factor, volume_basis, peak_factor, min_workers, preferred_max_workers, default_window_minutes,
       startup_minutes, min_runtime_minutes, always_on, photon_default, serverless_ok, tips)) s
ON t.profile = s.profile
WHEN MATCHED AND coalesce(t.calibration_status, 'ASSUMED') = 'ASSUMED' THEN UPDATE SET
  description = s.description, aliases = s.aliases, node_category = s.node_category,
  gb_per_core_hour_low = s.gb_per_core_hour_low, gb_per_core_hour_expected = s.gb_per_core_hour_expected,
  gb_per_core_hour_high = s.gb_per_core_hour_high, working_set_factor = s.working_set_factor, volume_basis = s.volume_basis,
  peak_factor = s.peak_factor, min_workers = s.min_workers, preferred_max_workers = s.preferred_max_workers,
  default_window_minutes = s.default_window_minutes, startup_minutes = s.startup_minutes, min_runtime_minutes = s.min_runtime_minutes,
  always_on = s.always_on, photon_default = s.photon_default, serverless_ok = s.serverless_ok, tips = s.tips,
  source = 'Planning assumption', calibration_status = 'ASSUMED'
WHEN NOT MATCHED THEN INSERT (profile, description, aliases, node_category, gb_per_core_hour_low, gb_per_core_hour_expected,
  gb_per_core_hour_high, working_set_factor, volume_basis, peak_factor, min_workers, preferred_max_workers, default_window_minutes,
  startup_minutes, min_runtime_minutes, always_on, photon_default, serverless_ok, tips, source, calibration_status, calibration_runs,
  calibrated_at, calibration_notes)
VALUES (s.profile, s.description, s.aliases, s.node_category, s.gb_per_core_hour_low, s.gb_per_core_hour_expected,
  s.gb_per_core_hour_high, s.working_set_factor, s.volume_basis, s.peak_factor, s.min_workers, s.preferred_max_workers,
  s.default_window_minutes, s.startup_minutes, s.min_runtime_minutes, s.always_on, s.photon_default, s.serverless_ok, s.tips,
  'Planning assumption', 'ASSUMED', 0, NULL, NULL)
-- @@
CREATE OR REPLACE TABLE __FQ__.ref_sizing_node_families (
  cloud STRING, node_category STRING, instance_pattern STRING, min_vcpus DOUBLE, notes STRING)
COMMENT 'Node families size_cluster may pick from, per cloud and category (regex on instance_type in ref_instance_dbu_rates). Edit to match the customer''s approved instance list; ref_sizing_policy.allowed_node_pattern narrows it further.'
-- @@
INSERT INTO __FQ__.ref_sizing_node_families VALUES
 ('AWS', 'GENERAL', '^m5d\\.', 4, 'General purpose with local NVMe'),
 ('AWS', 'MEMORY', '^r5d\\.', 4, 'Memory optimized with local NVMe'),
 ('AWS', 'COMPUTE', '^c5d\\.', 4, 'Compute optimized with local NVMe'),
 ('AWS', 'STORAGE', '^i3\\.', 4, 'Storage optimized (shuffle heavy)'),
 ('AWS', 'GPU', '^g5\\.', 4, 'NVIDIA A10G'),
 ('AZURE', 'GENERAL', '^Standard_DS[0-9]+_v2$', 4, ''),
 ('AZURE', 'MEMORY', '^Standard_E[0-9]+s_v3$', 4, ''),
 ('AZURE', 'COMPUTE', '^Standard_F[0-9]+s_v2$', 4, ''),
 ('AZURE', 'STORAGE', '^Standard_L[0-9]+s_v2$', 4, ''),
 ('AZURE', 'GPU', '^Standard_NC[0-9]+s_v3$', 4, 'NVIDIA V100'),
 ('GCP', 'GENERAL', '^n2-highmem-[0-9]+$', 4, ''),
 ('GCP', 'MEMORY', '^n2-highmem-[0-9]+$', 4, ''),
 ('GCP', 'COMPUTE', '^c2-standard-[0-9]+$', 4, ''),
 ('GCP', 'STORAGE', '^n2-highmem-[0-9]+$', 4, ''),
 ('GCP', 'GPU', '^a2-highgpu-', 12, '')
-- @@
CREATE TABLE IF NOT EXISTS __FQ__.ref_sizing_policy (
  cloud STRING, max_workers INT, allowed_node_pattern STRING, policy_name STRING, policy_id STRING, source STRING, updated_at TIMESTAMP)
COMMENT 'Worker limit and allowed node types for sized clusters, one row per cloud. Load from the customer''s job cluster policy with notebook 11 (source = cluster policy). The default row is a planning guardrail, not a Databricks limit.'
-- @@
MERGE INTO __FQ__.ref_sizing_policy t
USING (SELECT * FROM VALUES ('AWS'), ('AZURE'), ('GCP') AS v(cloud)) s ON t.cloud = s.cloud
WHEN NOT MATCHED THEN INSERT (cloud, max_workers, allowed_node_pattern, policy_name, policy_id, source, updated_at)
VALUES (s.cloud, 100, NULL, 'Default guardrail', NULL, 'default guardrail - load the job cluster policy with notebook 11', current_timestamp())
-- @@
CREATE OR REPLACE FUNCTION __FQ__.size_cluster(
  p_workload STRING, p_data_gb DOUBLE, p_runs_per_day INT DEFAULT 1, p_window_minutes INT DEFAULT NULL,
  p_cloud STRING DEFAULT 'AWS', p_node_type STRING DEFAULT NULL, p_max_workers INT DEFAULT NULL, p_region STRING DEFAULT NULL)
RETURNS TABLE(workload_profile STRING, description STRING, data_gb_per_day DOUBLE, runs_per_day INT, sizing_gb DOUBLE,
  window_minutes INT, node_type STRING, driver_node_type STRING, num_workers INT, autoscale_min_workers INT, autoscale_max_workers INT,
  vcpus_per_node DOUBLE, memory_gb_per_node DOUBLE, total_worker_vcpus DOUBLE, total_worker_memory_gb DOUBLE, dbu_per_hour DOUBLE,
  required_cores DOUBLE, required_memory_gb DOUBLE, workers_needed INT, max_workers_allowed INT, max_workers_source STRING,
  limiting_factor STRING, status STRING, runtime_minutes_low INT, runtime_minutes_expected INT, runtime_minutes_high INT,
  workers_low INT, workers_high INT, parallel_jobs_needed INT,
  serverless_workers_low INT, serverless_workers_expected INT, serverless_workers_high INT,
  serverless_runtime_minutes_low INT, serverless_runtime_minutes_expected INT, serverless_runtime_minutes_high INT,
  startup_minutes INT, always_on BOOLEAN, photon_recommended BOOLEAN, serverless_ok BOOLEAN, shuffle_partitions INT,
  gb_per_core_hour_low DOUBLE, gb_per_core_hour_expected DOUBLE, gb_per_core_hour_high DOUBLE, throughput_basis STRING,
  options STRING, assumptions STRING, tips STRING)
COMMENT 'Recommend a classic job cluster for a described workload when the user does not know the cluster size. p_workload: INGESTION, ETL, STREAMING (always-on), ML_TRAINING, ML_INFERENCE (or an alias). p_data_gb: GB processed per day (ML_TRAINING: per run). p_window_minutes: time each run may take (default per profile). Sized on EXPECTED throughput so each run fits the window; runtime_minutes_low/expected/high give the range on that cluster. status FITS_WINDOW or EXCEEDS_WINDOW / EXCEEDS_POLICY (then read options). Worker limit from ref_sizing_policy. No rows = unknown workload type or no data volume.'
RETURN
  WITH i AS (
    SELECT CASE WHEN upper(trim(p_workload)) IN ('', 'NULL', 'NONE', 'N/A') THEN NULL ELSE upper(regexp_replace(trim(p_workload), '[ -]+', '_')) END AS w,
           p_data_gb AS gb, greatest(coalesce(p_runs_per_day, 1), 1) AS rpd, p_window_minutes AS win_in, p_max_workers AS maxw_in,
           coalesce(CASE WHEN upper(trim(p_cloud)) IN ('', 'NULL', 'NONE', 'N/A') THEN NULL ELSE upper(trim(p_cloud)) END, 'AWS') AS cl,
           CASE WHEN upper(trim(p_node_type)) IN ('', 'NULL', 'NONE', 'N/A') THEN NULL ELSE trim(p_node_type) END AS node,
           CASE WHEN upper(trim(p_region)) IN ('', 'NULL', 'NONE', 'N/A') THEN NULL ELSE lower(trim(p_region)) END AS rg),
  p AS (
    SELECT s.*, i.*,
           CASE WHEN s.volume_basis = 'PEAK_HOUR' THEN i.gb / 24.0 * s.peak_factor ELSE i.gb / i.rpd END AS sgb,
           CASE WHEN s.always_on THEN 60 ELSE greatest(coalesce(i.win_in, s.default_window_minutes), s.startup_minutes + 1) END AS win,
           greatest(coalesce(i.maxw_in, pol.max_workers, 100), 1) AS pmax,
           CASE WHEN i.maxw_in IS NOT NULL THEN 'given by the user' ELSE coalesce(pol.source, 'default guardrail') END AS pmax_src,
           pol.allowed_node_pattern AS allow_pat
    FROM i JOIN __FQ__.ref_sizing_profiles s ON i.w = s.profile OR array_contains(split(s.aliases, ','), i.w)
    LEFT JOIN __FQ__.ref_sizing_policy pol ON pol.cloud = i.cl
    WHERE i.gb IS NOT NULL AND i.gb > 0),
  q AS (
    SELECT p.*, greatest(win - startup_minutes, 1) / 60.0 AS eff_h, sgb * working_set_factor AS req_mem FROM p),
  c AS (
    SELECT q.*, d.instance_type, d.vcpus, d.memory_gb, d.dbu_rate,
           q.sgb / (q.gb_per_core_hour_expected * q.eff_h) AS req_cores,
           cast(ceil(q.sgb / (q.gb_per_core_hour_expected * q.eff_h) / d.vcpus) AS INT) AS n_cpu,
           cast(ceil(q.req_mem / d.memory_gb) AS INT) AS n_mem
    FROM q JOIN __FQ__.ref_instance_dbu_rates d ON upper(d.cloud) = q.cl AND d.is_active
    WHERE (q.node IS NOT NULL AND lower(d.instance_type) = lower(q.node))
       OR (q.node IS NULL
           AND EXISTS (SELECT 1 FROM __FQ__.ref_sizing_node_families f
                       WHERE f.cloud = q.cl AND f.node_category = q.node_category
                         AND d.instance_type RLIKE f.instance_pattern AND d.vcpus >= f.min_vcpus)
           AND (q.allow_pat IS NULL OR d.instance_type RLIKE q.allow_pat)
           AND (q.rg IS NULL OR EXISTS (SELECT 1 FROM __FQ__.ref_vm_costs v
                                        WHERE upper(v.cloud) = q.cl AND lower(v.region) = q.rg
                                          AND v.instance_type = d.instance_type AND lower(v.pricing_tier) = 'on_demand')))),
  n AS (SELECT c.*, greatest(min_workers, n_cpu, n_mem) AS need FROM c),
  best AS (
    SELECT n.*, least(need, pmax) AS wk FROM n
    ORDER BY CASE WHEN need <= least(preferred_max_workers, pmax) THEN 0 WHEN need <= pmax THEN 1 ELSE 2 END,
             CASE WHEN need <= pmax THEN vcpus ELSE -vcpus END, dbu_rate
    LIMIT 1),
  r AS (
    SELECT b.*,
           CASE WHEN always_on THEN 1440 ELSE cast(ceil(startup_minutes + greatest(min_runtime_minutes, 60.0 * sgb / (gb_per_core_hour_high * wk * vcpus))) AS INT) END AS rt_l,
           CASE WHEN always_on THEN 1440 ELSE cast(ceil(startup_minutes + greatest(min_runtime_minutes, 60.0 * sgb / (gb_per_core_hour_expected * wk * vcpus))) AS INT) END AS rt_e,
           CASE WHEN always_on THEN 1440 ELSE cast(ceil(startup_minutes + greatest(min_runtime_minutes, 60.0 * sgb / (gb_per_core_hour_low * wk * vcpus))) AS INT) END AS rt_h,
           CASE WHEN always_on THEN least(pmax, greatest(min_workers, cast(ceil(sgb / gb_per_core_hour_high / vcpus) AS INT), n_mem)) ELSE wk END AS wk_l,
           CASE WHEN always_on THEN least(pmax, greatest(min_workers, cast(ceil(sgb / gb_per_core_hour_low / vcpus) AS INT), n_mem)) ELSE wk END AS wk_h,
           CASE WHEN always_on THEN greatest(min_workers, cast(ceil(sgb / gb_per_core_hour_high / vcpus) AS INT), n_mem) ELSE need END AS sw_l,
           CASE WHEN always_on THEN greatest(min_workers, cast(ceil(sgb / gb_per_core_hour_low / vcpus) AS INT), n_mem) ELSE need END AS sw_h
    FROM best b)
  SELECT profile, description, gb, rpd, round(sgb, 3), CASE WHEN always_on THEN NULL ELSE win END,
         instance_type, instance_type, wk, least(min_workers, wk), least(pmax, greatest(wk, wk_h)),
         vcpus, memory_gb, wk * vcpus, wk * memory_gb, round(dbu_rate * (wk + 1), 4),
         round(req_cores, 2), round(req_mem, 2), need, pmax, pmax_src,
         CASE WHEN need > pmax THEN 'POLICY_CAP'
              WHEN need = min_workers AND n_cpu < min_workers AND n_mem < min_workers THEN 'MIN_WORKERS'
              WHEN n_mem > n_cpu THEN 'MEMORY' ELSE 'CORES' END,
         CASE WHEN need > pmax THEN CASE WHEN always_on THEN 'EXCEEDS_POLICY' ELSE 'EXCEEDS_WINDOW' END
              WHEN NOT always_on AND rt_e > win THEN 'EXCEEDS_WINDOW' ELSE 'FITS_WINDOW' END,
         rt_l, rt_e, rt_h, wk_l, wk_h,
         cast(ceil(need / pmax) AS INT),
         sw_l, need, sw_h,
         CASE WHEN always_on THEN 1440 ELSE cast(ceil(greatest(min_runtime_minutes, 60.0 * sgb / (gb_per_core_hour_high * sw_l * vcpus))) AS INT) END,
         CASE WHEN always_on THEN 1440 ELSE cast(ceil(greatest(min_runtime_minutes, 60.0 * sgb / (gb_per_core_hour_expected * need * vcpus))) AS INT) END,
         CASE WHEN always_on THEN 1440 ELSE cast(ceil(greatest(min_runtime_minutes, 60.0 * sgb / (gb_per_core_hour_low * sw_h * vcpus))) AS INT) END,
         startup_minutes, always_on, photon_default, serverless_ok,
         cast(greatest(wk * vcpus * 2, 200) AS INT),
         gb_per_core_hour_low, gb_per_core_hour_expected, gb_per_core_hour_high,
         CASE WHEN calibration_status = 'CALIBRATED'
              THEN concat('CALIBRATED from ', calibration_runs, ' job runs (', date_format(calibrated_at, 'yyyy-MM-dd'), ')')
              ELSE 'ASSUMED planning values (not yet calibrated with job history)' END,
         CASE WHEN need > pmax AND NOT always_on THEN concat(
                'Needs ', need, ' x ', instance_type, ' workers to finish in ', win, ' min but the worker limit is ', pmax, ' (', pmax_src, '). Options: ',
                '(1) run on the ', wk, '-worker cluster, about ', rt_e, ' min per run; ',
                '(2) split into ', cast(ceil(need / pmax) AS INT), ' parallel jobs of up to ', pmax, ' workers each; ',
                '(3) serverless jobs (no worker limit)', CASE WHEN serverless_ok THEN '' ELSE ' - not suited to this workload type' END, '; ',
                '(4) ask for a higher worker limit.')
              WHEN need > pmax THEN concat('Needs ', need, ' workers to keep up but the worker limit is ', pmax, ' (', pmax_src, '): split the stream or raise the limit.')
              WHEN NOT always_on AND rt_e > win THEN concat('The minimum runtime (', rt_e, ' min incl. ', startup_minutes, ' min cluster start) is longer than the ', win, '-minute window.')
              ELSE NULL END,
         concat('Planning estimate - validate with a test run. Sized on ', round(sgb, 2), ' GB',
                CASE WHEN volume_basis = 'PEAK_HOUR' THEN concat(' in the peak hour (daily GB / 24 x ', peak_factor, ')') ELSE ' per run' END,
                CASE WHEN always_on THEN ', always on 24 x 7' ELSE concat(' to finish within ', win, ' min') END,
                '; throughput ', gb_per_core_hour_low, ' / ', gb_per_core_hour_expected, ' / ', gb_per_core_hour_high, ' GB per core-hour (low / expected / high)',
                CASE WHEN working_set_factor > 0 THEN concat('; memory ', working_set_factor, ' GB per GB sized') ELSE '; memory not a constraint (data streams through)' END,
                CASE WHEN always_on THEN '' ELSE concat('; ', startup_minutes, ' min cluster start, min ', min_runtime_minutes, ' min runtime') END,
                '; worker limit ', pmax, ' (', pmax_src, ')',
                CASE WHEN node IS NOT NULL THEN concat('; node type ', instance_type, ' given by the user') ELSE concat('; smallest ', lower(node_category), ' node that fits') END,
                '. Throughput ', CASE WHEN calibration_status = 'CALIBRATED' THEN concat('calibrated from ', calibration_runs, ' job runs') ELSE 'values are assumed' END, '.'),
         tips
  FROM r
-- @@
CREATE OR REPLACE FUNCTION __FQ__.estimate_sized_workload(
  p_workload STRING, p_data_gb DOUBLE, p_runs_per_day INT DEFAULT 1, p_days_per_month INT DEFAULT 30,
  p_window_minutes INT DEFAULT NULL, p_cloud STRING DEFAULT 'AWS', p_region STRING DEFAULT 'us-east-1', p_tier STRING DEFAULT 'ENTERPRISE',
  p_node_type STRING DEFAULT NULL, p_photon_enabled BOOLEAN DEFAULT NULL, p_max_workers INT DEFAULT NULL)
RETURNS TABLE(compute_option STRING, scenario STRING, workload_profile STRING, node_type STRING, num_workers INT, photon_enabled BOOLEAN,
  runtime_minutes_per_run INT, runs_per_day INT, days_per_month INT, hours_per_month DOUBLE,
  dbu_per_hour DOUBLE, dbu_per_month DOUBLE, dbu_price DOUBLE, dbu_cost_per_month DOUBLE, vm_cost_per_month DOUBLE,
  total_cost_per_month DOUBLE, status STRING, window_minutes INT, throughput_basis STRING, options STRING, price_basis STRING, sizing_basis STRING)
COMMENT 'Size a cluster for a described workload (size_cluster) and price it with the Lakemeter functions (DBU price from ref_dbu_price_card; NULL cost = PENDING price). Rows: compute_option JOBS_CLASSIC (DBU + VM on the recommended cluster) and JOBS_SERVERLESS (no worker limit, no cluster start), each for scenario LOW / EXPECTED / HIGH (throughput range). Use when the user describes data volume and frequency but no cluster. p_data_gb = GB per day. Weekly: p_runs_per_day 1, p_days_per_month 4. p_window_minutes = time each run may take.'
RETURN
  WITH a AS (
    SELECT coalesce(CASE WHEN upper(trim(p_cloud)) IN ('', 'NULL', 'NONE', 'N/A') THEN NULL ELSE upper(trim(p_cloud)) END, 'AWS') AS cl,
           coalesce(CASE WHEN upper(trim(p_region)) IN ('', 'NULL', 'NONE', 'N/A') THEN NULL ELSE lower(trim(p_region)) END, 'us-east-1') AS rg,
           coalesce(CASE WHEN upper(trim(p_tier)) IN ('', 'NULL', 'NONE', 'N/A') THEN NULL ELSE upper(trim(p_tier)) END, 'ENTERPRISE') AS tr,
           p_workload AS w, p_data_gb AS gb, p_runs_per_day AS rpd_in, p_window_minutes AS win_in, p_node_type AS node_in,
           p_max_workers AS maxw_in, p_photon_enabled AS ph_in, coalesce(p_days_per_month, 30) AS dpm),
  s AS (
    SELECT z.*, a.cl, a.rg, a.tr, a.dpm, coalesce(a.ph_in, z.photon_recommended) AS ph,
           CASE WHEN z.always_on THEN 1 ELSE z.runs_per_day END AS rpd
    FROM a, LATERAL __FQ__.size_cluster(a.w, a.gb, a.rpd_in, a.win_in, a.cl, a.node_in, a.maxw_in, a.rg) z),
  k AS (
    SELECT s.*, x.scenario,
           CASE x.scenario WHEN 'LOW' THEN s.workers_low WHEN 'HIGH' THEN s.workers_high ELSE s.num_workers END AS cw,
           CASE x.scenario WHEN 'LOW' THEN s.runtime_minutes_low WHEN 'HIGH' THEN s.runtime_minutes_high ELSE s.runtime_minutes_expected END AS crt,
           CASE x.scenario WHEN 'LOW' THEN s.serverless_workers_low WHEN 'HIGH' THEN s.serverless_workers_high ELSE s.serverless_workers_expected END AS sw,
           CASE x.scenario WHEN 'LOW' THEN s.serverless_runtime_minutes_low WHEN 'HIGH' THEN s.serverless_runtime_minutes_high ELSE s.serverless_runtime_minutes_expected END AS srt
    FROM s, (SELECT * FROM VALUES ('LOW'), ('EXPECTED'), ('HIGH') AS v(scenario)) x)
  SELECT 'JOBS_CLASSIC', k.scenario, k.workload_profile, k.node_type, k.cw, k.ph, k.crt, k.rpd, k.dpm, e.hours_per_month,
         e.dbu_per_hour, e.dbu_per_month, e.dbu_price, e.dbu_cost_per_month, e.vm_cost_per_month, e.total_cost_per_month,
         CASE WHEN k.always_on THEN k.status WHEN k.crt <= k.window_minutes THEN 'FITS_WINDOW' ELSE 'EXCEEDS_WINDOW' END,
         k.window_minutes, k.throughput_basis, k.options,
         concat('DBU price: ', __FQ__.get_dbu_price_status(k.cl, k.rg, k.tr, __FQ__.get_product_type_for_pricing('JOBS', false, k.ph, NULL, NULL, NULL)),
                '; VM: cloud on-demand list price (ref_vm_costs)'), k.assumptions
  FROM k, LATERAL __FQ__.estimate_jobs_classic_cost(k.cl, k.rg, k.tr, k.node_type, k.node_type, k.cw, k.ph, k.rpd, k.crt, k.dpm) e
  UNION ALL
  SELECT 'JOBS_SERVERLESS', k.scenario, k.workload_profile, concat('serverless (sized as ', k.sw, ' x ', k.node_type, ')'), k.sw, true,
         k.srt, k.rpd, k.dpm, e.hours_per_month,
         e.dbu_per_hour, e.dbu_per_month, e.dbu_price, e.dbu_cost_per_month, e.vm_cost_per_month, e.total_cost_per_month,
         CASE WHEN k.always_on OR k.srt <= k.window_minutes THEN 'FITS_WINDOW' ELSE 'EXCEEDS_WINDOW' END, k.window_minutes, k.throughput_basis, NULL,
         concat('DBU price: ', __FQ__.get_dbu_price_status(k.cl, k.rg, k.tr, __FQ__.get_product_type_for_pricing('JOBS', true, true, NULL, NULL, NULL))), concat(k.assumptions, ' Serverless: sized to fit the window with no worker limit; no cluster start billed.')
  FROM k, LATERAL __FQ__.estimate_serverless_compute_cost(k.cl, k.rg, k.tr, 'JOBS', k.node_type, k.node_type, k.sw, 'standard', k.rpd, k.srt, k.dpm) e
  WHERE k.serverless_ok
