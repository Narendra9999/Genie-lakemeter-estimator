"""Generate workload-specific intake mapping. Coverage/validation remains in capabilities.
DCP fields are joined in Python to schema-derived input metadata; Lakemeter names/types
are read from function signatures, with conditional/null semantics recorded explicitly.
"""
import json
import re
from pathlib import Path
from build_engine_inputs import build_rows
ROOT=Path(__file__).resolve().parents[1]
COLUMNS=['capability_id','estimate_type','request_area','parameter_name','requirement','question','unit','default_value','known_value','input_order']
PROCESSING=['SCHEDULED_BATCH','EVENT_DRIVEN_BATCH','CONTINUOUS_STREAMING','ML_TRAINING','BATCH_SCORING']
DCP_FIELDS={
 'SQL_REPORTING':{'sql':{'concurrent_users':'REQUIRED','working_days_per_month':'CONFIRM_DEFAULT'}},
 'TRANSFER_COSTS':{'transfer':{'daily_gb_expected':'REQUIRED','daily_gb_low':'RANGE','daily_gb_high':'RANGE','transfer_days_per_month':'CONFIRM_DEFAULT','initial_transfer_gb':'OPTIONAL'}},
 'STORAGE_COSTS':{'storage':{'retained_tb':'REQUIRED','files_per_day':'REQUIRED','s3_read_requests_per_file':'REQUIRED','days_per_month':'CONFIRM_DEFAULT','encrypted_files_per_day':'CONFIRM_DEFAULT','kms_keys':'CONFIRM_DEFAULT','s3_write_requests_per_file':'CONFIRM_DEFAULT','kms_requests_per_encrypted_file':'CONDITIONAL','request_assumptions_confirmed':'CONFIRM_DEFAULT'}},
 'MODEL_API_COSTS':{
  'model_endpoints':{'model':'CONDITIONAL','calls_per_day':'CONDITIONAL','input_tokens_expected':'CONDITIONAL','output_tokens_expected':'CONDITIONAL','input_tokens_low':'RANGE','input_tokens_high':'RANGE','output_tokens_low':'RANGE','output_tokens_high':'RANGE','serving_mode':'CONFIRM_DEFAULT'},
  'one_time_model_jobs':{'model':'CONDITIONAL','input_tokens_low':'CONDITIONAL','input_tokens_high':'CONDITIONAL','output_tokens_low':'CONDITIONAL','output_tokens_high':'CONDITIONAL','serving_mode':'CONFIRM_DEFAULT'}}}
UNITS={'cloud':'provider','region':'region code','tier':'edition','num_workers':'workers','runs_per_day':'runs/day','avg_runtime_minutes':'minutes/run','days_per_month':'days/month','hours_per_month':'hours/month','concurrency':'concurrent requests','capacity_millions':'million vectors','capacity_units':'capacity units','ha_nodes':'nodes','num_apps':'apps','pages_thousands':'thousand pages','num_docs':'documents','num_inputs':'inputs','data_gb':'GB (profile-specific basis)','window_minutes':'minutes','max_workers':'workers','custom_rate_per_1000':'DBU/1,000 items'}
def build_rows_map():
 caps=json.loads((ROOT/'genie/catalog/functional_capabilities.json').read_text())
 products={x['id']:x for x in json.loads((ROOT/'genie/catalog/estimator_catalog.json').read_text())['products']}
 fields={x['input_id']:x for x in build_rows()}
 sql=(ROOT/'sql/02_functions.sql').read_text()+'\n'+(ROOT/'sql/08_workload_sizing.sql').read_text()
 rows=[]
 for cap in caps:
  category=cap['category'];cid=cap['capability_id'];et=cap['estimate_type']
  def add(area,name,req,question,unit='',default=None,known=None):
   rows.append(dict(zip(COLUMNS,[cid,et,area,name,req,question,unit,default,known,len(rows)+1])))
  if et=='DCP':
   if category in PROCESSING:
    fs={'workload_type':'KNOWN','quantity':'CONFIRM_DEFAULT','requires_custom_runtime':'CONFIRM_DEFAULT','requires_custom_networking':'CONFIRM_DEFAULT','requires_gpu':'CONFIRM_DEFAULT','declarative_pipeline_preferred':'CONFIRM_DEFAULT','dbu_per_hour':'OPTIONAL'}
    if category=='CONTINUOUS_STREAMING':fs.update({'streaming_mode':'REQUIRED','latency_requirement':'REQUIRED'})
    else:fs.update({'runtime_hours_expected':'REQUIRED','runs_per_month':'CONFIRM_DEFAULT' if category=='EVENT_DRIVEN_BATCH' else 'REQUIRED','runtime_hours_low':'RANGE','runtime_hours_high':'RANGE'})
    groups={'processing':fs}
   else:groups=DCP_FIELDS[category]
   for area,fs in groups.items():
    for name,req in fs.items():
     f=fields[area+'.'+name];default=f['default_value_json']
     if category=='EVENT_DRIVEN_BATCH' and name=='runs_per_month':default='LOW=0; EXPECTED=1; HIGH=1; STRESS=4'
     question=f['question']
     if category=='MODEL_API_COSTS':question=('Recurring API only: ' if area=='model_endpoints' else 'One-time job only: ')+question
     add(area,name,req,question,f['unit'],None if default=='null' else default,category if name=='workload_type' else None)
  else:
   func='estimate_sized_workload' if cap['capability_kind']=='WORKLOAD_SIZING' else products[cap['catalog_product_ids'][0]]['entrypoints'][0]
   match=re.search(r'FUNCTION __FQ__\.'+func+r'\((.*?)\)\s*RETURNS',sql,re.S)
   if not match:raise ValueError('Missing signature '+func)
   for arg in match.group(1).split(','):
    m=re.fullmatch(r'\s*p_(\w+)\s+\w+(?:\s+DEFAULT\s+(.+?))?\s*',arg)
    if not m:raise ValueError(arg)
    name,default=m.groups();req='REQUIRED' if default is None else 'OPTIONAL' if default=='NULL' else 'CONFIRM_DEFAULT'
    known=category if name=='workload' and cap['capability_kind']=='WORKLOAD_SIZING' else None
    if known:req='KNOWN'
    if name in ('runs_per_day','avg_runtime_minutes','days_per_month','hours_per_month') and category=='DBSQL_WAREHOUSE':req='CONDITIONAL'
    if name in ('mode','pages_thousands','complexity','hours_per_month') and category=='AI_PARSE':req='CONDITIONAL'
    if name=='custom_rate_per_1000':req='CONDITIONAL'
    if cap['capability_kind']=='WORKLOAD_SIZING' and name=='runs_per_day':req='KNOWN' if category=='STREAMING' else 'REQUIRED';known='1' if category=='STREAMING' else None
    question='Provide '+name.replace('_',' ')+'.'
    if category=='DBSQL_WAREHOUSE' and name in ('runs_per_day','avg_runtime_minutes','days_per_month','hours_per_month'):question='Provide monthly hours OR runs/day, runtime minutes and days/month; do not supply both schedules.'
    if category=='AI_PARSE' and name in ('mode','pages_thousands','complexity','hours_per_month'):question='Choose pages mode (page volume + complexity) or direct DBU mode (DBU quantity passed as hours_per_month).'
    if name=='custom_rate_per_1000':question='For a custom/unlisted document type, provide its approved DBU rate per 1,000 items; never assume zero.'
    unit=UNITS.get(name,'')
    if name=='quantity':unit='tokens or hours according to rate_type'
    add(func,'p_'+name,req,question,unit,None if default=='NULL' else default,known)
 assert len({(x['capability_id'],x['request_area'],x['parameter_name']) for x in rows})==len(rows)
 return rows

def generate():
 def q(x):return 'NULL' if x is None else "'"+str(x).replace("'","''")+"'"
 values=['('+', '.join(str(v) if isinstance(v,int) else q(v) for v in [row[c] for c in COLUMNS])+')' for row in build_rows_map()]
 return "CREATE OR REPLACE TABLE __FQ__.cost_estimator_workload_inputs\nUSING DELTA\nCOMMENT 'Exact capability_id + estimate_type join to functional capabilities. Category-specific intake only. Select parameter_name, question and unit for user display; filter requirement REQUIRED and applicable CONDITIONAL. KNOWN fields are already established by category; defaults require confirmation, RANGE optional scenario bounds. Never show technical columns or all fields by default. Validate actual DCP requests with dcp_check_inputs.'\nAS SELECT * FROM VALUES\n"+',\n'.join(values)+'\nAS inputs('+', '.join(COLUMNS)+')\n'
if __name__=='__main__':
 (ROOT/'sql/13_workload_inputs.sql').write_text(generate())
 print('Generated targeted input map:',len(build_rows_map()),'rows for 28 capabilities')
