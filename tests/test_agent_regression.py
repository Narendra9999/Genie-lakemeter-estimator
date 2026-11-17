import copy
import json
from pathlib import Path
import sys
import unittest
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'regression'))
sys.path.insert(0, str(ROOT/'calculator'))
from agent_regression import calculation_requests, check_prepared, check_translation_intake, no_calls, prepared_json, verify_monthly_totals, evaluate_case, write_reports
from python_bundle import build_loader, cache_key, load_bundle


def response(text='', queries=()):
    return {'output': [{'type': 'message', 'content': [{'text': text}]}]+[
        {'type': 'function_call', 'arguments': json.dumps({'sql': sql})} for sql in queries]}


class AgentRegressionTests(unittest.TestCase):
    def setUp(self):
        self.request = json.loads((ROOT/'tests/fixtures/translation_prepared.json').read_text())

    def test_prepared_request_retains_user_facts_and_all_inactive_areas(self):
        draft = response('Please confirm:\n```json\n'+json.dumps(self.request)+'\n```')
        check_prepared(prepared_json(draft), self.request)
        for mutation in ('id', 'inactive', 'runtime', 'dbu', 'inactive_id'):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(self.request)
                if mutation == 'id': bad['use_case_id'] = 'GENERATED_ID'
                if mutation == 'inactive': bad['_areas']['storage'] = 'UNCONFIRMED'
                if mutation == 'runtime': bad['processing'][0]['runtime_hours_expected'] = 7
                if mutation == 'dbu': bad['processing'][0]['dbu_per_hour']['HIGH'] = 1
                if mutation == 'inactive_id': bad.pop('metadata')
                with self.assertRaises(AssertionError): check_prepared(bad, self.request)

    def test_premature_function_calls_fail_even_without_visible_costs(self):
        no_calls(response('What is the expected runtime?'))
        with self.assertRaises(AssertionError):
            no_calls(response('Need a name.', ['SELECT * FROM c.s.dcp_check_inputs()']))

    def test_intake_rejects_cost_figures_without_a_table_or_tool_call(self):
        for text in ['Your estimate is $27.00/month.', 'The cost is 27 USD.', 'USD 27.00 per month.']:
            with self.subTest(text=text):
                with self.assertRaises(AssertionError): no_calls(response(text))
        no_calls(response('Pending costs must not be treated as $0. What is the runtime?'))

    def test_checker_and_totals_must_use_identical_confirmed_json_in_order(self):
        def sql(name, request):
            return "SELECT * FROM c.s."+name+"(p_request => '"+json.dumps(request).replace("'", "''")+"')"
        good = [sql('dcp_check_inputs', self.request), sql('dcp_calc_totals', self.request)]
        self.assertEqual(calculation_requests(response(queries=good)), self.request)
        changed = copy.deepcopy(self.request)
        changed['processing'][0]['runs_per_month'] = 99
        for queries in [good[::-1], good[1:], [good[0], sql('dcp_calc_totals', changed)]]:
            with self.subTest(queries=queries):
                with self.assertRaises(AssertionError): calculation_requests(response(queries=queries))

    def test_explicit_inactive_scope_must_not_be_asked_again(self):
        text = 'NEW-WORKLOAD-ID-TEST / NEW-C01: what business name? Does 2 DBU/hour apply to all scenarios?'
        check_translation_intake(response(text))
        with self.assertRaises(AssertionError):
            check_translation_intake(response(text+'\nAre SQL and the remaining areas not applicable?'))

    def financial_response(self, low='13.50', expected='27.00', high='54.00', extra=''):
        text = ('| Scenario | Recurring monthly USD |\n| --- | --- |\n'
                '| LOW | $'+low+' |\n| EXPECTED | '+expected+' USD |\n| HIGH | '+high+' |\n'
                + extra + '\nConfidence: LOW')
        return response(text)

    def test_financial_checks_match_amounts_to_scenarios(self):
        rows = [['LOW', '13.50', 'OK'], ['EXPECTED', '27.00', 'OK'], ['HIGH', '54.00', 'OK']]
        verify_monthly_totals(self.financial_response(), rows)
        # The same amounts appearing under the wrong scenarios must fail.
        with self.assertRaises(AssertionError):
            verify_monthly_totals(self.financial_response(low='54.00', high='13.50'), rows)

    def test_financial_checks_reject_missing_pending_and_extra_scenarios(self):
        rows = [['LOW', '13.50', 'OK'], ['EXPECTED', '27.00', 'OK'], ['HIGH', '54.00', 'OK']]
        for bad in [self.financial_response(high='PENDING'),
                    self.financial_response(extra='| STRESS | 99.00 |\n'),
                    response('Confidence LOW. Some numbers: 13.50, 27.00, 54.00')]:
            with self.subTest(answer=bad):
                with self.assertRaises(AssertionError): verify_monthly_totals(bad, rows)
        with self.assertRaises(AssertionError):
            verify_monthly_totals(self.financial_response(), [['LOW', None, 'NEEDS_INPUT'], *rows[1:]])

    def test_completed_estimate_must_include_confidence(self):
        rows = [['LOW', '13.50', 'OK'], ['EXPECTED', '27.00', 'OK'], ['HIGH', '54.00', 'OK']]
        bad = self.financial_response()
        bad['output'][0]['content'][0]['text'] = bad['output'][0]['content'][0]['text'].replace('Confidence: LOW', '')
        with self.assertRaises(AssertionError): verify_monthly_totals(bad, rows)

    def test_intake_rejects_raw_validation_tables(self):
        with self.assertRaises(AssertionError):
            no_calls(response('| Field | Status |\n| --- | --- |\n| runtime | MISSING |'))

    def test_behavior_failures_and_operational_errors_are_reported_separately(self):
        def regression(): raise AssertionError('Premature function call')
        def unavailable(): raise TimeoutError('Workspace timeout')
        results = [evaluate_case('valid', 1, lambda: None),
                   evaluate_case('missing', 1, regression), evaluate_case('network', 1, unavailable)]
        self.assertEqual([r['status'] for r in results], ['PASS', 'FAIL', 'ERROR'])
        with tempfile.TemporaryDirectory() as folder:
            write_reports(Path(folder), results)
            suite = ET.parse(Path(folder)/'junit.xml').getroot()
            self.assertEqual(suite.attrib['tests'], '3')
            self.assertEqual(suite.attrib['failures'], '1')
            self.assertEqual(suite.attrib['errors'], '1')
            self.assertEqual(json.loads((Path(folder)/'results.json').read_text()), results)
            self.assertIn('Premature function call', (Path(folder)/'REPORT.md').read_text())

    def test_repeats_retain_failures_instead_of_overwriting_them(self):
        def failed(): raise AssertionError('first run failed')
        results = [evaluate_case('missing', 1, failed), evaluate_case('missing', 2, lambda: None)]
        with tempfile.TemporaryDirectory() as folder:
            write_reports(Path(folder), results)
            saved = json.loads((Path(folder)/'results.json').read_text())
            self.assertEqual([(r['iteration'], r['status']) for r in saved], [(1, 'FAIL'), (2, 'PASS')])

    def test_strict_calculator_rejects_alias_json_without_conversational_translation(self):
        bundle = load_bundle(ROOT/'calculator/bundle_manifest.json', ROOT)
        key = cache_key(bundle)
        exec(build_loader(bundle, key), {})
        adapter, estimator = sys.modules['dcp_adapter_'+key], sys.modules['dcp_estimator_'+key]
        original = json.loads((ROOT/'tests/fixtures/translation_request.json').read_text())
        rejected = json.loads(adapter.run_check(estimator, original, None, False))
        self.assertEqual(rejected['status'], 'NEEDS_INPUT')
        accepted = json.loads(adapter.run_check(estimator, self.request, None, False))
        self.assertEqual(accepted['status'], 'READY')
        for mutation in ('negative', 'missing-runtime', 'unknown-area'):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(self.request)
                if mutation == 'negative': bad['processing'][0]['runtime_hours_expected'] = -1
                if mutation == 'missing-runtime': bad['processing'][0]['runtime_hours_expected'] = None
                if mutation == 'unknown-area': bad['_areas'].pop('transfer')
                check = json.loads(adapter.run_check(estimator, bad, None, False))
                self.assertEqual(check['status'], 'NEEDS_INPUT')
                result = json.loads(adapter.run(estimator, bad, {'rows': []}, named=False))
                self.assertEqual(result['status'], 'NEEDS_INPUT')
                self.assertEqual(result['totals'], [])

    def test_horizontal_scenario_tables_compare_actual_monthly_costs(self):
        rows = [['LOW','13.50','OK'], ['EXPECTED','27.00','OK'], ['HIGH','54.00','OK']]
        good = response('| Cost | LOW | EXPECTED | HIGH |\n|---|---|---|---|\n| Recurring monthly | $13.50 | $27.00 | $54.00 |\nConfidence LOW')
        verify_monthly_totals(good, rows)
        bad = response('| Cost | LOW | EXPECTED | HIGH |\n|---|---|---|---|\n| Recurring monthly | $54.00 | $27.00 | $13.50 |\nConfidence LOW')
        with self.assertRaises(AssertionError): verify_monthly_totals(bad, rows)

    def test_monthly_chart_amounts_are_verified_by_scenario(self):
        rows = [['LOW','13.50','OK'], ['EXPECTED','27.00','OK'], ['HIGH','54.00','OK']]
        chart = '```\nMonthly USD — priced-area subtotal\nLOW █ $13.50\nEXPECTED ██ $27.00\nHIGH ███ $54.00\n```\nConfidence LOW'
        verify_monthly_totals(response(chart), rows)
        for bad in (chart.replace('LOW █ $13.50', 'LOW █ $54.00'),
                    chart.replace('Monthly USD', 'Annual USD'),
                    chart.replace('HIGH ███ $54.00', '')):
            with self.assertRaises(AssertionError): verify_monthly_totals(response(bad), rows)
