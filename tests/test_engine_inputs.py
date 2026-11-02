import dataclasses
import json
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'calculator'))
import build_engine_inputs as b
class EngineInputTests(unittest.TestCase):
 def setUp(self):self.rows=b.build_rows();self.by={r['input_id']:r for r in self.rows}
 def test_schema_fields_all_present_once(self):
  expected={area+'.'+f.name for area,cls,_ in b.adapter.SECTIONS for f in dataclasses.fields(getattr(b.model,cls))}
  self.assertEqual(set(self.by),expected)
  self.assertEqual(len(self.rows),len(expected))
  self.assertEqual(len({r['engine'] for r in self.rows}),5)
 def test_conditional_rules_and_token_units(self):
  self.assertIn('not used for CONTINUOUS_STREAMING',self.by['processing.runs_per_month']['applies_when'])
  self.assertEqual(self.by['processing.runtime_hours_low']['requirement'],'SCENARIO_BOUND')
  self.assertEqual(self.by['model_endpoints.input_tokens_low']['unit'],'tokens/call')
  self.assertEqual(self.by['one_time_model_jobs.input_tokens_low']['unit'],'tokens, job total')
  self.assertEqual(self.by['one_time_model_jobs.api_calls']['input_role'],'CONTEXT_ONLY')
  self.assertEqual(self.by['sql.total_users']['input_role'],'CONTEXT_ONLY')
 def test_generation_and_examples(self):
  self.assertEqual(b.generate(),(ROOT/'sql/12_dcp_engine_inputs.sql').read_text())
  for r in self.rows:
   json.loads(r['default_value_json']);json.loads(r['example_value_json'])
  space=json.loads((ROOT/'genie/space_definition.json').read_text())
  self.assertEqual(sum(t['identifier']=='__FQ__.dcp_engine_input_requirements' for t in space['tables']),1)
