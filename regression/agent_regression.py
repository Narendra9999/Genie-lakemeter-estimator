"""Opt-in Agent Mode regression; never changes agent configuration or UC objects."""
import argparse
import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import hashlib
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def answer_text(response):
    return '\n'.join(c['text'] for item in response.get('output', [])
                     if item.get('type') == 'message'
                     for c in item.get('content', []) if 'text' in c)


def tool_sql(response):
    return [json.loads(item['arguments']).get('sql', '')
            for item in response.get('output', []) if item.get('type') == 'function_call']


def prepared_json(response):
    blocks = re.findall(r'```(?:json)?\s*\n(.*?)```', answer_text(response), re.S)
    values = []
    for block in blocks:
        try:
            value = json.loads(block)
        except ValueError:
            continue
        if isinstance(value, dict) and 'processing' in value:
            values.append(value)
    require(len(values) == 1, 'Expected exactly one prepared workload JSON block')
    return values[0]


def check_prepared(request, expected):
    """Compare user facts semantically; allow additional documented defaults."""
    for field in ('use_case_id', 'use_case_name', 'scenarios', '_areas'):
        require(request.get(field) == expected[field], 'Changed or lost '+field)
    require(len(request.get('processing', [])) == 1, 'Processing component was lost or duplicated')
    for key, value in expected['processing'][0].items():
        require(request['processing'][0].get(key) == value, 'Changed or lost processing.'+key)
    ids = request.get('metadata', {}).get('original_inactive_component_ids', {})
    require(ids.get('sql') == 'NEW-SQL', 'Lost inactive SQL identifier NEW-SQL')
    for area in ('sql', 'transfer', 'storage', 'model_endpoints', 'one_time_model_jobs'):
        require(request.get(area) in (None, []), 'Inactive area has calculator inputs: '+area)
    require(not set(request).intersection({'request_id', 'datasync', 's3_kms', 'model_api'}),
            'Untranslated aliases remain in calculator request')


def no_calls(response):
    require(answer_text(response).strip(), 'Missing explanatory answer')
    require(not tool_sql(response), 'Function called while inputs or confirmation were unresolved')
    money = re.findall(r'(?i)\$\s*([+-]?\d[\d,]*(?:\.\d+)?)|([+-]?\d[\d,]*(?:\.\d+)?)\s*(?:USD|dollars)\b|\bUSD\s*([+-]?\d[\d,]*(?:\.\d+)?)', answer_text(response))
    for groups in money:
        amount = Decimal(next(value for value in groups if value).replace(',', ''))
        require(amount == 0, 'Dollar figures presented while inputs or confirmation were unresolved')

    require(not re.search(r'(?m)^\s*\|?\s*:?-{3,}:?\s*\|', answer_text(response)),
            'Result table presented before inputs/confirmation resolved')


def check_translation_intake(response):
    no_calls(response)
    text = answer_text(response)
    require('NEW-WORKLOAD-ID-TEST' in text and 'NEW-C01' in text,
            'Supplied IDs not acknowledged')
    require(re.search(r'(?i)(?:name|business)', text) and re.search(r'(?i)DBU', text),
            'Missing name/DBU clarification')
    # Topic checks on explicit questions, not an exact-response text snapshot.
    for line in text.splitlines():
        if '?' in line:
            require(not re.search(r'(?i)\b(?:SQL|DataSync|S3|KMS|model.api|areas?)\b|not.applicable', line),
                    'Asked again about an explicitly settled area: '+line)


def calculation_requests(response):
    found = []
    for sql in tool_sql(response):
        match = re.search(r'\bdcp_(check_inputs|calc_totals)`?\s*\(', sql, re.I)
        if match:
            literal = re.search(r"p_request\s*=>\s*'((?:''|[^'])*)'", sql, re.I)
            require(literal is not None, 'Expected named p_request with the confirmed canonical JSON')
            found.append((match.group(1).lower(), json.loads(literal.group(1).replace("''", "'"))))
        require(not re.search(r"p_\w+\s*=>\s*'NULL'", sql, re.I), 'Passed string NULL')
    require([name for name, _ in found] == ['check_inputs', 'calc_totals'],
            'Expected one input check followed by one totals call')
    require(found[0][1] == found[1][1], 'Input check and calculation used different requests')
    return found[0][1]


CASE_NAMES = (
    'translate-confirm-validate-calculate', 'conflicting-identifiers',
    'invalid-negative-runtime', 'missing-expected-runtime',
    'unknown-area-state', 'conflicting-area-status', 'revised-inputs-require-confirmation',
)


def verify_monthly_totals(response, rows):
    """Match monthly dollars to their scenario, not to arbitrary numbers in prose."""
    text = answer_text(response)
    observed = {}
    headers = None
    for line in text.splitlines():
        if not line.lstrip().startswith('|'):
            headers = None
            continue
        cells = [cell.strip().strip('`*') for cell in line.strip().strip('|').split('|')]
        normalized = [re.sub(r'[_-]', ' ', cell.lower()) for cell in cells]
        if any('scenario' in cell for cell in normalized):
            scenario_index = next(i for i, cell in enumerate(normalized) if 'scenario' in cell)
            monthly = [i for i, cell in enumerate(normalized)
                       if 'monthly' in cell and not any(word in cell for word in ('core', 'dbu', 'databricks', 'aws'))]
            headers = (scenario_index, monthly[0]) if len(monthly) == 1 else None
            continue
        if headers is None or len(cells) <= max(headers):
            continue
        scenario_index, monthly_index = headers
        match = re.fullmatch(r'(LOW|EXPECTED|HIGH|STRESS)(?:\s*\(.*\))?', cells[scenario_index], re.I)
        if not match:
            continue
        scenario = match.group(1).upper()
        amount = re.sub(r'(?i)USD|[$,`*]', '', cells[monthly_index]).strip()
        try:
            value = Decimal(amount)
        except InvalidOperation:
            raise AssertionError('Monthly amount is not numeric for '+scenario)
        require(value.is_finite(), 'Non-finite monthly amount for '+scenario)
        require(scenario not in observed or observed[scenario] == value, 'Conflicting displayed totals for '+scenario)
        observed[scenario] = value
    require({row[0] for row in rows} == {'LOW', 'EXPECTED', 'HIGH'}, 'Missing calculator scenario totals')
    require(set(observed) == {row[0] for row in rows}, 'Displayed scenarios do not match the requested scenarios')
    for scenario, monthly, status in rows:
        require(status == 'OK' and monthly is not None, 'Calculator did not return a complete '+scenario)
        require(observed[scenario] == Decimal(str(monthly)), 'Incorrect monthly amount for '+scenario)
    require(re.search(r'(?i)confidence', text), 'Completed estimate omits confidence')


def evaluate_case(name, iteration, fn):
    started = time.monotonic()
    result = {'case': name, 'iteration': iteration, 'status': 'PASS'}
    try:
        fn()
    except AssertionError as error:
        result.update(status='FAIL', error=str(error), category='behavior')
    except Exception as error:
        result.update(status='ERROR', error=str(error), category='execution')
    result['duration_seconds'] = round(time.monotonic()-started, 3)
    return result


def write_reports(output, results):
    (output/'results.json').write_text(json.dumps(results, indent=2))
    suite = ET.Element('testsuite', name='DCP Agent Mode regression', tests=str(len(results)),
                       failures=str(sum(r['status'] == 'FAIL' for r in results)),
                       errors=str(sum(r['status'] == 'ERROR' for r in results)),
                       time=str(sum(r['duration_seconds'] for r in results)))
    lines = ['# Agent Mode regression results', '', '| Case | Run | Status | Reason |', '| --- | --- | --- | --- |']
    for result in results:
        case = ET.SubElement(suite, 'testcase', name=result['case']+' [run '+str(result['iteration'])+']',
                             classname='dcp.agent_mode', time=str(result['duration_seconds']))
        if result['status'] != 'PASS':
            node = ET.SubElement(case, 'failure' if result['status'] == 'FAIL' else 'error', message=result['error'])
            node.text = result['error']
        reason = result.get('error', '').replace('|', '/').replace('\n', ' ')
        lines.append('| '+result['case']+' | '+str(result['iteration'])+' | '+result['status']+' | '+reason+' |')
    ET.ElementTree(suite).write(output/'junit.xml', encoding='unicode', xml_declaration=True)
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n')


class AgentClient:
    def __init__(self, profile, agent_id, output, timeout):
        from databricks.sdk import WorkspaceClient
        self.w = WorkspaceClient(profile=profile)
        self.agent_id, self.output, self.timeout = agent_id, output, timeout

    def turn(self, label, prompt, conversation_id=None):
        body = {'model': 'genie-agent', 'stream': True, 'input': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': prompt}]}]}
        if conversation_id:
            body['conversation_id'] = conversation_id
        request = urllib.request.Request(
            self.w.config.host+'/api/2.0/genie/agents/'+self.agent_id+'/responses',
            data=json.dumps(body).encode(), headers={**self.w.config.authenticate(), 'Content-Type': 'application/json'})
        events, completed = [], None
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as stream:
                for line in stream:
                    require(time.monotonic()-started < self.timeout, 'Agent response exceeded timeout')
                    if not line.startswith(b'data:'):
                        continue
                    payload = line[5:].strip()
                    if payload == b'[DONE]':
                        continue
                    event = json.loads(payload)
                    events.append(event)
                    require(event.get('type') != 'response.failed', 'Agent failed: '+str(event.get('response', {}).get('error')))
                    if event.get('type') == 'response.completed':
                        completed = event['response']
        finally:
            (self.output/(label+'-events.json')).write_text(json.dumps(events, indent=2))
        require(completed is not None, 'No completed Agent Mode response')
        (self.output/(label+'-response.json')).write_text(json.dumps(completed, indent=2))
        print(label+': response received', flush=True)
        return completed

    def sql(self, statement):
        config = json.loads((self.output/'agent-config.json').read_text())
        started = time.monotonic()
        result = self.w.api_client.do('POST', '/api/2.0/sql/statements', body={
            'warehouse_id': config['warehouse_id'], 'statement': statement, 'wait_timeout': '10s'})
        while result['status']['state'] in ('PENDING', 'RUNNING'):
            require(time.monotonic()-started < self.timeout, 'Direct calculation timed out')
            time.sleep(1)
            result = self.w.api_client.do('GET', '/api/2.0/sql/statements/'+result['statement_id'])
        require(result['status']['state'] == 'SUCCEEDED', 'Direct calculation failed: '+str(result['status']))
        return result


def run(args):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = Path(args.output)/stamp
    output.mkdir(parents=True)
    client = AgentClient(args.profile, args.agent_id, output, args.timeout)
    config = client.w.api_client.do('GET', '/api/2.0/genie/spaces/'+args.agent_id,
                                  query={'include_serialized_space': 'true'})
    (output/'agent-config.json').write_text(json.dumps(config, indent=2))
    manifest = {'profile': args.profile, 'agent_id': args.agent_id, 'mode': 'AGENT',
                'started_at': stamp, 'case_names': args.case or list(CASE_NAMES), 'repeat': args.repeat,
                'configuration_sha256': hashlib.sha256(config['serialized_space'].encode()).hexdigest(),
                'configuration_update_time': config.get('update_time'),
                'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        manifest['git_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        manifest['working_tree_dirty'] = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT))
    except (OSError, subprocess.CalledProcessError):
        manifest['git_commit'] = None
    (output/'run.json').write_text(json.dumps(manifest, indent=2))
    fixture = ROOT/'tests/fixtures'
    original = json.loads((fixture/'translation_request.json').read_text())
    expected = json.loads((fixture/'translation_prepared.json').read_text())
    manifest['fixture_sha256'] = {name: hashlib.sha256((fixture/name).read_bytes()).hexdigest()
                                  for name in ('translation_request.json', 'translation_prepared.json')}
    (output/'run.json').write_text(json.dumps(manifest, indent=2))
    results = []
    prefix = ''

    def turn(label, prompt, cid=None):
        return client.turn(prefix+label, prompt, cid)

    def translation(revise=False):
        expected_request = copy.deepcopy(expected)
        first = turn('translation-0', 'Please estimate this workload: '+json.dumps(original))
        check_translation_intake(first)
        cid = first['conversation_id']
        draft = turn('translation-1', 'Use case name: Daily batch ingestion. Processing component name: Daily batch job. Yes, 2 DBU/hour applies to LOW, EXPECTED and HIGH.', cid)
        no_calls(draft)
        prepared = prepared_json(draft)
        check_prepared(prepared, expected_request)
        require(re.search(r'(?i)confirm|approve', answer_text(draft)), 'Did not ask for prepared-input approval')
        if revise:
            expected_request['processing'][0]['runs_per_month'] = 20
            revised = turn('translation-revised', 'Change the run count to 20 runs per month. Keep all other values. Show the revised prepared inputs before calculation.', cid)
            no_calls(revised)
            prepared = prepared_json(revised)
            check_prepared(prepared, expected_request)
            require(re.search(r'(?i)confirm|approve', answer_text(revised)), 'Revised draft did not request new confirmation')
        final = turn('translation-2', 'I confirm the prepared inputs. Please validate and calculate the estimate.', cid)
        actual = calculation_requests(final)
        check_prepared(actual, expected_request)
        require(actual == prepared, 'Calculated a request different from the confirmed draft')
        totals = next(sql for sql in tool_sql(final) if re.search(r'dcp_calc_totals', sql, re.I))
        identifier = re.search(r'((?:`?[\w-]+`?\.){2}`?dcp_calc_totals`?)\s*\(', totals, re.I)
        require(identifier is not None, 'Cannot identify fully qualified totals function')
        literal = json.dumps(actual).replace("'", "''")
        query = "SELECT scenario, recurring_monthly_usd, request_status FROM "+identifier.group(1)+"(p_request => '"+literal+"')"
        direct = client.sql(query)
        (output/(prefix+'direct-totals.json')).write_text(json.dumps(direct, indent=2))
        verify_monthly_totals(final, direct.get('result', {}).get('data_array', []))

    def blocked(name, request, required_text):
        response = turn(name, 'Please estimate this workload: '+json.dumps(request))
        no_calls(response)
        require(all(re.search(pattern, answer_text(response), re.I) for pattern in required_text),
                'Did not explain the unresolved issue')

    conflict = {**original, 'use_case_id': 'CONFLICTING-ID'}
    invalid = copy.deepcopy(expected)
    invalid['processing'][0]['runtime_hours_expected'] = -1
    missing = copy.deepcopy(expected)
    del missing['processing'][0]['runtime_hours_expected']
    unknown = copy.deepcopy(expected)
    unknown['_areas']['transfer'] = 'UNKNOWN'
    scope_conflict = copy.deepcopy(original)
    scope_conflict['_areas'] = {'transfer': 'INCLUDED'}
    cases = {
        'translate-confirm-validate-calculate': translation,
        'revised-inputs-require-confirmation': lambda: translation(revise=True),
        'conflicting-identifiers': lambda: blocked('conflict', conflict, ['NEW-WORKLOAD-ID-TEST', 'CONFLICTING-ID']),
        'invalid-negative-runtime': lambda: blocked('invalid-runtime', invalid, ['runtime', r'-1|negative|non.negative']),
        'missing-expected-runtime': lambda: blocked('missing-runtime', missing, ['runtime', 'expected']),
        'unknown-area-state': lambda: blocked('unknown-area', unknown, ['transfer|DataSync', 'unknown|confirm|scope|included']),
        'conflicting-area-status': lambda: blocked('scope-conflict', scope_conflict, ['transfer|DataSync', 'conflict|included|not.applicable']),
    }
    for iteration in range(1, args.repeat+1):
        for name in args.case or CASE_NAMES:
            prefix = name+'-run'+str(iteration)+'-'
            result = evaluate_case(name, iteration, cases[name])
            results.append(result)
            print(name+' [run '+str(iteration)+']: '+result['status']+(' — '+result['error'] if 'error' in result else ''), flush=True)
            write_reports(output, results)
    after = client.w.api_client.do('GET', '/api/2.0/genie/spaces/'+args.agent_id,
                                 query={'include_serialized_space': 'true'})
    manifest['configuration_changed_during_run'] = after['serialized_space'] != config['serialized_space']
    (output/'run.json').write_text(json.dumps(manifest, indent=2))
    if manifest['configuration_changed_during_run']:
        print('WARNING: live configuration changed during the run; review before comparing results.', flush=True)
        return 1
    print('Evidence: '+str(output), flush=True)
    return 1 if any(result['status'] != 'PASS' for result in results) else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, help='Explicit Databricks profile')
    parser.add_argument('--agent-id', required=True)
    parser.add_argument('--output', default=str(ROOT/'benchmark/results/agent-regression'))
    parser.add_argument('--timeout', type=int, default=300, help='Seconds per agent turn or SQL statement')
    parser.add_argument('--case', action='append', choices=CASE_NAMES, help='Select a case; repeat flag for multiple cases')
    parser.add_argument('--repeat', type=int, default=1, help='Independent runs per selected case, never retries')
    arguments = parser.parse_args()
    if not re.fullmatch(r'[0-9a-fA-F]{32}', arguments.agent_id):
        parser.error('--agent-id must be 32 hexadecimal characters')
    if arguments.repeat < 1 or arguments.timeout < 1:
        parser.error('--repeat and --timeout must be positive')
    sys.exit(run(arguments))
