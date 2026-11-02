"""Check the actual multi-step Genie Agent API; save all events for review."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import urllib.request
from databricks.sdk import WorkspaceClient
ROOT=Path(__file__).resolve().parent

def main():
 p=argparse.ArgumentParser();p.add_argument('--profile',required=True);a=p.parse_args()
 w=WorkspaceClient(profile=a.profile)
 sid=json.loads((ROOT/'deployment.json').read_text())['space_id']
 evidence=ROOT/'validation_agent';evidence.mkdir(exist_ok=True)
 def run(label,text,cid=None):
  payload={'model':'genie-agent','input':[{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}],'stream':True}
  if cid:payload['conversation_id']=cid
  req=urllib.request.Request(w.config.host+'/api/2.0/genie/agents/'+sid+'/responses',data=json.dumps(payload).encode(),headers={**w.config.authenticate(),'Content-Type':'application/json'})
  events=[];completed=None
  with urllib.request.urlopen(req,timeout=240) as response:
   for line in response:
    if line.startswith(b'data:'):
     event=json.loads(line[5:]);events.append(event)
     if event.get('type')=='response.completed':completed=event['response']
  (evidence/(label+'.json')).write_text(json.dumps(events,indent=2)+'\n')
  if not completed:raise RuntimeError('No completion: '+label)
  print(json.dumps({'label':label,'conversation_id':completed['conversation_id'],'output':completed['output']}),flush=True)
  return completed['conversation_id']
 incomplete={'use_case_id':'LIVE_BATCH','use_case_name':'Live batch','scenarios':['EXPECTED'],'processing':[{'component_id':'B1','name':'Batch','workload_type':'SCHEDULED_BATCH','runtime_hours_expected':2}]}
 with ThreadPoolExecutor(max_workers=2) as pool:
  vague=pool.submit(run,'vague','I need a cost estimate. What do you need from me?')
  missing=pool.submit(run,'missing','Please estimate this JSON: '+json.dumps(incomplete))
  vague.result();cid=missing.result()
 complete=json.loads((ROOT/'sample_request.json').read_text());complete['use_case_id']='LIVE_BATCH';complete['use_case_name']='Live batch'
 cid=run('confirmed','20 runs per month. EXPECTED only. All other engines and both model sections are not applicable. Validate then calculate this updated request: '+json.dumps(complete),cid)
 run('breakdown','Break down the same estimate by engine. Separate monthly and one-time charges and explain the pricing basis and assumptions.',cid)
if __name__=='__main__':main()
