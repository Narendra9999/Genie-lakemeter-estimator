"""Generate sql/06_dcp_calculator.sql and calculator/rate_registry_seed.json.

The Unity Catalog Python function dcp_calc embeds notebooks/dcp_estimator.py VERBATIM (plus the thin
calculator/dcp_adapter.py), so the notebook, the SQL API and Genie run the same calculator.
To upgrade the calculator: replace notebooks/dcp_estimator.py, re-run this script, calculator/build_notebook.py and
calculator/build_setup.py, then run the setup_agent bundle job (or setup_genie_dcp_agent.py).
Statements are separated by '-- @@'; __FQ__ becomes <catalog>.<schema> at deploy time.
"""
import dataclasses, hashlib, importlib.util, os
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC_PATH = os.path.join(ROOT, "notebooks", "dcp_estimator.py")
src = open(SRC_PATH).read()
adapter = open(os.path.join(ROOT, "calculator", "dcp_adapter.py")).read()
for name, text in (("dcp_estimator.py", src), ("dcp_adapter.py", adapter)):
    assert "'''" not in text and "$$" not in text, f"{name} cannot be embedded as a raw string"
import sys
spec = importlib.util.spec_from_file_location("dcp_estimator", SRC_PATH); m = importlib.util.module_from_spec(spec)
sys.modules["dcp_estimator"] = m; spec.loader.exec_module(m)
VERSION = m.CALCULATOR_VERSION
KEY = hashlib.sha256((src + adapter).encode()).hexdigest()[:10]

N = lambda p: f"CASE WHEN upper(trim({p})) IN ('', 'NULL', 'NONE', 'N/A') THEN NULL ELSE {p} END"   # Genie sometimes passes the text 'NULL'
# comma list of area names -> array of (area, state) entries for the request's _areas map
AREA_LIST = lambda p, state: (f"transform(filter(split(lower(coalesce({N(p)}, '')), '[ ,;]+'), x -> x <> ''), "
                              f"x -> struct(x AS key, '{state}' AS value))")
TOTALS = ("array<struct<scenario:string,label:string,recurring_monthly_usd:double,core_monthly_usd:double,annual_usd:double,"
          "one_time_usd:double,first_year_usd:double,databricks_dbu:double,databricks_usd:double,aws_usd:double,pending_items:int,status:string>>")
LINES = ("array<struct<scenario:string,component_id:string,engine:string,description:string,quantity:double,unit:string,rate:double,"
         "rate_status:string,rate_source:string,cost_usd:double,recurrence:string,treatment:string,evidence:string,basis:string,"
         "formula:string,warnings:string>>")
DECISIONS = ("array<struct<component_id:string,selected:string,alternative:string,score:int,confidence:string,status:string,"
             "sku:string,eligible:string,streaming_decision:string>>")
RESULT = (f"struct<status:string,errors:array<string>,header:struct<calculator_version:string,use_case_id:string,use_case_name:string,"
          f"generated_at:string,rate_card_source:string,rate_card_as_of:string>,totals:{TOTALS},line_items:{LINES},"
          f"compute_decisions:{DECISIONS},confidence:struct<rating:string,reference_scenario:string,planning_assumption_share:double,"
          f"range_derived_share:double,reason:string>,pending:array<struct<component_id:string,reason:string>>,"
          f"assumptions:array<string>,warnings:array<string>,scope:struct<included:array<string>,excluded:array<string>,rates_used:array<string>>,"
          f"areas:array<struct<area:string,state:string>>,next_step:string,notes:array<string>,summary:string,disclaimer:string>")
ARGS = "p_use_case STRING DEFAULT NULL, p_request STRING DEFAULT NULL, p_overrides STRING DEFAULT NULL"
CALL = "__FQ__.dcp_calc_run(p_use_case, p_request, p_overrides)"
WHO = ("Input: EITHER p_use_case (a loaded use case id or business name, case-insensitive) OR p_request (built with dcp_request(...)). "
       "Optional p_overrides = JSON what-if map, e.g. ''{\"migration_gb\": 8000, \"retained_storage_tb\": 8}'' for a preset use case "
       "or dotted paths such as ''{\"transfer.daily_gb_high\": 12}''.")
# Shared by dcp_calc and dcp_check: loads the calculator and adapter once per Python worker.
LOADER = f"""import sys, types, textwrap
KEY = "{KEY}"
# Unity Catalog indents the function body, including these strings; dedent restores the files exactly.
DCP_ESTIMATOR_SOURCE = r'''
{src}'''
DCP_ADAPTER_SOURCE = r'''
{adapter}'''
if "dcp_adapter_" + KEY not in sys.modules:
    for name, code in (("dcp_estimator", DCP_ESTIMATOR_SOURCE), ("dcp_adapter", DCP_ADAPTER_SOURCE)):
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        exec(compile(textwrap.dedent(code), name + ".py", "exec"), mod.__dict__)
        sys.modules[name + "_" + KEY] = mod"""
# Resolve the request: ad-hoc p_request, else a named use case by exact id/name or a UNIQUE prefix ("SEND" -> SEND_US01).
RESOLVE = f"""p AS (SELECT {N('p_use_case')} AS uc, {N('p_request')} AS rq, {N('p_overrides')} AS ov),
  req AS (
    SELECT coalesce(get_json_object(p.rq, '$.use_case_id'), 'ADHOC') AS use_case_id, p.rq AS r, p.ov, false AS named FROM p WHERE p.rq IS NOT NULL
    UNION ALL
    SELECT use_case_id, request_json, ov, true FROM (
      SELECT q.use_case_id, q.request_json, p.ov, m.rnk, count(*) OVER (PARTITION BY m.rnk) AS n_at_rank, min(m.rnk) OVER () AS best
      FROM __FQ__.dcp_requests q JOIN p ON p.rq IS NULL
      LATERAL VIEW explode(array(
        CASE WHEN upper(trim(q.use_case_id)) = upper(trim(p.uc)) OR upper(trim(q.use_case_name)) = upper(trim(p.uc)) THEN 1
             WHEN startswith(upper(q.use_case_id), upper(trim(p.uc))) OR startswith(upper(q.use_case_name), upper(trim(p.uc))) THEN 2 END)) m AS rnk
      WHERE m.rnk IS NOT NULL)
    WHERE rnk = best AND n_at_rank = 1)"""   # ambiguous prefix -> no match

stmts = []
stmts.append("""CREATE OR REPLACE TABLE __FQ__.dcp_rate_card (
  provider STRING, service STRING, sku STRING, unit STRING, region STRING, rate DOUBLE, status STRING, source STRING,
  effective_start STRING, effective_end STRING, observed_rate DOUBLE, notes STRING, billing_sku STRING, refreshed_at TIMESTAMP)
COMMENT 'The only rates the DCP calculator uses (one row per calculator RateRecord: provider, sku, unit, region). Databricks DBU rows are loaded from system.billing.account_prices (status GOVERNED) or, in test workspaces, system.billing.list_prices (status REFERENCE_ONLY) by notebook 09; AWS rows are the calculator reference registry. A missing or PENDING rate makes the line PENDING - the agent never substitutes a rate.'""")
stmts.append("""CREATE TABLE IF NOT EXISTS __FQ__.dcp_requests (
  use_case_id STRING, use_case_name STRING, request_json STRING, source STRING, loaded_at TIMESTAMP)
COMMENT 'One DCP calculator request per named use case: either {"preset": "<name>"} for a calculator preset (e.g. SEND_US01) or a full EstimateRequest JSON built from the intake. dcp_calc_* functions look use cases up here by id or business name.'""")
stmts.append(f"""CREATE OR REPLACE FUNCTION __FQ__.dcp_calc(request STRING, rates STRING, overrides STRING, named BOOLEAN)
RETURNS STRING
LANGUAGE PYTHON
COMMENT 'DCP Smart Estimation Calculator v{VERSION} (notebooks/dcp_estimator.py, embedded verbatim) as a Unity Catalog function. Runs the input check first (named = true: named use case, validate only; false: ad-hoc or pasted JSON, every required input; NULL: no check) and returns status NEEDS_INPUT with one question per missing or invalid input instead of pricing. Otherwise builds the request (preset or JSON, plus what-if overrides), prices it only with the supplied rate card, and returns the full result JSON or status REJECTED with the calculator validation errors. Internal: call through dcp_calc_totals / dcp_calc_line_items / dcp_calc_details.'
AS $$
{LOADER}
return sys.modules["dcp_adapter_" + KEY].run(sys.modules["dcp_estimator_" + KEY], request, rates, overrides, named)
$$""")
stmts.append(f"""CREATE OR REPLACE FUNCTION __FQ__.dcp_check(request STRING, overrides STRING, named BOOLEAN)
RETURNS STRING
LANGUAGE PYTHON
COMMENT 'DCP input check (calculator v{VERSION}, calculator/dcp_adapter.py check): lists every required input that is missing (no calculator default, so the cost would be PENDING), every value of the wrong type or out of range, and anything the calculator would reject. Returns status READY or NEEDS_INPUT. Internal: call through dcp_check_inputs.'
AS $$
{LOADER}
return sys.modules["dcp_adapter_" + KEY].run_check(sys.modules["dcp_estimator_" + KEY], request, overrides, named)
$$""")
stmts.append(f"""CREATE OR REPLACE FUNCTION __FQ__.dcp_request(
  p_workload_type STRING DEFAULT NULL, p_quantity INT DEFAULT NULL,
  p_runtime_hours_low DOUBLE DEFAULT NULL, p_runtime_hours_expected DOUBLE DEFAULT NULL, p_runtime_hours_high DOUBLE DEFAULT NULL,
  p_runs_per_month DOUBLE DEFAULT NULL, p_dbu_per_hour DOUBLE DEFAULT NULL,
  p_latency_requirement STRING DEFAULT NULL, p_streaming_mode STRING DEFAULT NULL,
  p_requires_gpu BOOLEAN DEFAULT NULL, p_requires_custom_runtime BOOLEAN DEFAULT NULL, p_requires_custom_networking BOOLEAN DEFAULT NULL,
  p_concurrent_users INT DEFAULT NULL, p_total_users INT DEFAULT NULL, p_working_days_per_month INT DEFAULT NULL,
  p_daily_gb_low DOUBLE DEFAULT NULL, p_daily_gb_expected DOUBLE DEFAULT NULL, p_daily_gb_high DOUBLE DEFAULT NULL,
  p_transfer_days_per_month INT DEFAULT NULL, p_initial_transfer_gb DOUBLE DEFAULT NULL,
  p_retained_tb DOUBLE DEFAULT NULL, p_files_per_day DOUBLE DEFAULT NULL, p_encrypted_files_per_day DOUBLE DEFAULT NULL,
  p_kms_keys DOUBLE DEFAULT NULL, p_s3_read_requests_per_file DOUBLE DEFAULT NULL,
  p_model STRING DEFAULT NULL, p_calls_per_day DOUBLE DEFAULT NULL,
  p_input_tokens_low DOUBLE DEFAULT NULL, p_input_tokens_expected DOUBLE DEFAULT NULL, p_input_tokens_high DOUBLE DEFAULT NULL,
  p_output_tokens_low DOUBLE DEFAULT NULL, p_output_tokens_expected DOUBLE DEFAULT NULL, p_output_tokens_high DOUBLE DEFAULT NULL,
  p_region STRING DEFAULT NULL, p_use_case_id STRING DEFAULT 'ADHOC', p_use_case_name STRING DEFAULT 'Ad-hoc estimate',
  p_scenarios STRING DEFAULT NULL, p_component_id STRING DEFAULT NULL, p_pattern STRING DEFAULT NULL,
  p_storage_days_per_month INT DEFAULT NULL, p_s3_write_requests_per_file DOUBLE DEFAULT NULL,
  p_kms_requests_per_encrypted_file DOUBLE DEFAULT NULL, p_serving_mode STRING DEFAULT NULL, p_model_days_per_month INT DEFAULT NULL,
  p_included_areas STRING DEFAULT NULL, p_excluded_areas STRING DEFAULT NULL, p_unknown_areas STRING DEFAULT NULL,
  p_not_applicable_areas STRING DEFAULT NULL)
RETURNS STRING
COMMENT 'Build a DCP calculator request (EstimateRequest JSON) for an ad-hoc question with named arguments. Pass ONLY what the user stated - blanks stay PENDING, never zero. One stated runtime per run applies to every scenario. Sections: processing (p_workload_type CONTINUOUS_STREAMING | EVENT_DRIVEN_BATCH | SCHEDULED_BATCH | ML_TRAINING | BATCH_SCORING, runtimes, runs, DBU/hour, latency, GPU/custom flags), SQL (p_concurrent_users...), transfer (p_daily_gb_*, p_initial_transfer_gb), storage (p_retained_tb, files, KMS, reads per file), model API (p_model, p_calls_per_day, tokens per call). p_scenarios: comma list, default LOW,EXPECTED,HIGH - add STRESS only when the user asks for it. p_component_id / p_use_case_id / p_use_case_name: the user''s own identifiers. AREAS (every area must be confirmed before an estimate is final): an area with inputs is included; give the state of each other area as a comma list of area names in p_excluded_areas, p_unknown_areas, p_not_applicable_areas (or p_included_areas, then its inputs are required). Area names: processing, sql, transfer, storage, model_endpoints, one_time_model_jobs. Use as: SELECT * FROM dcp_calc_totals(p_request => dcp_request(p_workload_type => ''SCHEDULED_BATCH'', p_runtime_hours_expected => 2, p_runs_per_month => 30, p_not_applicable_areas => ''sql,transfer,storage,model_endpoints,one_time_model_jobs''))'
RETURN to_json(named_struct(
  'use_case_id', p_use_case_id, 'use_case_name', p_use_case_name,
  'scenarios', transform(split(coalesce({N('p_scenarios')}, 'LOW,EXPECTED,HIGH'), ','), x -> upper(trim(x))),
  'model_days_per_month', p_model_days_per_month,
  '_areas', map_from_entries(concat({AREA_LIST('p_included_areas', 'INCLUDED')}, {AREA_LIST('p_excluded_areas', 'EXCLUDED')},
                                    {AREA_LIST('p_unknown_areas', 'UNKNOWN')}, {AREA_LIST('p_not_applicable_areas', 'NOT_APPLICABLE')})),
  'region', upper(replace({N('p_region')}, '-', '_')),
  'processing', CASE WHEN {N('p_workload_type')} IS NOT NULL THEN array(named_struct(
      'component_id', coalesce({N('p_component_id')}, 'ADHOC-C01'), 'name', initcap(replace(lower(p_workload_type), '_', ' ')), 'workload_type', upper(p_workload_type),
      'pattern', upper({N('p_pattern')}),
      'quantity', p_quantity,
      'runtime_hours_low', coalesce(p_runtime_hours_low, p_runtime_hours_expected),
      'runtime_hours_expected', coalesce(p_runtime_hours_expected, p_runtime_hours_low, p_runtime_hours_high),
      'runtime_hours_high', coalesce(p_runtime_hours_high, p_runtime_hours_expected),
      'runs_per_month', p_runs_per_month,
      'dbu_per_hour', CASE WHEN p_dbu_per_hour IS NOT NULL THEN map('LOW', p_dbu_per_hour, 'EXPECTED', p_dbu_per_hour, 'HIGH', p_dbu_per_hour, 'STRESS', p_dbu_per_hour) END,
      'dbu_per_hour_basis', CASE WHEN p_dbu_per_hour IS NOT NULL THEN 'TEAM_SUPPLIED_DBU_PER_HOUR' END,
      'latency_requirement', upper({N('p_latency_requirement')}), 'streaming_mode', upper({N('p_streaming_mode')}),
      'requires_gpu', p_requires_gpu, 'requires_custom_runtime', p_requires_custom_runtime,
      'requires_custom_networking', p_requires_custom_networking)) END,
  'sql', CASE WHEN p_concurrent_users IS NOT NULL THEN named_struct(
      'component_id', 'ADHOC-SQL', 'name', 'SQL reporting', 'concurrent_users', p_concurrent_users,
      'total_users', p_total_users, 'working_days_per_month', p_working_days_per_month) END,
  'transfer', CASE WHEN coalesce(p_daily_gb_low, p_daily_gb_expected, p_daily_gb_high, p_initial_transfer_gb) IS NOT NULL THEN named_struct(
      'daily_gb_low', coalesce(p_daily_gb_low, p_daily_gb_expected), 'daily_gb_expected', p_daily_gb_expected,
      'daily_gb_high', coalesce(p_daily_gb_high, p_daily_gb_expected),
      'transfer_days_per_month', p_transfer_days_per_month, 'initial_transfer_gb', p_initial_transfer_gb) END,
  'storage', CASE WHEN coalesce(p_retained_tb, p_files_per_day, p_encrypted_files_per_day, p_kms_keys, p_s3_read_requests_per_file) IS NOT NULL THEN named_struct(
      'retained_tb', p_retained_tb, 'files_per_day', p_files_per_day, 'encrypted_files_per_day', p_encrypted_files_per_day,
      'kms_keys', p_kms_keys, 's3_read_requests_per_file', p_s3_read_requests_per_file, 'days_per_month', p_storage_days_per_month,
      's3_write_requests_per_file', p_s3_write_requests_per_file, 'kms_requests_per_encrypted_file', p_kms_requests_per_encrypted_file) END,
  'model_endpoints', CASE WHEN {N('p_model')} IS NOT NULL THEN array(named_struct(
      'component_id', 'ADHOC-MODEL-01', 'model', p_model, 'role', 'Model API', 'calls_per_day', p_calls_per_day, 'serving_mode', upper({N('p_serving_mode')}),
      'input_tokens_low', p_input_tokens_low, 'input_tokens_expected', p_input_tokens_expected, 'input_tokens_high', p_input_tokens_high,
      'output_tokens_low', p_output_tokens_low, 'output_tokens_expected', p_output_tokens_expected, 'output_tokens_high', p_output_tokens_high)) END))""")
stmts.append(f"""CREATE OR REPLACE FUNCTION __FQ__.dcp_calc_run({ARGS})
RETURNS TABLE(use_case_id STRING, result STRING)
COMMENT 'Internal: resolve the request (named use case by exact id/name or unique prefix, or ad-hoc), attach the rate card, and run the DCP calculator once. {WHO}'
RETURN
  WITH {RESOLVE},
  rates AS (
    SELECT to_json(named_struct(
             'as_of', CAST(current_date() AS STRING),
             'source', array_join(array_sort(collect_set(CASE WHEN provider = 'DATABRICKS' AND unit = 'DBU' AND rate IS NOT NULL THEN source END)), ', '),
             'rows', collect_list(named_struct('provider', provider, 'service', service, 'sku', sku, 'unit', unit, 'region', region,
                       'rate', rate, 'status', status, 'source', source, 'effective_start', effective_start,
                       'effective_end', effective_end, 'observed_rate', observed_rate, 'notes', coalesce(notes, ''))))) AS j
    FROM __FQ__.dcp_rate_card)
  SELECT req.use_case_id, __FQ__.dcp_calc(req.r, rates.j, req.ov, req.named) AS result FROM req CROSS JOIN rates""")
stmts.append(f"""CREATE OR REPLACE FUNCTION __FQ__.dcp_check_inputs({ARGS})
RETURNS TABLE(use_case_id STRING, status STRING, section STRING, component_id STRING, field STRING, issue STRING, blocking BOOLEAN,
              expected STRING, received STRING, question STRING)
COMMENT 'DCP INPUT CHECK as rows - NOT needed for estimates: dcp_calc_totals / dcp_calc_line_items / dcp_calc_details already run this check and return request_status NEEDS_INPUT. Use ONLY when the user explicitly asks to validate a request or what is missing, without asking for a cost. status READY = every required input is present and valid. status NEEDS_INPUT = ask the user every blocking row''s question (issue MISSING = required input not given, WRONG_TYPE = e.g. text "30" instead of the number 30, INVALID_VALUE = negative, unknown type/model or rejected by the calculator, UNKNOWN_FIELD = misspelt key, NOT_JSON, NO_WORKLOAD). blocking = false rows (RANGE_BOUND_MISSING) do not stop the estimate: that scenario shows PENDING. UNKNOWN_USE_CASE = the named use case was not found. {WHO} A JSON request pasted by the user goes in p_request verbatim.'
RETURN
  WITH {RESOLVE},
  chk AS (SELECT req.use_case_id, from_json(__FQ__.dcp_check(req.r, req.ov, req.named),
                 'struct<status:string,issues:array<struct<section:string,component_id:string,field:string,issue:string,blocking:boolean,expected:string,received:string,question:string>>>') AS j
          FROM req)
  SELECT chk.use_case_id, chk.j.status, i.section, i.component_id, i.field, i.issue, i.blocking, i.expected, i.received, i.question
  FROM chk LATERAL VIEW OUTER explode(chk.j.issues) x AS i
  UNION ALL
  SELECT p.uc, 'UNKNOWN_USE_CASE', 'request', NULL, 'p_use_case', 'UNKNOWN_USE_CASE', true, 'a loaded use case id or name', p.uc,
         'No single loaded use case matches this name. Which use case is it (SELECT use_case_id, use_case_name FROM __FQ__.dcp_requests)?'
  FROM p WHERE p.rq IS NULL AND NOT EXISTS (SELECT 1 FROM req)""")
stmts.append(f"""CREATE OR REPLACE FUNCTION __FQ__.dcp_calc_totals({ARGS})
RETURNS TABLE(use_case_id STRING, use_case_name STRING, scenario STRING, scenario_label STRING, recurring_monthly_usd DOUBLE, core_monthly_usd DOUBLE,
              annual_usd DOUBLE, one_time_usd DOUBLE, first_year_usd DOUBLE, databricks_dbu DOUBLE, databricks_usd DOUBLE, aws_usd DOUBLE,
              pending_items INT, pending_detail STRING, status STRING, area_states STRING, confidence STRING, confidence_reason STRING, request_status STRING, errors STRING, next_step STRING,
              calculator_version STRING, rate_card_source STRING, disclaimer STRING)
COMMENT 'DCP CALCULATOR - totals by scenario (LOW / EXPECTED / HIGH / STRESS): recurring_monthly_usd (all-in monthly), core_monthly_usd (processing + SQL + recurring DataSync), annual (Year 2 on), one-time, first year, Databricks DBU and USD, AWS USD, pending count and pending_detail (each PENDING item and why - always report it), status (COMPLETE or PARTIAL_PENDING_INPUTS), area_states (each calculator area: INCLUDED, EXCLUDED, UNKNOWN or NOT_APPLICABLE - always report EXCLUDED / UNKNOWN areas as not estimated), confidence. request_status NEEDS_INPUT + errors (one question per missing or invalid input; no numbers) when inputs must be supplied or corrected first, and next_step = exactly how to make the next call with the user''s answers (follow it; never write a request JSON yourself); REJECTED + errors when validation fails. Show the numbers exactly as returned. {WHO}'
RETURN
  WITH jj AS (SELECT r.use_case_id, from_json(r.result, '{RESULT}') AS j FROM {CALL} r)
  SELECT jj.use_case_id, jj.j.header.use_case_name, t.scenario, t.label, t.recurring_monthly_usd, t.core_monthly_usd, t.annual_usd,
         t.one_time_usd, t.first_year_usd, t.databricks_dbu, t.databricks_usd, t.aws_usd, t.pending_items,
         array_join(transform(jj.j.pending, x -> concat(x.component_id, ': ', x.reason)), '; '), t.status,
         array_join(transform(jj.j.areas, x -> concat(x.area, ': ', x.state)), '; '),
         jj.j.confidence.rating, jj.j.confidence.reason, jj.j.status, array_join(jj.j.errors, '; '), replace(jj.j.next_step, '<SCHEMA>.', '__FQ__.'), jj.j.header.calculator_version,
         jj.j.header.rate_card_source, jj.j.disclaimer
  FROM jj LATERAL VIEW OUTER explode(jj.j.totals) x AS t""")
stmts.append(f"""CREATE OR REPLACE FUNCTION __FQ__.dcp_calc_line_items({ARGS}, p_scenario STRING DEFAULT NULL)
RETURNS TABLE(use_case_id STRING, scenario STRING, component_id STRING, engine STRING, description STRING, quantity DOUBLE, unit STRING,
              rate DOUBLE, rate_status STRING, rate_source STRING, cost_usd DOUBLE, recurrence STRING, treatment STRING, evidence STRING,
              basis STRING, formula STRING, warnings STRING)
COMMENT 'DCP CALCULATOR - line items: one row per priced or pending cost (quantity x rate = cost_usd; PENDING lines have no cost and are not counted). p_scenario filters to LOW / EXPECTED / HIGH / STRESS. Quote rate, rate_status and rate_source from here; never change them. {WHO}'
RETURN
  SELECT r.use_case_id, l.scenario, l.component_id, l.engine, l.description, l.quantity, l.unit, l.rate, l.rate_status, l.rate_source,
         l.cost_usd, l.recurrence, l.treatment, l.evidence, l.basis, l.formula, l.warnings
  FROM {CALL} r
  LATERAL VIEW explode(from_json(r.result, '{RESULT}').line_items) x AS l
  WHERE {N('p_scenario')} IS NULL OR l.scenario = upper(p_scenario)""")
stmts.append(f"""CREATE OR REPLACE FUNCTION __FQ__.dcp_calc_details({ARGS})
RETURNS TABLE(use_case_id STRING, section STRING, component_id STRING, item STRING, detail STRING)
COMMENT 'DCP CALCULATOR - explanation: section = SUMMARY (the calculator plain-language summary), CONFIDENCE (rating + reason), PENDING (what could not be priced and why), COMPUTE_DECISION (selected compute, alternative, confidence, status), ASSUMPTION (each non-confirmed input and its basis), WARNING, NOTE, SCOPE_INCLUDED, SCOPE_EXCLUDED, AREA (each calculator area and its state), RATE (rates used and their source), DISCLAIMER, ERROR (validation). {WHO}'
RETURN
  WITH j AS (SELECT r.use_case_id, from_json(r.result, '{RESULT}') AS j FROM {CALL} r)
  SELECT use_case_id, 'SUMMARY', NULL, NULL, j.summary FROM j WHERE j.summary IS NOT NULL
  UNION ALL SELECT use_case_id, 'CONFIDENCE', NULL, j.confidence.rating, j.confidence.reason FROM j WHERE j.confidence IS NOT NULL
  UNION ALL SELECT use_case_id, 'PENDING', p.component_id, NULL, p.reason FROM j LATERAL VIEW explode(j.pending) x AS p
  UNION ALL SELECT use_case_id, 'COMPUTE_DECISION', d.component_id, d.selected,
         concat('alternative ', coalesce(d.alternative, '-'), '; confidence ', d.confidence, '; status ', d.status,
                '; streaming ', d.streaming_decision, '; eligible ', d.eligible) FROM j LATERAL VIEW explode(j.compute_decisions) x AS d
  UNION ALL SELECT use_case_id, 'ASSUMPTION', NULL, NULL, a FROM j LATERAL VIEW explode(j.assumptions) x AS a
  UNION ALL SELECT use_case_id, 'WARNING', NULL, NULL, w FROM j LATERAL VIEW explode(j.warnings) x AS w
  UNION ALL SELECT use_case_id, 'NOTE', NULL, NULL, n FROM j LATERAL VIEW explode(j.notes) x AS n
  UNION ALL SELECT use_case_id, 'SCOPE_INCLUDED', NULL, NULL, s FROM j LATERAL VIEW explode(j.scope.included) x AS s
  UNION ALL SELECT use_case_id, 'SCOPE_EXCLUDED', NULL, NULL, s FROM j LATERAL VIEW explode(j.scope.excluded) x AS s
  UNION ALL SELECT use_case_id, 'RATE', NULL, NULL, s FROM j LATERAL VIEW explode(j.scope.rates_used) x AS s
  UNION ALL SELECT use_case_id, 'AREA', a.area, a.state, NULL FROM j LATERAL VIEW explode(j.areas) x AS a
  UNION ALL SELECT use_case_id, 'ERROR', NULL, NULL, e FROM j LATERAL VIEW explode(j.errors) x AS e
  UNION ALL SELECT use_case_id, 'NEXT_STEP', NULL, NULL, replace(j.next_step, '<SCHEMA>.', '__FQ__.') FROM j WHERE j.next_step IS NOT NULL
  UNION ALL SELECT use_case_id, 'DISCLAIMER', NULL, NULL, j.disclaimer FROM j""")
sys.path.insert(0, os.path.join(ROOT, "calculator"))
import dcp_adapter
q = lambda v: "NULL" if v is None else "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"
INTAKE = ",\n    ".join("(" + ", ".join(q(v) for v in r) + ")" for r in dcp_adapter.intake_questions(m))
stmts.append(f"""CREATE OR REPLACE FUNCTION __FQ__.dcp_intake_questions(p_area STRING DEFAULT NULL)
RETURNS TABLE(area STRING, field STRING, type STRING, requirement STRING, default_value STRING, question STRING)
COMMENT 'DCP GUIDED INTAKE - every input of the calculator v{VERSION} request, per area (processing, sql, transfer, storage, model_endpoints, one_time_model_jobs), generated from the request schema: requirement REQUIRED (no default; must be asked), DEFAULT (the calculator applies default_value - show it and let the user change it) or OPTIONAL. Use for a guided estimate: ask the REQUIRED questions of each included area and show its defaults explicitly. p_area filters to one area.'
RETURN
  SELECT * FROM VALUES
    {{INTAKE}}
    AS v(area, field, type, requirement, default_value, question)
  WHERE {N('p_area')} IS NULL OR area = lower(trim(p_area))""".replace("{INTAKE}", INTAKE))
open(os.path.join(ROOT, "sql", "06_dcp_calculator.sql"), "w").write("\n-- @@\n".join(stmts) + "\n")

# ---- rate card seed = the calculator's reference registry; Databricks DBU rows are re-priced by the setup notebook / notebook 09 ----
BILLING_SKU = {"ENTERPRISE_JOBS_SERVERLESS": "ENTERPRISE_JOBS_SERVERLESS_COMPUTE", "SQL_PRO": "ENTERPRISE_SQL_PRO_COMPUTE",
               "ENTERPRISE_ALL_PURPOSE_COMPUTE": "ENTERPRISE_ALL_PURPOSE_COMPUTE",
               "ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE": "ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE",
               "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE": "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE"}
open(os.path.join(ROOT, "calculator", "rate_registry_seed.json"), "w").write(
    __import__("json").dumps({"calculator_version": VERSION, "billing_sku": BILLING_SKU,
                              "rows": [dataclasses.asdict(r) for r in m.DEFAULT_RATE_REGISTRY]}, indent=1))
print(f"calculator v{VERSION} key {KEY}: wrote sql/06_dcp_calculator.sql ({len(stmts)} statements) and calculator/rate_registry_seed.json")
