# Live Genie examples

Synchronized from `01f1c809927f1586bbd2566786d9c455` at `2026-11-02T19:02:17.134Z`. The instructions in `instructions.md` are authoritative; examples below are preserved as exported.

## Example 1

Give me cost estimations for this pasted workload JSON — always validate inputs first (step 1: call dcp_check_inputs)

```sql
SELECT issues.section, issues.component_id, issues.field, issues.issue, issues.question FROM __FQ__.dcp_check_inputs(p_request => '{"_calculator_version":"4.0.2","use_case_id":"NEW_WORKLOAD_01","use_case_name":"Daily batch ingestion template validation","scenarios":["LOW","EXPECTED","HIGH"],"region":"US_EAST_1","processing":[{"component_id":"NEW-C01","name":"Daily batch ingestion","workload_type":"SCHEDULED_BATCH","pattern":"SCHEDULED","runtime_hours_low":0.5,"runtime_hours_expected":1.0,"runtime_hours_high":2.0,"runs_per_month":30,"evidence":"Calculator v4.0.2 new-workload template"}],"sql":{"component_id":"NEW-SQL","name":"Dashboards","concurrent_users":5,"working_days_per_month":22},"transfer":{"daily_gb_low":1,"daily_gb_expected":2,"daily_gb_high":4,"transfer_days_per_month":22,"initial_transfer_gb":null},"storage":{"retained_tb":1.0,"files_per_day":50,"encrypted_files_per_day":0,"kms_keys":0,"days_per_month":30,"s3_write_requests_per_file":1,"s3_read_requests_per_file":null},"model_endpoints":[],"one_time_model_jobs":[],"assumptions":{},"scope_included":["Scheduled Databricks batch processing","Databricks SQL reporting","Recurring AWS DataSync transfer","S3 retained storage and write requests"],"scope_excluded":["One-time migration","Model API workloads","KMS key and encryption costs"]}') AS issues WHERE issues.blocking
```

## Example 2

Follow-up after the user confirmed all area states and answered every missing input for a pasted JSON (e.g. model endpoints and one-time model jobs not applicable, each file read twice): calculate the totals

```sql
SELECT scenario, recurring_monthly_usd, one_time_usd, databricks_usd, aws_usd, pending_items, pending_detail, area_states, confidence, request_status, rate_card_source FROM __FQ__.dcp_calc_totals(p_request => '{"_calculator_version":"4.0.2","use_case_id":"NEW_WORKLOAD_01","use_case_name":"Daily batch ingestion template validation","scenarios":["LOW","EXPECTED","HIGH"],"region":"US_EAST_1","_areas":{"model_endpoints":"NOT_APPLICABLE","one_time_model_jobs":"NOT_APPLICABLE"},"processing":[{"component_id":"NEW-C01","name":"Daily batch ingestion","workload_type":"SCHEDULED_BATCH","pattern":"SCHEDULED","runtime_hours_low":0.5,"runtime_hours_expected":1.0,"runtime_hours_high":2.0,"runs_per_month":30}],"sql":{"component_id":"NEW-SQL","name":"Dashboards","concurrent_users":5,"working_days_per_month":22},"transfer":{"daily_gb_low":1,"daily_gb_expected":2,"daily_gb_high":4,"transfer_days_per_month":22,"initial_transfer_gb":null},"storage":{"retained_tb":1.0,"files_per_day":50,"encrypted_files_per_day":0,"kms_keys":0,"days_per_month":30,"s3_write_requests_per_file":1,"s3_read_requests_per_file":2},"model_endpoints":[],"one_time_model_jobs":[]}')
```

## Example 3

How do I estimate a recurring model API and a separate one-time indexing job?

```sql
SELECT totals.scenario, totals.recurring_monthly_usd, totals.one_time_usd, totals.request_status FROM __FQ__.dcp_calc_totals(p_request => __FQ__.dcp_request(p_model => 'claude-sonnet-4-6', p_model_component_id => 'API-01', p_calls_per_day => 500, p_input_tokens_expected => 2000, p_output_tokens_expected => 400, p_one_time_model => 'gpt-5-4', p_one_time_component_id => 'INDEX-01', p_one_time_input_tokens_low => 1000000, p_one_time_input_tokens_high => 1000000, p_one_time_output_tokens_low => 50000, p_one_time_output_tokens_high => 50000, p_one_time_api_calls => 500, p_not_applicable_areas => 'processing,sql,transfer,storage')) AS totals
```

## Example 4

List the required inputs for each DCP calculator engine.

```sql
SELECT inputs.engine, inputs.request_area, inputs.parameter_name, inputs.requirement, inputs.applies_when, inputs.default_value_json, inputs.unit, inputs.question FROM __FQ__.dcp_engine_input_requirements AS inputs ORDER BY inputs.sort_order
```

## Example 5

I need a cost estimate but I am not sure what kind. What can you estimate?

```sql
SELECT c.category, c.capability_kind FROM __FQ__.cost_estimator_functional_capabilities AS c WHERE c.estimate_type = 'DCP' ORDER BY c.sort_order
```

## Example 6

What DBU rate does the DCP calculator use for SQL Pro, and what is its status?

```sql
SELECT provider, service, sku, unit, region, rate, status, source FROM __FQ__.dcp_rate_card WHERE provider = 'DATABRICKS' AND unit = 'DBU' AND region = 'US_EAST_1' AND sku ILIKE '%SQL_PRO%'
```

## Example 7

5 TB is retained in S3 with 400 files a day, none encrypted, each read twice. The other calculator areas are not applicable. Monthly cost?

```sql
SELECT * FROM __FQ__.dcp_calc_totals(p_request => __FQ__.dcp_request(p_retained_tb => 5, p_files_per_day => 400, p_encrypted_files_per_day => 0, p_s3_read_requests_per_file => 2, p_not_applicable_areas => 'processing,sql,transfer,model_endpoints,one_time_model_jobs'))
```

## Example 8

Which DCP use cases are loaded?

```sql
SELECT use_case_id, use_case_name FROM __FQ__.dcp_requests ORDER BY use_case_id
```

## Example 9

Two Kafka streaming jobs need second-level latency. SQL, transfer, storage and model work are not applicable. What do they cost?

```sql
SELECT * FROM __FQ__.dcp_calc_totals(p_request => __FQ__.dcp_request(p_workload_type => 'CONTINUOUS_STREAMING', p_quantity => 2, p_latency_requirement => 'SECONDS', p_not_applicable_areas => 'sql,transfer,storage,model_endpoints,one_time_model_jobs'))
```

## Example 10

What if FIN_CLAIMS_02 runs its nightly rebuild 30 times a month?

```sql
SELECT * FROM __FQ__.dcp_calc_totals(p_use_case => 'FIN_CLAIMS_02', p_overrides => '{"processing[C02].runs_per_month": 30}')
```

## Example 11

For DCP scheduled batch, I already know runtime is 2 hours. What else is required?

```sql
SELECT c.category, i.parameter_name AS input, i.question AS what_to_provide, i.unit FROM __FQ__.cost_estimator_functional_capabilities AS c JOIN __FQ__.cost_estimator_workload_inputs AS i ON c.capability_id = i.capability_id AND c.estimate_type = i.estimate_type WHERE c.category = 'SCHEDULED_BATCH' AND c.estimate_type = 'DCP' AND i.requirement = 'REQUIRED' AND i.parameter_name <> 'runtime_hours_expected' ORDER BY c.sort_order, i.input_order
```

## Example 12

What rates does the DCP calculator use?

```sql
SELECT provider, service, sku, unit, region, rate, status, source FROM __FQ__.dcp_rate_card ORDER BY provider, sku
```

## Example 13

How do I estimate a scheduled workload together with a one-time indexing job?

```sql
SELECT dcp_calc_totals.scenario, dcp_calc_totals.recurring_monthly_usd, dcp_calc_totals.one_time_usd, dcp_calc_totals.request_status FROM __FQ__.dcp_calc_totals(p_request => __FQ__.dcp_request(p_workload_type => 'SCHEDULED_BATCH', p_runtime_hours_expected => 1, p_runs_per_month => 20, p_one_time_model => 'gpt-5-4', p_one_time_input_tokens_low => 1000000, p_one_time_input_tokens_high => 1000000, p_one_time_output_tokens_low => 0, p_one_time_output_tokens_high => 0, p_not_applicable_areas => 'sql,transfer,storage,model_endpoints')) AS dcp_calc_totals
```

## Example 14

We copy 3 GB a day with DataSync on 20 days a month and migrate 1,200 GB once. Processing, SQL, storage and model work are not applicable. Costs?

```sql
SELECT * FROM __FQ__.dcp_calc_totals(p_request => __FQ__.dcp_request(p_daily_gb_expected => 3, p_transfer_days_per_month => 20, p_initial_transfer_gb => 1200, p_not_applicable_areas => 'processing,sql,storage,model_endpoints,one_time_model_jobs'))
```

## Example 15

What exact inputs does the DCP calculator still need before it can estimate a partially specified request?

```sql
SELECT issues.section, issues.component_id, issues.field, issues.issue, issues.question FROM __FQ__.dcp_check_inputs(p_request => __FQ__.dcp_request(p_workload_type => 'SCHEDULED_BATCH', p_runtime_hours_expected => 2)) AS issues WHERE issues.blocking
```

## Example 16

A scheduled batch runs 30 times a month, 2 hours per run. SQL and storage are not applicable, transfer is excluded, and model work is unknown. Monthly cost?

```sql
SELECT * FROM __FQ__.dcp_calc_totals(p_request => __FQ__.dcp_request(p_workload_type => 'SCHEDULED_BATCH', p_runtime_hours_expected => 2, p_runs_per_month => 30, p_not_applicable_areas => 'sql,storage', p_excluded_areas => 'transfer', p_unknown_areas => 'model_endpoints,one_time_model_jobs'))
```

## Example 17

What if SEND_US01 keeps 12 TB of data?

```sql
SELECT * FROM __FQ__.dcp_calc_totals(p_use_case => 'SEND_US01', p_overrides => '{"retained_storage_tb": 12}')
```

## Example 18

What inputs do you need for a DCP continuous streaming estimate?

```sql
SELECT c.category, i.parameter_name AS input, i.question AS what_to_provide, i.unit FROM __FQ__.cost_estimator_functional_capabilities AS c JOIN __FQ__.cost_estimator_workload_inputs AS i ON c.capability_id = i.capability_id AND c.estimate_type = i.estimate_type WHERE c.category = 'CONTINUOUS_STREAMING' AND c.estimate_type = 'DCP' AND i.requirement = 'REQUIRED' ORDER BY c.sort_order, i.input_order
```

## Example 19

I selected scheduled batch for a DCP estimate. What inputs do you need and what other categories can I estimate?

```sql
SELECT i.parameter_name AS input, i.question AS what_to_provide, i.unit FROM __FQ__.cost_estimator_functional_capabilities AS c JOIN __FQ__.cost_estimator_workload_inputs AS i ON c.capability_id = i.capability_id AND c.estimate_type = i.estimate_type WHERE c.category = 'SCHEDULED_BATCH' AND c.estimate_type = 'DCP' AND i.requirement = 'REQUIRED' ORDER BY i.input_order
```

## Example 20

Which defaults should I confirm for a DCP scheduled batch estimate?

```sql
SELECT i.parameter_name AS input, i.default_value AS proposed_default, i.question AS what_to_confirm FROM __FQ__.cost_estimator_functional_capabilities AS c JOIN __FQ__.cost_estimator_workload_inputs AS i ON c.capability_id = i.capability_id AND c.estimate_type = i.estimate_type WHERE c.category = 'SCHEDULED_BATCH' AND c.estimate_type = 'DCP' AND i.requirement = 'CONFIRM_DEFAULT' ORDER BY i.input_order
```

## Example 21

Give me the DCP estimate for FIN_CLAIMS_02.

```sql
SELECT * FROM __FQ__.dcp_calc_totals(p_use_case => 'FIN_CLAIMS_02')
```

## Example 22

For a processing-only scheduled batch, show the scenario totals and confirmed area states.

```sql
SELECT totals.scenario, totals.recurring_monthly_usd, totals.area_states, totals.request_status FROM __FQ__.dcp_calc_totals(p_request => __FQ__.dcp_request(p_workload_type => 'SCHEDULED_BATCH', p_runtime_hours_expected => 2, p_runs_per_month => 20, p_not_applicable_areas => 'sql,transfer,storage,model_endpoints,one_time_model_jobs')) AS totals
```

## Example 23

I need DCP scheduled batch and SQL reporting estimates. Which required inputs should I provide?

```sql
SELECT c.category, i.parameter_name AS input, i.question AS what_to_provide, i.unit FROM __FQ__.cost_estimator_functional_capabilities AS c JOIN __FQ__.cost_estimator_workload_inputs AS i ON c.capability_id = i.capability_id AND c.estimate_type = i.estimate_type WHERE c.category IN ('SCHEDULED_BATCH', 'SQL_REPORTING') AND c.estimate_type = 'DCP' AND i.requirement = 'REQUIRED' ORDER BY c.sort_order, i.input_order
```

## Example 24

What fields and defaults should I collect before estimating a batch workload?

```sql
SELECT i.parameter_name AS input, i.question AS what_to_provide, i.unit FROM __FQ__.cost_estimator_functional_capabilities AS c JOIN __FQ__.cost_estimator_workload_inputs AS i ON c.capability_id = i.capability_id AND c.estimate_type = i.estimate_type WHERE c.category = 'SCHEDULED_BATCH' AND c.estimate_type = 'DCP' AND i.requirement = 'REQUIRED' ORDER BY i.input_order
```
