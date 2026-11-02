"""Verify the installer embeds only the reviewed DCP configuration and source."""
import ast
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

class SetupArtifactTests(unittest.TestCase):
    def assignments(self, path='setup_genie_dcp_agent.py'):
        source = (ROOT/path).read_text()
        tree = ast.parse(source)
        compile(source, path, 'exec')
        return {node.targets[0].id: node.value for node in tree.body
                if isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)}

    def test_installer_embeds_exact_dcp_sql(self):
        values = self.assignments()
        for name, path in [('CALC_SQL','sql/06_dcp_calculator.sql'), ('ENGINE_INPUTS_SQL','sql/12_dcp_engine_inputs.sql')]:
            self.assertEqual(ast.literal_eval(values[name]), (ROOT/path).read_text().strip())
        self.assertNotIn('FUNCTIONS_SQL', values)
        self.assertNotIn('CATALOG_SQL', values)

    def test_agent_payload_and_policy_are_preserved(self):
        call = self.assignments()['SPACE']
        embedded = json.loads(ast.literal_eval(call.args[0]))
        definition = json.loads((ROOT/'genie/space_definition.json').read_text())
        export = json.loads((ROOT/'genie/serialized_space.json').read_text())
        self.assertEqual(embedded, definition)
        self.assertEqual(embedded['serialized_space'], export)
        self.assertEqual(embedded['text_instructions'], [(ROOT/'genie/instructions.md').read_text().strip()])
        self.assertEqual(embedded['tables'], export['data_sources']['tables'])
        self.assertTrue(all(f.rsplit('.',1)[-1].startswith('dcp_') for f in embedded['sql_functions']))
        self.assertEqual({t['identifier'] for t in embedded['tables']}, {'__FQ__.dcp_requests','__FQ__.dcp_rate_card','__FQ__.dcp_engine_input_requirements'})
        allowed = {f.rsplit('.',1)[-1] for f in embedded['sql_functions']} | {t['identifier'].rsplit('.',1)[-1] for t in embedded['tables']}
        for example in embedded['example_question_sqls']:
            for sql in example['sql']:
                self.assertLessEqual(set(re.findall(r'__FQ__\.(\w+)', sql.replace('`',''))), allowed)
        self.assertEqual(export['instructions']['join_specs'], [])

    def test_rate_refresh_source_and_seed_are_shared(self):
        source = (ROOT/'calculator/rate_refresh.py').read_text()
        seed = json.loads((ROOT/'calculator/rate_registry_seed.json').read_text())
        for path in ['setup_genie_dcp_agent.py','notebooks/09_load_rate_card.py']:
            self.assertIn(source, (ROOT/path).read_text())
            call = self.assignments(path)['SEED']
            self.assertEqual(json.loads(ast.literal_eval(call.args[0])), seed)
