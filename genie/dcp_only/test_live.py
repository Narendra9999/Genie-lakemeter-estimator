"""Conversation smoke checks; preserve complete responses for review."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import time
ROOT=Path(__file__).resolve().parent

def main():
 p=argparse.ArgumentParser();p.add_argument('--profile',required=True);a=p.parse_args()
 sid=json.loads((ROOT/'deployment.json').read_text())['space_id']
 evidence=ROOT/'validation';evidence.mkdir(exist_ok=True)
 def cli(*args):
  r=subprocess.run(['databricks','genie',*args,'--profile',a.profile,'--output','json'],capture_output=True,text=True)
  if r.returncode:raise RuntimeError(r.stderr)
  return json.loads(r.stdout)
 def wait(label,msg):
  cid=msg['conversation_id'];mid=msg.get('message_id') or msg.get('id')
  for _ in range(120):
   result=cli('get-message',sid,cid,mid)
   if result['status'] in ['COMPLETED','FAILED','CANCELLED']:
    (evidence/(label+'.json')).write_text(json.dumps(result,indent=2)+'\n')
    queries=[x['query']['query'] for x in result.get('attachments',[]) if 'query' in x]
    texts=[x['text']['content'] for x in result.get('attachments',[]) if 'text' in x]
    print(json.dumps(dict(label=label,status=result['status'],queries=queries,text=texts,error=result.get('error'))),flush=True)
    return result
   time.sleep(2)
  raise TimeoutError(label)
 def run(label,question):return wait(label,cli('start-conversation','--no-wait',sid,question))
 incomplete={'use_case_id':'LIVE_BATCH','use_case_name':'Live batch','scenarios':['EXPECTED'],'processing':[{'component_id':'B1','name':'Batch','workload_type':'SCHEDULED_BATCH','runtime_hours_expected':2}]}
 with ThreadPoolExecutor(max_workers=2) as pool:
  vague=pool.submit(run,'vague','I need a cost estimate. What do you need from me?')
  partial=pool.submit(run,'missing','Estimate this workload: '+json.dumps(incomplete))
  vague.result();first=partial.result()
 complete=json.loads((ROOT/'sample_request.json').read_text());complete['use_case_id']='LIVE_BATCH';complete['use_case_name']='Live batch'
 question='20 runs per month, EXPECTED scenario only. Processing is included; SQL, DataSync, storage/KMS, recurring model endpoints and one-time model jobs are NOT_APPLICABLE. Here is the updated JSON. Validate it, then calculate when ready: '+json.dumps(complete)
 second=wait('confirmed',cli('create-message','--no-wait',sid,first['conversation_id'],question))
 wait('breakdown',cli('create-message','--no-wait',sid,second['conversation_id'],'Break down that same estimate by engine, separating monthly and one-time costs.'))
if __name__=='__main__':main()
