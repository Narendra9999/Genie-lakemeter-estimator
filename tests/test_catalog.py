import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "calculator"))
from build_catalog import generate_sql, load_catalog, validate_catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog(ROOT / "genie" / "catalog" / "estimator_catalog.json")

    def test_all_entrypoints_exist_and_statuses_are_explicit(self):
        validate_catalog(
            self.catalog,
            ROOT / "sql" / "02_functions.sql",
            ROOT / "sql" / "06_dcp_calculator.sql",
            ROOT / "sql" / "08_workload_sizing.sql",
        )

    def test_catalog_function_lists_categories_and_bases(self):
        sql = generate_sql(self.catalog)
        self.assertIn("CREATE OR REPLACE FUNCTION __FQ__.estimator_catalog", sql)
        self.assertIn("implementation_state STRING", sql)
        self.assertIn("validation_state STRING", sql)
        self.assertIn("pricing_basis STRING", sql)
        self.assertIn("NOT_PORTED", sql)
        self.assertIn("DCP_MODEL_ENDPOINTS", sql)
        self.assertIn("DCP_ONE_TIME_MODEL_JOBS", sql)

    def test_dcp_request_builder_supports_one_time_jobs(self):
        sql = (ROOT / "sql" / "06_dcp_calculator.sql").read_text()
        self.assertIn("p_one_time_model STRING DEFAULT NULL", sql)
        self.assertIn("p_one_time_input_tokens_low DOUBLE DEFAULT NULL", sql)
        self.assertIn("p_sql_component_id STRING DEFAULT NULL", sql)
        self.assertIn("p_model_component_id STRING DEFAULT NULL", sql)
        self.assertIn("p_s3_write_requests_per_file", sql)
        self.assertIn("p_kms_requests_per_encrypted_file", sql)
        self.assertIn("p_one_time_input_tokens_low IS NOT NULL", sql)
        self.assertIn("'one_time_model_jobs'", sql)
        self.assertIn("'ADHOC-ONE-TIME-01'", sql)

    def test_duplicate_product_ids_fail_validation(self):
        broken = json.loads(json.dumps(self.catalog))
        broken["products"].append(dict(broken["products"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate product id"):
            validate_catalog(
                broken,
                ROOT / "sql" / "02_functions.sql",
                ROOT / "sql" / "06_dcp_calculator.sql",
                ROOT / "sql" / "08_workload_sizing.sql",
            )

    def test_unavailable_products_require_reason_and_no_entrypoint(self):
        broken = json.loads(json.dumps(self.catalog))
        row = next(x for x in broken["products"] if x["implementation_state"] == "NOT_PORTED")
        row["status_note"] = ""
        with self.assertRaisesRegex(ValueError, "requires a status_note"):
            validate_catalog(
                broken,
                ROOT / "sql" / "02_functions.sql",
                ROOT / "sql" / "06_dcp_calculator.sql",
                ROOT / "sql" / "08_workload_sizing.sql",
            )

    def test_agent_instructions_fit_global_context_guidance(self):
        text = (ROOT / "genie" / "instructions.md").read_text().strip()
        self.assertLessEqual(len(text), 2000)
        self.assertIn("Understand → Discover → Gather Facts → Check Coverage", text)
        self.assertIn("estimator_catalog", text)

    def test_workbook_green_yellow_rows_have_explicit_test_targets(self):
        coverage = json.loads((ROOT / "benchmark" / "coverage_map.json").read_text())
        ids = [x["id"] for x in coverage["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        for row in coverage["items"]:
            self.assertTrue(row.get("current_state"), row["id"])
            if row["status"] in {"GREEN", "YELLOW"}:
                self.assertTrue(row.get("planned_test_ids"), row["id"])
            if row["status"] == "GRAY":
                self.assertEqual(row.get("planned_test_ids"), [], row["id"])
        self.assertIn("not a claim", coverage["test_ids_are"])

    def test_setup_registers_catalog_and_injects_short_policy(self):
        space = json.loads((ROOT / "genie" / "space_definition.json").read_text())
        self.assertIn("__FQ__.estimator_catalog", space["sql_functions"])
        self.assertEqual(space["text_instructions"], [(ROOT / "genie" / "instructions.md").read_text().strip()])
        setup = (ROOT / "setup_genie_dcp_agent.py").read_text()
        self.assertIn("Product coverage catalog created", setup)
        match = re.search(r"^SPACE = json.loads\((.*)\)$", setup, re.M)
        self.assertIsNotNone(match)
        embedded_space = json.loads(ast.literal_eval(match.group(1)))
        self.assertIn("Understand → Discover", embedded_space["text_instructions"][0])
        self.assertIn("__FQ__.estimator_catalog", embedded_space["sql_functions"])


if __name__ == "__main__":
    unittest.main()
