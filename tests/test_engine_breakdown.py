import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "calculator"))
from breakdown_queries import breakdown_sql, request_argument
from python_bundle import build_loader, cache_key, load_bundle

class EngineBreakdownTests(unittest.TestCase):
    def aggregate(self, rows):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE items (scenario TEXT, engine TEXT, recurrence TEXT, treatment TEXT, cost_usd REAL)")
        db.executemany("INSERT INTO items VALUES (?,?,?,?,?)", rows)
        sql = breakdown_sql("p_request => 'test'").replace("__FQ__.dcp_calc_line_items(p_request => 'test')", "items")
        return db.execute(sql).fetchall()

    def test_five_engine_json_reconciles_unrounded_costs(self):
        bundle = load_bundle(ROOT / "calculator/bundle_manifest.json", ROOT)
        key = cache_key(bundle)
        exec(build_loader(bundle, key), {})
        adapter = sys.modules["dcp_adapter_" + key]
        estimator = sys.modules["dcp_estimator_" + key]
        request = json.loads((ROOT / "tests/fixtures/five_engine_request.json").read_text())
        self.assertEqual(json.loads(adapter.run_check(estimator, request, None, False))["status"], "READY")
        rates = json.loads((ROOT / "calculator/rate_registry_seed.json").read_text())
        result = json.loads(adapter.run(estimator, request, rates, named=False))
        self.assertEqual(result["status"], "OK")
        lines = result["line_items"]
        grouped = self.aggregate([(x["scenario"], x["engine"], x["recurrence"], x["treatment"], x["cost_usd"]) for x in lines])
        self.assertEqual({x[1] for x in grouped}, {"DATA_PROCESSING", "SQL_REPORTING", "DATA_TRANSFER", "AWS_STORAGE", "MODEL_API"})
        for recurrence in {x[2] for x in grouped}:
            self.assertAlmostEqual(sum(x[3] for x in grouped if x[2] == recurrence), sum(x["cost_usd"] for x in lines if x["recurrence"] == recurrence))
        self.assertEqual(len([x for x in grouped if x[1] == "DATA_TRANSFER"]), 2)

    def test_pending_and_alternative_scenarios_stay_separate(self):
        rows = [("LOW", "MODEL_API", "RECURRING_MONTHLY", "PENDING", None),
                ("HIGH", "MODEL_API", "RECURRING_MONTHLY", "PENDING", None),
                ("HIGH", "MODEL_API", "RECURRING_MONTHLY", "INCLUDED", 2.12345)]
        grouped = self.aggregate(rows)
        low = next(x for x in grouped if x[0] == "LOW")
        high = next(x for x in grouped if x[0] == "HIGH")
        self.assertIsNone(low[3])
        self.assertEqual(high[3], 2.12345)
        for row in grouped:
            self.assertEqual(row[4:], (1, "PARTIAL_PENDING_INPUTS"))

    def test_json_argument_preserves_values_and_escapes_apostrophes(self):
        request = {"use_case_id": "customer's original", "scenarios": ["LOW", "HIGH"]}
        argument = request_argument(request)
        escaped = argument[len("p_request => '"):-1]
        self.assertEqual(json.loads(escaped.replace("''", "'")), request)
        self.assertIn("customer''s original", argument)
