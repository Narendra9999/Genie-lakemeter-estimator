"""Exercise the embedded adapter used by UC, including default provenance."""
import copy
import json
from pathlib import Path
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'calculator'))
from python_bundle import build_loader, cache_key, load_bundle

class PlanningDefaultsTests(unittest.TestCase):
    def setUp(self):
        bundle = load_bundle(ROOT / 'calculator/bundle_manifest.json', ROOT)
        key = cache_key(bundle)
        exec(build_loader(bundle, key), {})
        self.a = sys.modules['dcp_adapter_' + key]
        self.m = sys.modules['dcp_estimator_' + key]
        self.rates = json.loads((ROOT / 'calculator/rate_registry_seed.json').read_text())
        self.request = {'processing': [{'workload_type': 'SCHEDULED_BATCH'}],
                        '_areas': {area: 'NOT_APPLICABLE' for area in self.a.AREAS if area != 'processing'}}

    def test_missing_usage_calculates_with_low_confidence_and_audit_trail(self):
        original = copy.deepcopy(self.request)
        self.assertEqual(json.loads(self.a.run_check(self.m, self.request))['status'], 'READY')
        result = json.loads(self.a.run(self.m, self.request, self.rates, named=False))
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['confidence']['rating'], 'LOW')
        self.assertIn('runs_per_month = 30', result['confidence']['reason'])
        self.assertTrue(any('runtime_hours_expected = 2' in x for x in result['assumptions']))
        self.assertIn('LOW confidence', result['summary'])
        self.assertTrue(all(x['recurring_monthly_usd'] > 0 for x in result['totals']))
        self.assertEqual(self.request, original)

    def test_generated_ids_avoid_all_supplied_ids_and_do_not_reduce_confidence(self):
        request = {'processing': [{'workload_type': 'SCHEDULED_BATCH'},
                                  {'component_id': 'PROCESSING-01', 'name': 'Customer name'}],
                   'sql': {'component_id': 'PROCESSING-02'}}
        d, defaults = self.a.prepare_defaults(self.m, request)
        self.assertEqual(d['processing'][0]['component_id'], 'PROCESSING-03')
        self.assertEqual(d['processing'][1]['component_id'], 'PROCESSING-01')
        self.assertEqual(d['processing'][1]['name'], 'Customer name')
        self.assertTrue(all(not row['affects_cost'] for row in defaults if row['field'].endswith('component_id')))

    def test_null_invalid_and_supplied_range_values_are_not_replaced(self):
        for value in (None, -2, '2'):
            request = copy.deepcopy(self.request)
            request['processing'][0]['runtime_hours_expected'] = value
            d, _ = self.a.prepare_defaults(self.m, request)
            self.assertEqual(d['processing'][0]['runtime_hours_expected'], value)
            self.assertEqual(json.loads(self.a.run_check(self.m, request))['status'], 'NEEDS_INPUT')
        request = copy.deepcopy(self.request)
        request['processing'][0].update(runtime_hours_low=.5, runtime_hours_high=2)
        d, _ = self.a.prepare_defaults(self.m, request)
        self.assertEqual(d['processing'][0]['runtime_hours_expected'], 2)
        self.assertEqual(d['processing'][0]['runtime_hours_low'], .5)
        self.assertEqual(d['processing'][0]['runtime_hours_high'], 2)

    def test_each_engine_has_complete_baselines_but_model_and_workload_need_selection(self):
        request = {'processing': [{'workload_type': 'SCHEDULED_BATCH'}], 'sql': {}, 'transfer': {}, 'storage': {},
                   'model_endpoints': [{'model': 'gpt-5.4-nano'}],
                   'one_time_model_jobs': [{'model': 'gpt-5.4-nano'}]}
        self.assertEqual(json.loads(self.a.run_check(self.m, request))['status'], 'READY')
        result = json.loads(self.a.run(self.m, request, self.rates, named=False))
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['confidence']['rating'], 'LOW')
        for area in self.a.REQUIRED_DEFAULTS:
            self.assertTrue(any('Default applied: ' + area in s for s in result['assumptions']))
        for area in ('processing', 'model_endpoints'):
            request[area] = [{}]
            self.assertEqual(json.loads(self.a.run_check(self.m, request))['status'], 'NEEDS_INPUT')

    def test_no_default_components_for_inactive_or_unconfirmed_scope(self):
        request = {'sql': {}, '_areas': {'processing': 'NOT_APPLICABLE'}}
        d, _ = self.a.prepare_defaults(self.m, request)
        self.assertNotIn('processing', d)
        self.assertEqual(d['_areas'], request['_areas'])
        self.assertEqual(json.loads(self.a.run_check(self.m, request))['status'], 'NEEDS_INPUT')

    def test_user_quantities_and_overrides_take_precedence(self):
        request = copy.deepcopy(self.request)
        request['processing'][0].update(runtime_hours_low=1, runtime_hours_expected=2,
                                        runtime_hours_high=3, runs_per_month=20)
        d, defaults = self.a.prepare_defaults(self.m, request)
        self.assertEqual(d['processing'][0]['runs_per_month'], 20)
        self.assertFalse(any(x['field'].endswith('runs_per_month') or 'runtime_hours' in x['field'] for x in defaults))
        request['processing'][0]['component_id'] = 'SUPPLIED-01'
        result = json.loads(self.a.run(self.m, request, self.rates,
                                      {'processing[SUPPLIED-01].runs_per_month': 5}, named=False))
        self.assertEqual(result['status'], 'OK')
        built = self.a.build_request(self.m, request, {'processing[SUPPLIED-01].runs_per_month': 5})
        self.assertEqual(built.processing[0].runs_per_month, 5)

    def test_source_planning_defaults_are_reused(self):
        request = copy.deepcopy(self.request)
        request['processing'][0]['workload_type'] = 'EVENT_DRIVEN_BATCH'
        d, defaults = self.a.prepare_defaults(self.m, request)
        self.assertNotIn('runs_per_month', d['processing'][0])
        rows = {x['field']: x['value'] for x in defaults}
        self.assertEqual(rows['assumptions.unconfirmed_event_runs_per_month'],
                         self.m.PlanningAssumptions().unconfirmed_event_runs_per_month)
        self.assertEqual(rows['assumptions.batch_dbu_per_hour'], self.m.PlanningAssumptions().batch_dbu_per_hour)
        d, defaults = self.a.prepare_defaults(self.m, {'sql': {}, 'transfer': {}, 'storage': {}})
        self.assertEqual(d['sql']['working_days_per_month'], self.m.SqlReporting.__dataclass_fields__['working_days_per_month'].default)
        self.assertEqual(d['transfer']['transfer_days_per_month'], self.m.DataTransfer().transfer_days_per_month)
        self.assertEqual(d['storage']['s3_write_requests_per_file'], self.m.AwsStorage().s3_write_requests_per_file)

    def test_model_expected_retains_source_midpoint_and_assumption_overrides(self):
        d, defaults = self.a.prepare_defaults(self.m, {
            'model_endpoints': [{'model': 'gpt-5.4-nano', 'input_tokens_low': 20, 'input_tokens_high': 80}],
            'processing': [{'workload_type': 'SCHEDULED_BATCH'}], 'assumptions': {'batch_dbu_per_hour': 7}})
        self.assertNotIn('input_tokens_expected', d['model_endpoints'][0])
        self.assertEqual(self.m._token_value('EXPECTED', 20, None, 80)[0], 50)
        self.assertEqual(d['assumptions']['batch_dbu_per_hour'], 7)
        self.assertFalse(any(x['field'] == 'assumptions.batch_dbu_per_hour' for x in defaults))

    def test_identity_defaults_alone_preserve_high_confidence(self):
        request = copy.deepcopy(self.request)
        request['processing'][0].update(quantity=1, runs_per_month=30, runtime_hours_low=1,
                                        runtime_hours_expected=2, runtime_hours_high=3,
                                        dbu_per_hour={'LOW': 1, 'EXPECTED': 1, 'HIGH': 1},
                                        dbu_per_hour_evidence=self.m.Evidence.CONFIRMED)
        result = json.loads(self.a.run(self.m, request, self.rates, named=False))
        self.assertEqual(result['confidence']['rating'], 'HIGH')

