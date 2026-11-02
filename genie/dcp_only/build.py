"""Build the small, portable DCP-only Genie definition; no workspace writes."""
import json
from pathlib import Path
import uuid
ROOT = Path(__file__).resolve().parent
FQ = '__FQ__'
def nid(key): return uuid.uuid5(uuid.NAMESPACE_URL, 'dcp-only/' + key).hex

def build():
    request = {'use_case_id':'BATCH_EXAMPLE','use_case_name':'Batch example','scenarios':['EXPECTED'],'processing':[{'component_id':'B1','name':'Batch','workload_type':'SCHEDULED_BATCH','runtime_hours_expected':2,'runs_per_month':20}], '_areas': {'processing':'INCLUDED', **{area:'NOT_APPLICABLE' for area in ['sql','transfer','storage','model_endpoints','one_time_model_jobs']}}}
    raw = json.dumps(request, separators=(',', ':'))
    arg = "p_request => '" + raw.replace("'", "''") + "'"
    check = lambda args: f'SELECT c.status, c.section, c.component_id, c.field, c.blocking, c.question FROM {FQ}.dcp_check_inputs({args}) AS c'
    examples = [
      ('Validate this new JSON before estimating: {"use_case_id":"NEW","use_case_name":"Batch","processing":[{"component_id":"B1","name":"Batch","workload_type":"SCHEDULED_BATCH","runtime_hours_expected":2}]}', check('p_request => \'{"use_case_id":"NEW","use_case_name":"Batch","processing":[{"component_id":"B1","name":"Batch","workload_type":"SCHEDULED_BATCH","runtime_hours_expected":2}]}\'')),
      ('I need a scheduled batch estimate, 2 hours per run, 20 runs per month. What else do you need?', check(f"p_request => {FQ}.dcp_request(p_workload_type => 'SCHEDULED_BATCH', p_runtime_hours_expected => 2, p_runs_per_month => 20)")),
      ('Validate my confirmed workload before calculating: ' + raw, check(arg)),
      ('Validation is READY and all areas are confirmed for this JSON. Calculate the estimate: ' + raw, f'SELECT t.scenario, t.recurring_monthly_usd, t.one_time_usd, t.first_year_usd, t.pending_items, t.pending_detail, t.status, t.area_states, t.request_status, t.errors, t.rate_card_source, t.disclaimer FROM {FQ}.dcp_calc_totals({arg}) AS t'),
      ('For the validated JSON, show costs by engine: ' + raw, f"SELECT li.scenario, li.engine, li.recurrence, SUM(CASE WHEN li.treatment = 'INCLUDED' THEN li.cost_usd END) AS priced_subtotal_usd, SUM(CASE WHEN li.treatment = 'PENDING' THEN 1 ELSE 0 END) AS pending_items FROM {FQ}.dcp_calc_line_items({arg}) AS li GROUP BY li.scenario, li.engine, li.recurrence ORDER BY li.scenario, li.engine, li.recurrence"),
      ('Explain assumptions and pending costs for this validated JSON: ' + raw, f'SELECT d.section, d.component_id, d.item, d.detail FROM {FQ}.dcp_calc_details({arg}) AS d'),
      ('Estimate the named SEND_US01 preset', f"SELECT t.scenario, t.recurring_monthly_usd, t.one_time_usd, t.pending_items, t.pending_detail, t.status, t.area_states, t.request_status, t.rate_card_source, t.disclaimer FROM {FQ}.dcp_calc_totals(p_use_case => 'SEND_US01') AS t"),
    ]
    tables = [dict(identifier=FQ+'.dcp_engine_input_requirements', description=['Schema for the five DCP engines. Filter request_area to the selected engine. Select parameter_name, requirement, unit, default_value_json, applies_when, question and example_value_json only when useful; dcp_check_inputs determines actual missing inputs.'], column_configs=[dict(column_name='request_area', description=['processing, sql, transfer, storage, model_endpoints, one_time_model_jobs.'], enable_format_assistance=True, enable_entity_matching=True),dict(column_name='engine', description=['One of the five DCP calculator engines.'],enable_format_assistance=True,enable_entity_matching=True)]), dict(identifier=FQ+'.dcp_requests',description=['Named calculator presets. Select use_case_id and use_case_name for discovery; pass the selected ID to p_use_case. New workloads use p_request, not a preset.'])]
    tables = tables[:1]
    for table in tables:
        if 'column_configs' in table:
            table['column_configs'].sort(key=lambda c: c['column_name'])
    funcs = ['dcp_check_inputs','dcp_request','dcp_calc_totals','dcp_calc_line_items','dcp_calc_details','dcp_check','dcp_calc','dcp_calc_run']
    result = dict(version=2, config={'sample_questions':sorted([dict(id=nid(q), question=[q]) for q in ['I need a DCP cost estimate. What information do you need?', 'I am building batch and streaming pipelines. What inputs do you need?', 'Estimate the named SEND_US01 preset.']],key=lambda x:x['id'])}, data_sources={'tables':tables}, instructions={'text_instructions':[dict(id=nid('policy'),content=[(ROOT/'instructions.md').read_text().strip()])], 'sql_functions':sorted([dict(id=nid(f),identifier=FQ+'.'+f) for f in funcs],key=lambda x:x['id']), 'example_question_sqls':sorted([dict(id=nid(q),question=[q],sql=[sql]) for q,sql in examples],key=lambda x:x['id'])})
    (ROOT/'serialized_space.json').write_text(json.dumps(result,indent=2)+'\n')
    (ROOT/'sample_request.json').write_text(json.dumps(request,indent=2)+'\n')
    print('Built DCP-only agent: 1 input-schema table, DCP functions, 7 SQL examples.')
if __name__ == '__main__': build()
