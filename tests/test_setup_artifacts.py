"""Ensure the standalone installer embeds the reviewed source artifacts."""
import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class SetupArtifactTests(unittest.TestCase):
    def assignments(self):
        tree = ast.parse((ROOT / "setup_genie_dcp_agent.py").read_text())
        return {node.targets[0].id: node.value for node in tree.body
                if isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)}

    def test_embedded_reference_sql_matches_sources(self):
        values = self.assignments()
        for name, file in (("CAPABILITIES_SQL", "11_functional_capabilities.sql"),
                           ("ENGINE_INPUTS_SQL", "12_dcp_engine_inputs.sql"),
                           ("CATALOG_SQL", "10_product_catalog.sql"),
                           ("WORKLOAD_INPUTS_SQL", "13_workload_inputs.sql")):
            self.assertEqual(ast.literal_eval(values[name]),
                             (ROOT / "sql" / file).read_text().strip())
        setup = (ROOT / "setup_genie_dcp_agent.py").read_text()
        self.assertIn('spark.sql(CAPABILITIES_SQL.replace("__FQ__", FQ))', setup)
        self.assertIn('spark.sql(ENGINE_INPUTS_SQL.replace("__FQ__", FQ))', setup)

    def test_embedded_agent_matches_definition_and_policy(self):
        call = self.assignments()["SPACE"]
        self.assertIsInstance(call, ast.Call)
        embedded = json.loads(ast.literal_eval(call.args[0]))
        definition = json.loads((ROOT / "genie/space_definition.json").read_text())
        self.assertEqual(embedded, definition)
        self.assertEqual(embedded["text_instructions"],
                         [(ROOT / "genie/instructions.md").read_text().strip()])
        tables = {t["identifier"] for t in embedded["tables"]}
        self.assertTrue({"__FQ__.cost_estimator_functional_capabilities",
                         "__FQ__.dcp_engine_input_requirements"} <= tables)


    def test_full_export_is_embedded_without_field_or_asset_id_loss(self):
        exported = json.loads((ROOT / "genie/serialized_space.json").read_text())
        definition = json.loads((ROOT / "genie/space_definition.json").read_text())
        call = self.assignments()["SPACE"]
        embedded = json.loads(ast.literal_eval(call.args[0]))
        self.assertEqual(exported, definition["serialized_space"])
        self.assertEqual(exported, embedded["serialized_space"])
        self.assertEqual(exported["data_sources"]["tables"], definition["tables"])
        self.assertEqual([e["identifier"] for e in exported["instructions"]["sql_functions"]],
                         definition["sql_functions"])
        self.assertEqual([{k: v for k, v in e.items() if k != "id"} for e in
                          exported["instructions"]["example_question_sqls"]],
                         definition["example_question_sqls"])
        self.assertIn('serialized = D["serialized_space"]',
                      (ROOT / "setup_genie_dcp_agent.py").read_text())
