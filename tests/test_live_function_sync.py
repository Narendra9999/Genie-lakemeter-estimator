"""Guard function contracts and comments captured from the user's live agent."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

def normalize(text):
    return re.sub(r"\s+", " ", text).strip().lower()

def split_parameters(text):
    parts, start, depth, quoted, i = [], 0, 0, False, 0
    while i < len(text):
        c = text[i]
        if c == "'":
            if quoted and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            quoted = not quoted
        if not quoted:
            if c in "(<": depth += 1
            elif c in ")>": depth -= 1
            elif c == "," and depth == 0:
                parts.append(text[start:i].strip())
                start = i + 1
        i += 1
    if text[start:].strip(): parts.append(text[start:].strip())
    return parts

class LiveFunctionSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads((ROOT / "genie/live_function_definitions.json").read_text())
        cls.local = {}
        for path in sorted((ROOT / "sql").glob("*.sql")):
            for stmt in path.read_text().split("\n-- @@\n"):
                name = re.search(r"CREATE OR REPLACE FUNCTION __FQ__\.(\w+)\(", stmt)
                if name: cls.local[name[1]] = stmt

    def test_all_live_function_signatures_and_return_types_match(self):
        for name, live in self.snapshot.items():
            with self.subTest(function=name):
                stmt = self.local[name]
                start = stmt.index("(", stmt.index("CREATE OR REPLACE FUNCTION")) + 1
                signature = stmt[start:stmt.index("RETURNS")].strip().removesuffix(")").strip()
                expected = []
                for param in live.get("input_params", {}).get("parameters", []):
                    text = param["name"] + " " + param["type_text"]
                    if "parameter_default" in param: text += " DEFAULT " + param["parameter_default"]
                    expected.append(text)
                self.assertEqual(list(map(normalize, split_parameters(signature))), list(map(normalize, expected)))
                result = re.search(r"RETURNS\s+(.*?)(?=\n(?:COMMENT|LANGUAGE)|\nRETURN\b)", stmt, re.S)[1].strip()
                if result.startswith("TABLE"): result = result[len("TABLE"):]
                self.assertEqual(re.sub(r"\s+", "", result).lower(), re.sub(r"\s+", "", live["full_data_type"]).lower())

    def test_all_live_function_bodies_and_comments_match(self):
        for name, live in self.snapshot.items():
            with self.subTest(function=name):
                stmt = self.local[name]
                comment = re.search(r"\bCOMMENT\s+'((?:''|[^'])*)'", stmt)
                self.assertEqual(comment[1].replace("''", "'"), live.get("comment", ""))
                tail = stmt[comment.end():]
                if re.search(r"\bAS\s+\$\$", tail):
                    body = re.split(r"\bAS\s+\$\$", tail, maxsplit=1)[1].rsplit("$$", 1)[0].strip()
                else:
                    body = re.split(r"\bRETURN\s+", tail, maxsplit=1)[1].strip().rstrip(";")
                expected = live["routine_definition"].strip()
                # Unity Catalog wraps this scalar subquery in an additional redundant pair.
                if name in ("get_dbu_price", "dcp_rates_json"):
                    while body.startswith("(") and body.endswith(")"): body = body[1:-1].strip()
                    while expected.startswith("(") and expected.endswith(")"): expected = expected[1:-1].strip()
                self.assertEqual(normalize(body), normalize(expected))

    def test_registered_and_example_functions_exist_in_snapshot(self):
        space = json.loads((ROOT / "genie/serialized_space.json").read_text())
        for function in space["instructions"]["sql_functions"]:
            self.assertIn(function["identifier"].split(".")[-1], self.snapshot)
        for example in space["instructions"]["example_question_sqls"]:
            for sql in example["sql"]:
                for name in re.findall(r"__FQ__\.(\w+)\s*\(", sql):
                    self.assertIn(name, self.snapshot)
