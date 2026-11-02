"""Generate the DCP engine input map from calculator dataclasses and intake questions.
Does not change calculator validation or defaults. Full JSON fields, not SQL argument aliases.
"""
import dataclasses
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'notebooks'))
sys.path.insert(0,str(ROOT/'calculator'))
import dcp_estimator as model
import dcp_adapter as adapter
TABLE='dcp_engine_input_requirements'
ENGINES={'processing':'Processing Engine','sql':'SQL Reporting Engine','transfer':'DataSync Transfer Engine','storage':'S3 and KMS Engine','model_endpoints':'Model API Engine','one_time_model_jobs':'Model API Engine'}
EXAMPLES={'component_id':'COMPONENT-01','name':'My workload','role':'Summarization','description':'Initial indexing','workload_type':'SCHEDULED_BATCH','quantity':1,'pattern':'SCHEDULED','runtime_hours_low':1,'runtime_hours_expected':2,'runtime_hours_high':3,'runs_per_month':30,'concurrent_users':10,'retained_tb':2,'files_per_day':100,'daily_gb_low':5,'daily_gb_expected':10,'daily_gb_high':15,'calls_per_day':100,'input_tokens_low':500,'input_tokens_expected':1000,'input_tokens_high':1500,'output_tokens_low':100,'output_tokens_expected':200,'output_tokens_high':300,'api_calls':100,'streaming_mode':'INCREMENTAL_AVAILABLE_NOW','latency_requirement':'MINUTES_OR_MORE','dbu_per_hour':{'EXPECTED':1.0}}
UNITS={'runtime_hours':'hours/run','runs_per_month':'runs/month','quantity':'identical workloads','daily_gb':'GB/day','initial_transfer_gb':'GB, one-time','transfer_days_per_month':'days/month','working_days_per_month':'days/month','retained_tb':'TB retained','files_per_day':'files/day','encrypted_files_per_day':'files/day','kms_keys':'keys','days_per_month':'days/month','s3_write_requests_per_file':'requests/file','s3_read_requests_per_file':'requests/file','kms_requests_per_encrypted_file':'requests/encrypted file','concurrent_users':'simultaneously active users','total_users':'users','calls_per_day':'calls/day','api_calls':'calls, one-time','dbu_per_hour':'DBU/hour by scenario'}
def build_rows():
 intake={(a,f):(typ,req,default,question) for a,f,typ,req,default,question in adapter.intake_questions(model)}
 rows=[]
 for area,cls,is_list in adapter.SECTIONS:
  for f in dataclasses.fields(getattr(model,cls)):
   default=None if f.default is dataclasses.MISSING else f.default
   typ=model._type_text(f)
   req='REQUIRED_IN_JSON' if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING else ('DEFAULT' if default is not None else 'OPTIONAL')
   question=adapter.QUESTIONS.get(f.name,'Provide '+f.name.replace('_',' ')+'.')
   if (area,f.name) in intake:
    typ,ireq,_,question=intake[area,f.name]
    if ireq.startswith('REQUIRED'):req='REQUIRED'
   applies='This area is included.'
   note=''
   role='USER_INPUT'
   if f.name in ('component_id','name','role','description'):
    role='IDENTITY';note='Required by raw JSON schema; the SQL request builder supplies selected identity fields. Preserve user identifiers.'
   if f.name in ('evidence','dbu_per_hour_basis','dbu_per_hour_evidence'):
    role='PROVENANCE';note='Evidence/traceability metadata; not an additional usage quantity.'
   if area=='processing':
    if f.name.startswith('runtime_hours'):
     applies='Batch types only; not used for CONTINUOUS_STREAMING.'
     req='REQUIRED_FOR_EXPECTED' if f.name.endswith('expected') else 'SCENARIO_BOUND'
     note='LOW/HIGH bounds price corresponding scenarios; absent bounds leave those scenarios pending. A single runtime supplied through dcp_request is mapped to all scenarios.'
    elif f.name=='runs_per_month':
     req='CONDITIONAL';applies='Required for SCHEDULED_BATCH, ML_TRAINING and BATCH_SCORING; EVENT_DRIVEN_BATCH has scenario planning defaults; not used for CONTINUOUS_STREAMING.'
     note='Event-driven default LOW/EXPECTED/HIGH/STRESS runs: 0/1/1/4. Disclose and confirm assumptions; zero is not a forecast.'
    elif f.name in ('streaming_mode','latency_requirement'):
     applies='CONTINUOUS_STREAMING classification.';note='UNCONFIRMED may select a Jobs pricing proxy; seconds-level or true continuous streaming selects a pipeline path whose rate can remain pending.'
    elif f.name=='dbu_per_hour':note='Scenario map overrides supplied calibration and planning intensity; optional, not a cluster size.'
    elif f.name.startswith('requires_') or f.name=='declarative_pipeline_preferred':note='Compute-selection constraint; selected compute may not have a supported DCP pricing path.'
   if area in ('transfer','model_endpoints','one_time_model_jobs') and (f.name.startswith('daily_gb_') or 'tokens_' in f.name):
    suffix=f.name.rsplit('_',1)[-1]
    req=('REQUIRED_BOUND' if area=='one_time_model_jobs' else 'REQUIRED_FOR_EXPECTED' if suffix=='expected' else 'SCENARIO_BOUND')
    note='A missing requested scenario bound leaves that scenario pending.'
    if 'tokens_' in f.name:note+=' EXPECTED can use the labeled midpoint when both LOW and HIGH token bounds are supplied; output tokens are never inferred from input.'
   if area=='storage' and f.name in ('encrypted_files_per_day','kms_keys'):note='Default zero is not evidence that encryption is out of scope; confirm applicability.'
   if area=='storage' and f.name=='kms_requests_per_encrypted_file':applies='KMS-encrypted files are present.'
   if area=='storage' and f.name=='request_assumptions_confirmed':note='Controls evidence labeling of request assumptions.'
   if f.name=='total_users':role='CONTEXT_ONLY';note='Recorded context; current SQL consumption formula uses concurrent_users, not total_users.'
   if f.name=='api_calls':role='CONTEXT_ONLY';note='Recorded context; current one-time token formula uses token totals, not api_calls.'
   unit=next((v for k,v in UNITS.items() if f.name==k or f.name.startswith(k+'_')),'not applicable')
   if 'tokens_' in f.name:
    unit='tokens, job total' if area=='one_time_model_jobs' else 'tokens/call'
    if area=='one_time_model_jobs':question='How many TOTAL '+f.name.replace('_',' ')+' for the one-time job?'
   allowed=''
   if f.name=='workload_type':allowed=', '.join(model.WORKLOAD_TYPES)
   if f.name=='streaming_mode':allowed=', '.join(adapter.STREAMING_MODES)
   if f.name=='latency_requirement':allowed=', '.join(adapter.LATENCIES)
   if f.name=='serving_mode':allowed='PAY_PER_TOKEN';note='Only pay-per-token mode is priced by this engine.'
   example=EXAMPLES.get(f.name,default)
   if f.name=='model':example=model.DEFAULT_MODEL_CONVERSIONS[0].model;allowed='Exact effective model in the DCP conversion registry; no substitute model.'
   rows.append({'sort_order':len(rows)+1,'input_id':area+'.'+f.name,'engine':ENGINES[area],'request_area':area,'parameter_name':f.name,'json_path':area+('[].' if is_list else '.')+f.name,'data_type':typ,'requirement':req,'applies_when':applies,'default_value_json':json.dumps(default),'unit':unit,'allowed_values':allowed,'question':question,'example_value_json':json.dumps(example),'input_role':role,'notes':note,'calculator_version':model.CALCULATOR_VERSION})
 return rows
COLUMNS=['sort_order','input_id','engine','request_area','parameter_name','json_path','data_type','requirement','applies_when','default_value_json','unit','allowed_values','question','example_value_json','input_role','notes','calculator_version']
def generate():
 def q(x):return "'"+str(x).replace("'","''")+"'"
 vals=['('+str(r['sort_order'])+', '+', '.join(q(r[k]) for k in COLUMNS[1:])+')' for r in build_rows()]
 return 'CREATE OR REPLACE TABLE __FQ__.'+TABLE+"\nUSING DELTA\nCOMMENT 'DCP Engine Input Requirements: parameter map for five engines and six request sections. Generated from calculator schema. Use for required-input discovery, units, defaults and conditional questions; dcp_check_inputs remains the authority for a specific request. Examples are illustrative, never confirmed user data. Identity and provenance fields are distinguished from usage inputs. Shared request scope, region and scenarios are separate from these per-engine fields.'\nAS SELECT * FROM VALUES\n"+',\n'.join(vals)+'\nAS inputs('+', '.join(COLUMNS)+')\n'
if __name__=='__main__':
 out=ROOT/'sql/12_dcp_engine_inputs.sql';out.write_text(generate());print('Wrote',out.name,'with',len(build_rows()),'input fields across five engines')
