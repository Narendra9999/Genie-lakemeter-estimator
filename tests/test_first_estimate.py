"""First estimates keep strict maths and mark every unresolved/deferred area unpriced."""
import copy
import json
from pathlib import Path
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'calculator'))
from python_bundle import build_loader, cache_key, load_bundle

class FirstEstimateTests(unittest.TestCase):
    def setUp(self):
        b=load_bundle(ROOT/'calculator/bundle_manifest.json', ROOT);k=cache_key(b)
        exec(build_loader(b,k),{})
        self.a=sys.modules['dcp_adapter_'+k];self.m=sys.modules['dcp_estimator_'+k]
        self.rates=json.loads((ROOT/'calculator/rate_registry_seed.json').read_text())

    def run_request(self, request):
        return json.loads(self.a.run(self.m, request, self.rates, named=False))

    def test_first_sql_estimate_prices_known_area_and_keeps_unknown_scope_pending(self):
        req={'_first_estimate':True,'sql':{'concurrent_users':10}}
        original=copy.deepcopy(req)
        self.assertEqual(json.loads(self.a.run_check(self.m,req))['status'],'READY')
        result=self.run_request(req)
        self.assertEqual(result['status'],'OK')
        self.assertEqual(result['confidence']['rating'],'LOW')
        self.assertEqual({a['state'] for a in result['areas']},{'INCLUDED','UNKNOWN'})
        self.assertEqual(len(result['pending']),5)
        for t in result['totals']:
            self.assertGreater(t['recurring_monthly_usd'],0)
            self.assertEqual(t['status'],'PARTIAL_PENDING_INPUTS')
            self.assertEqual(t['pending_items'],5)
        self.assertTrue(any('Monthly USD' in n for n in result['notes']))
        self.assertEqual(req,original)
        strict=self.run_request({'sql':{'concurrent_users':10}})
        self.assertEqual(strict['status'],'NEEDS_INPUT')
        self.assertEqual(strict['totals'],[])

    def test_first_mode_does_not_relax_invalid_values_or_empty_workload(self):
        for req in ({'_first_estimate':True,'sql':{'concurrent_users':-1}},
                    {'_first_estimate':True,'sql':{'concurrent_users':None}},
                    {'_first_estimate':'yes','sql':{'concurrent_users':10}},
                    {'_first_estimate':True}):
            result=self.run_request(req)
            self.assertEqual(result['status'],'NEEDS_INPUT')
            self.assertEqual(result['totals'],[])

    def test_explicit_inactive_states_are_preserved(self):
        req={'_first_estimate':True,'sql':{'concurrent_users':10},'_areas':{'storage':'EXCLUDED','transfer':'NOT_APPLICABLE'}}
        result=self.run_request(req)
        areas={a['area']:a['state'] for a in result['areas']}
        self.assertEqual(areas['storage'],'EXCLUDED')
        self.assertEqual(areas['transfer'],'NOT_APPLICABLE')
        self.assertEqual(len(result['pending']),3)

    def test_deferred_same_engine_component_marks_subtotal_incomplete(self):
        req={'_first_estimate':True,'sql':{'concurrent_users':10},
             '_areas':{a:'NOT_APPLICABLE' for a in self.a.AREAS if a!='sql'},
             'metadata':{'deferred_inputs':[{'area':'sql','component_id':'BAD-SQL','reason':'invalid concurrency',
                                             'original_inputs':{'concurrent_users':-1}}]}}
        result=self.run_request(req)
        self.assertEqual(result['status'],'OK')
        self.assertTrue(all(t['status']=='PARTIAL_PENDING_INPUTS' and t['pending_items']==1 for t in result['totals']))
        self.assertIn('BAD-SQL',result['confidence']['reason'])

    def test_monthly_model_token_aggregate_matches_source_formula(self):
        req={'_first_estimate':True, 'model_days_per_month':1,
             'model_endpoints':[{'model':'gpt-5.4-nano','calls_per_day':1,
                'input_tokens_low':1000000,'input_tokens_expected':1000000,'input_tokens_high':1000000,
                'output_tokens_low':100000,'output_tokens_expected':100000,'output_tokens_high':100000}],
             '_areas':{a:'NOT_APPLICABLE' for a in self.a.AREAS if a!='model_endpoints'}}
        result=self.run_request(req)
        self.assertEqual(result['status'],'OK')
        built=self.a.build_request(self.m,req)
        source=self.m.estimate(built,self.a.build_pricing(self.m,self.rates))
        self.assertEqual(result['totals'],self.a.shape(self.m,source,self.rates)['totals'])
        self.assertTrue(all(t['recurring_monthly_usd']>0 for t in result['totals']))
        self.assertEqual(len({t['recurring_monthly_usd'] for t in result['totals']}),1)
        self.assertEqual(result['pending'],[])

    def test_chart_uses_exact_result_amounts_and_omits_unknown_values(self):
        totals=[{'scenario':s,'recurring_monthly_usd':v} for s,v in [('LOW',10),('EXPECTED',20),('HIGH',30)]]
        chart=self.a.range_chart(totals)
        for v in ('$10.00','$20.00','$30.00'): self.assertIn(v,chart)
        totals[1]['recurring_monthly_usd']=None
        self.assertIsNone(self.a.range_chart(totals))

    def test_null_and_blank_identities_are_generated_without_affecting_user_ids(self):
        request={'_first_estimate':True, 'use_case_id':None, 'use_case_name':'',
                 'sql':{'component_id':'  ', 'name':None, 'concurrent_users':10}}
        result=self.run_request(request)
        self.assertEqual(result['status'],'OK')
        self.assertEqual(result['header']['use_case_id'],'ADHOC')
        self.assertEqual(result['line_items'][0]['component_id'],'SQL-01')
