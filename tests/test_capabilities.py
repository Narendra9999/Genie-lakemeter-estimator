import json
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'calculator'))
from build_capabilities import generate

class CapabilitiesTests(unittest.TestCase):
 def test_all_mappings_have_valid_products_and_unique_identity(self):
  rows=json.loads((ROOT/'genie/catalog/functional_capabilities.json').read_text())
  products={p['id'] for p in json.loads((ROOT/'genie/catalog/estimator_catalog.json').read_text())['products']}
  self.assertEqual(len(rows),28)
  self.assertEqual(len({r['capability_id'] for r in rows}),28)
  counts={}
  for r in rows:
   counts[(r['estimate_type'],r['capability_kind'])]=counts.get((r['estimate_type'],r['capability_kind']),0)+1
   self.assertTrue(set(r['catalog_product_ids']) <= products)
  self.assertEqual(counts,{('DCP','WORKLOAD_ESTIMATE'):9,('Lakemeter','PRODUCT_ESTIMATE'):14,('Lakemeter','WORKLOAD_SIZING'):5})
 def test_generated_table_and_genie_attachment_stay_in_sync(self):
  sql=generate()
  self.assertEqual(sql,(ROOT/'sql/11_functional_capabilities.sql').read_text())
  self.assertIn('validation_state',sql)
  self.assertIn('UNTESTED',sql)
  space=json.loads((ROOT/'genie/space_definition.json').read_text())
  ident='__FQ__.cost_estimator_functional_capabilities'
  self.assertEqual(sum(t['identifier']==ident for t in space['tables']),1)
  self.assertTrue(any(ident in ' '.join(e['sql']) for e in space['example_question_sqls']))
