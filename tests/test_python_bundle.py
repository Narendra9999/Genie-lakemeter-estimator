import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "calculator"))
from python_bundle import build_loader, cache_key, load_bundle


class PythonBundleTests(unittest.TestCase):
    def setUp(self):
        self.bundle = load_bundle(ROOT / "calculator" / "bundle_manifest.json", ROOT)
        self.key = cache_key(self.bundle)

    def tearDown(self):
        for name in ("dcp_estimator", "dcp_adapter", "dcp_helper"):
            sys.modules.pop(name, None)
        for name in list(sys.modules):
            if name.startswith(("dcp_estimator_", "dcp_adapter_", "dcp_helper_")):
                sys.modules.pop(name, None)

    def test_cache_key_changes_with_module_source_and_manifest(self):
        changed = copy.deepcopy(self.bundle)
        changed["modules"][0]["source"] += "\n# changed\n"
        self.assertNotEqual(self.key, cache_key(changed))
        changed = copy.deepcopy(self.bundle)
        changed["manifest_bytes"] += b" "
        self.assertNotEqual(self.key, cache_key(changed))

    def test_embedded_loader_runs_adapter_check_and_estimate(self):
        namespace = {}
        exec(build_loader(self.bundle, self.key), namespace)
        estimator = sys.modules["dcp_estimator_" + self.key]
        adapter = sys.modules["dcp_adapter_" + self.key]
        missing = {
            "use_case_id": "ADHOC",
            "scenarios": ["LOW", "EXPECTED", "HIGH"],
            "processing": [{"component_id": "NEW-C01", "workload_type": "SCHEDULED_BATCH", "runtime_hours_expected": 2}],
        }
        check = json.loads(adapter.run_check(estimator, missing, None, False))
        self.assertEqual(check["status"], "NEEDS_INPUT")
        self.assertTrue(any(row["field"] == "runs_per_month" for row in check["issues"]))
        result = json.loads(adapter.run(estimator, missing, {"rows": []}, named=False))
        self.assertEqual(result["status"], "NEEDS_INPUT")
        self.assertEqual(result["totals"], [])

        intake = adapter.intake_questions(estimator)
        storage = {row[1]: row for row in intake if row[0] == "storage"}
        self.assertIn("s3_write_requests_per_file", storage)
        self.assertEqual(storage["s3_write_requests_per_file"][3], "DEFAULT")
        self.assertIn("days_per_month", storage)
        self.assertEqual(storage["days_per_month"][3], "DEFAULT")

        one_time = {
            "use_case_id": "ONE-TIME-TEST",
            "use_case_name": "One-time test",
            "scenarios": ["LOW", "EXPECTED", "HIGH"],
            "one_time_model_jobs": [{
                "component_id": "NEW-INDEX", "model": "gpt-5-4", "description": "Initial indexing",
                "input_tokens_low": 1000, "input_tokens_high": 1000,
                "output_tokens_low": 100, "output_tokens_high": 100,
            }],
            "_areas": {area: "NOT_APPLICABLE" for area in ("processing", "sql", "transfer", "storage", "model_endpoints")},
        }
        one_time_check = json.loads(adapter.run_check(estimator, one_time, None, False))
        self.assertEqual(one_time_check["status"], "READY")

        sample = json.loads((ROOT / "samples" / "requests" / "SEND_US01.json").read_text())
        rates = json.loads((ROOT / "calculator" / "rate_registry_seed.json").read_text())
        result = json.loads(adapter.run(estimator, sample, rates, named=True))
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["totals"])

    def test_manifest_order_loads_helper_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "helper.py").write_text("VALUE = 41\n")
            (root / "estimator.py").write_text("import dcp_helper\nVALUE = dcp_helper.VALUE + 1\n")
            (root / "adapter.py").write_text("RESULT = 'loaded'\n")
            manifest = {
                "modules": [
                    {"name": "dcp_helper", "path": "helper.py"},
                    {"name": "dcp_estimator", "path": "estimator.py"},
                    {"name": "dcp_adapter", "path": "adapter.py"},
                ]
            }
            path = root / "bundle.json"
            path.write_text(json.dumps(manifest))
            bundle = load_bundle(path, root)
            key = cache_key(bundle)
            exec(build_loader(bundle, key), {})
            self.assertEqual(sys.modules["dcp_estimator_" + key].VALUE, 42)
            self.assertEqual(sys.modules["dcp_adapter_" + key].RESULT, "loaded")

    def test_rejects_uc_delimiters_and_invalid_python(self):
        for source, expected in (("VALUE = '$$'\n", "cannot contain"), ("VALUE = '''x'''\n", "cannot contain"), ("def broken(:\n", "invalid syntax")):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "estimator.py").write_text(source)
                (root / "adapter.py").write_text("VALUE = 1\n")
                path = root / "bundle.json"
                path.write_text(json.dumps({"modules": [
                    {"name": "dcp_estimator", "path": "estimator.py"},
                    {"name": "dcp_adapter", "path": "adapter.py"},
                ]}))
                with self.assertRaisesRegex((ValueError, SyntaxError), expected):
                    load_bundle(path, root)


if __name__ == "__main__":
    unittest.main()
