import json
import sqlite3
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'calculator'))
import build_workload_inputs as b

class WorkloadInputTests(unittest.TestCase):
 def setUp(self):
  self.rows=b.build_rows_map()
 def required(self,cap):
  return {r['parameter_name'] for r in self.rows if r['capability_id']==cap and r['requirement']=='REQUIRED'}
 def test_batch_and_streaming_have_distinct_required_inputs(self):
  self.assertEqual(self.required('DCP_SCHEDULED_BATCH'),{'runtime_hours_expected','runs_per_month'})
  self.assertEqual(self.required('DCP_CONTINUOUS_STREAMING'),{'streaming_mode','latency_requirement'})
  self.assertFalse(any(r['parameter_name'].startswith('runtime_hours') or r['parameter_name']=='runs_per_month' for r in self.rows if r['capability_id']=='DCP_CONTINUOUS_STREAMING'))
 def test_every_capability_mapped_and_lakemeter_contract_preserved(self):
  caps=json.loads((ROOT/'genie/catalog/functional_capabilities.json').read_text())
  self.assertEqual({r['capability_id'] for r in self.rows},{c['capability_id'] for c in caps})
  self.assertEqual(self.required('LAKEMETER_LAKEBASE'),{'p_cloud','p_region','p_tier','p_capacity_units','p_ha_nodes','p_days_per_month'})
  self.assertEqual(b.generate(),(ROOT/'sql/13_workload_inputs.sql').read_text())
 def test_joined_example_queries_return_only_selected_inputs(self):
  db=sqlite3.connect(':memory:')
  db.execute('CREATE TABLE cost_estimator_workload_inputs ('+', '.join(b.COLUMNS)+')')
  db.executemany('INSERT INTO cost_estimator_workload_inputs VALUES ('+','.join('?' for _ in b.COLUMNS)+')',[[r[k] for k in b.COLUMNS] for r in self.rows])
  db.execute('CREATE TABLE cost_estimator_functional_capabilities (capability_id, category, estimate_type, capability_kind, sort_order)')
  caps=json.loads((ROOT/'genie/catalog/functional_capabilities.json').read_text())
  db.executemany('INSERT INTO cost_estimator_functional_capabilities VALUES (?,?,?,?,?)',[[c[k] for k in ['capability_id','category','estimate_type','capability_kind','sort_order']] for c in caps])
  space=json.loads((ROOT/'genie/space_definition.json').read_text())
  checked=0
  for ex in space['example_question_sqls']:
   for sql in ex['sql']:
    if 'JOIN __FQ__.cost_estimator_workload_inputs' not in sql:continue
    self.assertNotIn('SELECT *',sql.upper())
    cur=db.execute(sql.replace('__FQ__.',''))
    rows=cur.fetchall()
    self.assertTrue(rows,ex['question'])
    self.assertLessEqual(len(cur.description),4)
    checked+=1
  self.assertGreaterEqual(checked,6)
  db.close()
