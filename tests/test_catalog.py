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
        self.assertIn("upper(trim(p_category)) IN ('', 'NULL'", sql)
        self.assertIn("NOT_PORTED", sql)
        self.assertIn("DCP_MODEL_ENDPOINTS", sql)
        self.assertIn("DCP_ONE_TIME_MODEL_JOBS", sql)

    def test_dcp_request_builder_supports_one_time_jobs(self):
        sql = (ROOT / "sql" / "06_dcp_calculator.sql").read_text()
        self.assertIn("p_one_time_model STRING DEFAULT NULL", sql)
        self.assertIn("p_one_time_input_tokens_low DOUBLE DEFAULT NULL", sql)
        self.assertIn("p_sql_component_id STRING DEFAULT NULL", sql)
        self.assertIn("p_model_component_id STRING DEFAULT NULL", sql)
        self.assertIn("p_region STRING DEFAULT 'US_EAST_1'", sql)
        self.assertIn("p_working_days_per_month INT DEFAULT NULL", sql)
        self.assertIn("'working_days_per_month', coalesce(p_working_days_per_month, 22)", sql)
        self.assertIn("p_transfer_days_per_month INT DEFAULT NULL", sql)
        self.assertIn("'transfer_days_per_month', coalesce(p_transfer_days_per_month, 22)", sql)
        self.assertIn("p_storage_days_per_month INT DEFAULT NULL", sql)
        self.assertIn("'days_per_month', coalesce(p_storage_days_per_month, 30)", sql)
        self.assertIn("p_s3_write_requests_per_file DOUBLE DEFAULT NULL", sql)
        self.assertIn("'s3_write_requests_per_file', coalesce(p_s3_write_requests_per_file, 1.0)", sql)
        self.assertIn("p_model_days_per_month INT DEFAULT 30", sql)
        self.assertIn("p_request_assumptions_confirmed BOOLEAN DEFAULT NULL", sql)
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

    def test_agent_instructions_preserve_live_policy(self):
        text = (ROOT / "genie" / "instructions.md").read_text().strip()
        exported = json.loads((ROOT / "genie" / "serialized_space.json").read_text())
        self.assertEqual(text, "\n\n".join("".join(t["content"]) for t in exported["instructions"]["text_instructions"]))
        self.assertIn("DCP cost estimates only", text)
        self.assertIn("Never decide an area state yourself", text)

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

    def test_setup_preserves_live_registration_and_policy(self):
        space = json.loads((ROOT / "genie/space_definition.json").read_text())
        exported = json.loads((ROOT / "genie/serialized_space.json").read_text())
        self.assertEqual(space["sql_functions"],
                         [f["identifier"] for f in exported["instructions"]["sql_functions"]])
        self.assertEqual(space["sample_questions"],
                         [q["question"] for q in exported["config"]["sample_questions"]])
        self.assertEqual(space["example_question_sqls"],
                         [{k: v for k, v in q.items() if k != "id"}
                          for q in exported["instructions"]["example_question_sqls"]])
        self.assertEqual(space["text_instructions"], [(ROOT / "genie/instructions.md").read_text().strip()])
        setup = (ROOT / "setup_genie_dcp_agent.py").read_text()
        match = re.search(r"^SPACE = json.loads\((.*)\)$", setup, re.M)
        self.assertIsNotNone(match)
        embedded_space = json.loads(ast.literal_eval(match.group(1)))
        self.assertEqual(embedded_space, space)


if __name__ == "__main__":
    unittest.main()
