"""DCP Genie benchmark: 20 numeric regression cases plus routing/coverage cases Q21-Q27.

The first 20 cases compare Genie with the deployed DCP calculator and the local v4.0.2 engine. The added cases check
area-gate behavior, exact scenarios and component IDs, raw-JSON parity, missing-input follow-up, explicit DCP routing,
catalog discovery, unsupported coverage, and a six-area guided intake. Route-only cases are not calculator-parity tests.
The latest recorded full run is tracked in benchmark/coverage_map.json; no run is performed during build or unit tests.

Usage: python3 run_benchmark.py <profile> <warehouse_id> <space_id> <catalog.schema> <out_dir> [question numbers]
"""
import json, os, re, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "notebooks")); sys.path.insert(0, os.path.join(HERE, "..", "calculator"))
import dataclasses
import dcp_estimator as calc, dcp_adapter as adapter

PROFILE, WH, SID, FQ, OUT = sys.argv[1:6]
ONLY = {int(x) for x in sys.argv[6].split(",")} if len(sys.argv) > 6 else None
GOVERNED = {"as_of": calc.DEFAULT_AS_OF, "source": "calculator registry (governed account rates)",
            "rows": [dataclasses.asdict(r) for r in calc.DEFAULT_RATE_REGISTRY]}
SC4 = list(calc.SCENARIOS_ALL)


def api(method, path, body=None):
    a = ["databricks", "api", method, path, "--profile", PROFILE] + (["--json", json.dumps(body)] if body else [])
    out = subprocess.run(a, capture_output=True, text=True)
    return json.loads(out.stdout or "{}")


def sql(stmt):
    r = api("post", "/api/2.0/sql/statements", {"warehouse_id": WH, "statement": stmt, "wait_timeout": "50s", "on_wait_timeout": "CONTINUE"})
    while r.get("status", {}).get("state") in ("PENDING", "RUNNING"):
        time.sleep(2); r = api("get", f"/api/2.0/sql/statements/{r['statement_id']}")
    if r.get("status", {}).get("state") != "SUCCEEDED":
        raise RuntimeError(f"{r.get('status')} :: {stmt[:200]}")
    return r.get("result", {}).get("data_array") or []


def q(s): return "'" + s.replace("'", "''") + "'"


def calculator(case):
    args = []
    if case.get("use_case"): args.append(f"p_use_case => {q(case['use_case'])}")
    if case.get("request") is not None: args.append(f"p_request => {q(json.dumps(case['request']))}")
    if case.get("overrides"): args.append(f"p_overrides => {q(json.dumps(case['overrides']))}")
    rows = sql(f"SELECT result FROM {FQ}.dcp_calc_run({', '.join(args)})")
    return json.loads(rows[0][0]) if rows else None


def engine_governed(case):
    req = case.get("request")
    if case.get("use_case"):
        rows = sql(f"SELECT request_json FROM {FQ}.dcp_requests WHERE upper(use_case_id)=upper({q(case['use_case'])}) OR upper(use_case_name)=upper({q(case['use_case'])})")
        if not rows: return None
        req = json.loads(rows[0][0])
    return json.loads(adapter.run(calc, req, GOVERNED, case.get("overrides")))


def ask_genie(question, cid=None):
    if cid:
        r = api("post", f"/api/2.0/genie/spaces/{SID}/conversations/{cid}/messages", {"content": question})
        mid = r.get("message_id") or r["id"]
    else:
        r = api("post", f"/api/2.0/genie/spaces/{SID}/start-conversation", {"content": question})
        cid, mid = r["conversation_id"], r["message_id"]
    for _ in range(120):
        m = api("get", f"/api/2.0/genie/spaces/{SID}/conversations/{cid}/messages/{mid}")
        if m.get("status") in ("COMPLETED", "FAILED", "CANCELLED"): break
        time.sleep(3)
    sqls, rows, texts = [], [], []
    for a in m.get("attachments") or []:
        if "text" in a: texts.append(a["text"].get("content", ""))
        if "query" in a:
            sqls.append(a["query"].get("query", ""))
            res = api("get", f"/api/2.0/genie/spaces/{SID}/conversations/{cid}/messages/{mid}/attachments/{a['attachment_id']}/query-result")
            st = res.get("statement_response", {})
            cols = [c["name"] for c in st.get("manifest", {}).get("schema", {}).get("columns", [])]
            for row in st.get("result", {}).get("data_array") or []:
                rows.append(dict(zip(cols, row)))
    return {"status": m.get("status"), "conversation_id": cid, "sql": sqls, "rows": rows, "text": "\n".join(texts)}


NUM = re.compile(r"-?\$?\d[\d,]*\.?\d*")
USD = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
def dollars_in(text):
    """Dollar amounts the agent wrote (a cost shown to the user), ignoring numbers such as 'S3' or '2 hours'."""
    return sorted({round(float(t.replace("$", "").replace(",", "").strip()), 2) for t in USD.findall(text)})
def numbers_in(agent):
    vals = set()
    blob = agent["text"] + " " + " ".join(str(v) for r in agent["rows"] for v in r.values() if v is not None)
    for tok in NUM.findall(blob):
        try: vals.add(round(float(tok.replace("$", "").replace(",", "")), 2))
        except ValueError: pass
    return vals


def T(res, sc, k): return next(t[k] for t in res["totals"] if t["scenario"] == sc)
def L(res, sc, cid, k="cost_usd"): return next(l[k] for l in res["line_items"] if l["scenario"] == sc and l["component_id"] == cid)
def money(x): return f"${x:,.2f}"

# Each case: inputs, what the calculator answer is (figures + required words), and the workbook v4.0.2 expectation.
ADHOC = dict(use_case_id="ADHOC", use_case_name="Ad-hoc", scenarios=["LOW", "EXPECTED", "HIGH"])
AREAS = ("processing", "sql", "transfer", "storage", "model_endpoints", "one_time_model_jobs")
FOLLOWUP = "The other calculator areas are not applicable."
def with_areas(req):
    """The calculator reference for an ad-hoc question: every area without inputs is NOT_APPLICABLE (the follow-up answer)."""
    return {**req, "_areas": {a: "NOT_APPLICABLE" for a in AREAS if not req.get(a)}}
def P(**kw): return {"component_id": "C01", "name": "Workload", **kw}
ROUTING_REQ = {
    **ADHOC,
    "use_case_id": "NEW-WORKLOAD-01",
    "use_case_name": "Identifier and scenario preservation",
    "processing": [{"component_id": "NEW-C01", "name": "Nightly batch", "workload_type": "SCHEDULED_BATCH",
                    "runtime_hours_low": 1, "runtime_hours_expected": 1, "runtime_hours_high": 1, "runs_per_month": 20}],
    "sql": {"component_id": "NEW-SQL", "name": "Reporting", "concurrent_users": 8, "working_days_per_month": 20},
}
JSON_ROUTING_REQ = {**ROUTING_REQ, "_areas": {a: "NOT_APPLICABLE" for a in AREAS if a not in ("processing", "sql")}}
FULL_GUIDED_REQ = {
    **ADHOC,
    "use_case_id": "GUIDED-SIX-AREA",
    "use_case_name": "Six-area guided estimate",
    "processing": [{"component_id": "GUIDED-C01", "name": "Scheduled batch", "workload_type": "SCHEDULED_BATCH",
                    "runtime_hours_low": 1, "runtime_hours_expected": 1, "runtime_hours_high": 1, "runs_per_month": 20}],
    "sql": {"component_id": "GUIDED-SQL", "name": "SQL reporting", "concurrent_users": 10, "working_days_per_month": 20},
    "transfer": {"daily_gb_low": 5, "daily_gb_expected": 5, "daily_gb_high": 5, "transfer_days_per_month": 20, "initial_transfer_gb": 500},
    "storage": {"retained_tb": 1, "files_per_day": 100, "encrypted_files_per_day": 10, "kms_keys": 1, "days_per_month": 30,
                "s3_write_requests_per_file": 1, "s3_read_requests_per_file": 2, "kms_requests_per_encrypted_file": 1,
                "request_assumptions_confirmed": True},
    "model_endpoints": [{"component_id": "GUIDED-MODEL", "model": "gpt-5-4", "role": "Model API", "calls_per_day": 100,
                         "input_tokens_low": 1000, "input_tokens_expected": 1000, "input_tokens_high": 1000,
                         "output_tokens_low": 100, "output_tokens_expected": 100, "output_tokens_high": 100}],
    "one_time_model_jobs": [{"component_id": "GUIDED-INDEX", "model": "gpt-5-4", "description": "Initial indexing",
                             "input_tokens_low": 1000, "input_tokens_high": 1000,
                             "output_tokens_low": 100, "output_tokens_high": 100, "api_calls": 100}],
}
GUIDED_ANSWER = ("All six areas are included. Processing: scheduled batch, 1 hour per run, 20 runs/month. "
                 "SQL: 10 concurrent users, 20 working days. DataSync: 5 GB/day for 20 days and a 500 GB one-time migration. "
                 "S3/KMS: 1 TB retained, 100 files/day, 10 encrypted files/day, 1 KMS key, 1 write and 2 reads per file, "
                 "1 KMS request per encrypted file; these request values are confirmed. Recurring model API: gpt-5-4, "
                 "100 calls/day, 1,000 input and 100 output tokens/call. One-time indexing: gpt-5-4, 1,000 input and 100 output tokens, 100 API calls. "
                 "Return LOW, EXPECTED and HIGH only; show line items and the area states.")
CASES = [
 dict(n=1, q="What is the High core monthly estimate for SEND_US01?", use_case="SEND_US01",
      figures=lambda r: [T(r, "HIGH", "core_monthly_usd")], v4=[531.78]),
 dict(n=2, q="Give the core monthly range Low / Expected / High / Stress for SEND_US01.", use_case="SEND_US01",
      figures=lambda r: [T(r, s, "core_monthly_usd") for s in SC4], v4=[146.76, 292.60, 531.78, 808.95], words=["sensitivity"]),
 dict(n=3, q="What is the High all-in monthly cost for SEND_US01 at 10 TB?", use_case="SEND_US01",
      figures=lambda r: [T(r, "HIGH", "recurring_monthly_usd")], v4=[762.81]),
 dict(n=4, q="What is the High first-year total for SEND_US01 at 10 TB, and what is the one-time part?", use_case="SEND_US01",
      figures=lambda r: [T(r, "HIGH", "first_year_usd"), T(r, "HIGH", "one_time_usd")], v4=[9278.76, 125.00],
      words_regex=[r"unrounded.*line items"]),
 dict(n=5, q="If SEND retains 8 TB and migrates 8,000 GB, what are the High monthly and first-year figures?", use_case="SEND_US01",
      overrides={"retained_storage_tb": 8, "migration_gb": 8000},
      figures=lambda r: [T(r, "HIGH", "recurring_monthly_usd"), T(r, "HIGH", "first_year_usd"), T(r, "HIGH", "one_time_usd")], v4=[716.81, 8701.76, 100.00]),
 dict(n=6, q="What is the confidence for SEND_US01 and why?", use_case="SEND_US01",
      figures=lambda r: [], words=lambda r: [r["confidence"]["rating"], f"{r['confidence']['planning_assumption_share']:.0%}"], v4_words=["LOW", "55%"]),
 dict(n=7, q="Which SEND_US01 costs are pending?", use_case="SEND_US01",
      figures=lambda r: [], words=["S3", "KMS", "PENDING"], v4_words=["S3-READ-REQUESTS", "KMS-DECRYPT-REQUESTS"]),
 dict(n=8, q="What rate is used for Databricks Jobs Serverless and where does it come from?", rate_card=True,
      figures=lambda r: [r["rate"]], words=lambda r: [r["rate_status"]], v4=[0.3465]),
 dict(n=9, q="Why is the Low backfill cost $0.00 for SEND?", use_case="SEND_US01",
      figures=lambda r: [], words=["not a forecast"], v4_words=["not a forecast"]),
 dict(n=10, q="SEND says each file is read 3 times. What is the new High all-in monthly cost?", use_case="SEND_US01",
      overrides={"s3_read_requests_per_file": 3}, figures=lambda r: [T(r, "HIGH", "recurring_monthly_usd")], v4=[762.83]),
 dict(n=11, q="SQL reporting for 25 concurrent users, 22 working days. What is the High monthly cost?",
      request={**ADHOC, "sql": {"component_id": "ADHOC-SQL", "name": "SQL reporting", "concurrent_users": 25, "working_days_per_month": 22}},
      figures=lambda r: [T(r, "HIGH", "recurring_monthly_usd")], v4=[298.14]),
 dict(n=12, q="A scheduled batch runs 30 times a month, 2 hours per run, and no DBU rate is given. What is the Expected monthly cost?",
      request={**ADHOC, "processing": [P(workload_type="SCHEDULED_BATCH", runtime_hours_low=2, runtime_hours_expected=2, runtime_hours_high=2, runs_per_month=30)]},
      figures=lambda r: [T(r, "EXPECTED", "recurring_monthly_usd")], words=lambda r: [r["confidence"]["rating"]], v4=[20.79]),
 dict(n=13, q="8 GB a day moves through DataSync for 22 days, plus a 5,000 GB one-time migration. What are the costs?",
      request={**ADHOC, "transfer": {"daily_gb_low": 8, "daily_gb_expected": 8, "daily_gb_high": 8, "transfer_days_per_month": 22, "initial_transfer_gb": 5000}},
      figures=lambda r: [T(r, "EXPECTED", "recurring_monthly_usd"), T(r, "EXPECTED", "one_time_usd"), T(r, "EXPECTED", "first_year_usd")],
      words=lambda r: [r["confidence"]["rating"]], v4=[2.20, 62.50, 88.90]),
 dict(n=14, q="A streaming job has no DBU rate and no run details. What is the Expected monthly cost?",
      request={**ADHOC, "processing": [P(workload_type="CONTINUOUS_STREAMING")]},
      figures=lambda r: [T(r, "EXPECTED", "recurring_monthly_usd")], v4=[252.94]),
 dict(n=15, q="2 TB retained, 100 files a day, 10 encrypted a day, 1 KMS key, 3 reads per file. What is the Expected monthly cost?",
      request={**ADHOC, "storage": {"retained_tb": 2, "files_per_day": 100, "encrypted_files_per_day": 10, "kms_keys": 1, "s3_read_requests_per_file": 3}},
      figures=lambda r: [T(r, "EXPECTED", "recurring_monthly_usd")], words=lambda r: [r["confidence"]["rating"]], v4=[47.02]),
 dict(n=16, q="What are the monthly Low / Expected / High and one-time costs for the AI_RAG_MODEL_API preset?", use_case="AI_RAG_MODEL_API",
      figures=lambda r: [T(r, s, "recurring_monthly_usd") for s in ("LOW", "EXPECTED", "HIGH")] + [T(r, "LOW", "one_time_usd"), T(r, "HIGH", "one_time_usd")],
      v4=[104.69, 475.60, 846.51, 509.29, 551.79]),
 dict(n=17, q="100 calls a day on gpt-5-4, 1,000 input and 100 output tokens per call. What is the Expected monthly cost?",
      request={**ADHOC, "model_endpoints": [{"component_id": "ADHOC-MODEL-01", "model": "gpt-5-4", "role": "Model API", "calls_per_day": 100,
               "input_tokens_expected": 1000, "output_tokens_expected": 100}]},
      figures=lambda r: [T(r, "EXPECTED", "recurring_monthly_usd")], v4=[12.00]),
 dict(n=18, q="A scheduled batch has 2 hours per run but no runs per month. What is the cost?",
      request={**ADHOC, "processing": [P(workload_type="SCHEDULED_BATCH", runtime_hours_low=2, runtime_hours_expected=2, runtime_hours_high=2)]},
      figures=lambda r: [], words=["runs per month"], v4_words=["runs per month is required"], followup=None,
      # New contract: no figures; the agent asks how many runs per month (no follow-up is sent).
      no_figures=True),
 dict(n=19, q="A streaming workload needs seconds-level latency. What does it cost?",
      request={**ADHOC, "processing": [P(workload_type="CONTINUOUS_STREAMING", latency_requirement="SECONDS")]},
      figures=lambda r: [], words=["PENDING", "Pipeline"], v4_words=["SERVERLESS_PIPELINE"]),
 dict(n=20, q="SEND daily volume is entered as -1 GB. What happens?", use_case="SEND_US01", overrides={"transfer.daily_gb_high": -1},
      figures=lambda r: [], v4_words=["daily_gb_high must be a non-negative number"],
      # Workbook pass rule: "Error names the field". The question does not say which daily value (low/expected/high),
      # so any transfer.daily_gb_* field rejected as non-negative passes; the workbook's own input used daily_gb_high.
      words_regex=[r"transfer\.daily_gb_(low|expected|high) must be a non-negative number"], no_figures=True),
 dict(n=21, q="Estimate this workload and show line items. Return only LOW, EXPECTED and HIGH; keep component IDs NEW-C01 and NEW-SQL.",
      request=ROUTING_REQ, figures=lambda r: [], followup=FOLLOWUP,
      expected_scenarios=["LOW", "EXPECTED", "HIGH"], expected_component_ids=["NEW-C01", "NEW-SQL"]),
 dict(n=22, q="Estimate this request exactly as supplied:\\n" + json.dumps(JSON_ROUTING_REQ),
      request=JSON_ROUTING_REQ, figures=lambda r: [], followup=None,
      expected_scenarios=["LOW", "EXPECTED", "HIGH"], expected_component_ids=["NEW-C01", "NEW-SQL"]),
 dict(n=23, q="A scheduled batch takes 2 hours per run. What is the monthly cost?",
      request={**ADHOC, "processing": [P(workload_type="SCHEDULED_BATCH", runtime_hours_low=2, runtime_hours_expected=2, runtime_hours_high=2, runs_per_month=30)]},
      figures=lambda r: [T(r, "EXPECTED", "recurring_monthly_usd")], followup="30 runs per month. The other calculator areas are not applicable.",
      required_first_turn_words=["runs per month"], direct_first_turn_question=True),
 dict(n=24, q="I need a DCP estimate for a daily scheduled batch. What information do you need?", route_only=True,
      route_must_contain=["run"], route_must_not_functions=["size_cluster", "estimate_sized_workload", "estimate_jobs_classic_cost"]),
 dict(n=25, q="What cost categories can you estimate? I need SQL and storage; let me choose more than one.", route_only=True,
      route_required_functions=["estimator_catalog"], route_must_contain=["SQL", "storage"]),
 dict(n=26, q="Can you estimate AI Gateway? If it is not covered, tell me rather than guessing a cost.", route_only=True,
      route_required_functions=["estimator_catalog"], route_must_contain=["AI Gateway"], route_must_contain_any=["not ported", "unsupported", "PENDING"]),
 dict(n=27, q="Help me build a DCP estimate covering processing, SQL, DataSync, S3/KMS, recurring model API use, and one-time indexing.",
      request=FULL_GUIDED_REQ, figures=lambda r: [], followup=GUIDED_ANSWER, expect_area_question=False,
      expected_scenarios=["LOW", "EXPECTED", "HIGH"],
      expected_component_ids=["GUIDED-C01", "GUIDED-SQL", "GUIDED-MODEL", "GUIDED-INDEX"]),
]


def resolve(x, r):
    return x(r) if callable(x) else (x or [])


def summarize_calc(case, res):
    if case.get("rate_card"):
        return f"{money(res['rate'])}/DBU, {res['rate_status']}, {res['rate_source']}" if res else "no rate"
    if res is None: return "Use case not loaded - no calculator result"
    if res["status"] in ("REJECTED", "NEEDS_INPUT"): return res["status"] + ": " + "; ".join(res["errors"])
    t = {x["scenario"]: x for x in res["totals"]}
    parts = ["All-in monthly " + " / ".join(f"{s[0]} {money(t[s]['recurring_monthly_usd'])}" for s in SC4 if s in t),
             "core " + " / ".join(f"{s[0]} {money(t[s]['core_monthly_usd'])}" for s in SC4 if s in t)]
    e = t.get("EXPECTED") or t.get("HIGH")
    parts.append(f"one-time {money(e['one_time_usd'])}; first year " + " / ".join(f"{s[0]} {money(t[s]['first_year_usd'])}" for s in SC4 if s in t))
    parts.append(f"{e['status']}; confidence {res['confidence']['rating']}")
    if res["pending"]: parts.append("PENDING: " + "; ".join(p['component_id'] + " - " + p['reason'] for p in res["pending"][:3]))
    return " | ".join(parts)


def main():
    results = []
    for c in CASES:
        if ONLY and c["n"] not in ONLY: continue
        print(f"Q{c['n']} ...", flush=True)
        if c.get("route_only"):
            agent = ask_genie(c["q"])
            blob = (agent["text"] + " " + json.dumps(agent["rows"])).lower()
            sql_blob = "\n".join(agent["sql"]).lower()
            missing = [term for term in c.get("route_must_contain", []) if term.lower() not in blob]
            if c.get("route_must_contain_any") and not any(term.lower() in blob for term in c["route_must_contain_any"]):
                missing.append("one of: " + ", ".join(c["route_must_contain_any"]))
            missing += [f"missing SQL function {name}" for name in c.get("route_required_functions", []) if name.lower() not in sql_blob]
            forbidden = [name for name in c.get("route_must_not_functions", []) if name.lower() in sql_blob]
            leaked = [v for v in dollars_in(agent["text"]) if v != 0]
            verdict = "PASS" if not missing and not forbidden and not leaked else "FAIL"
            why = "; ".join(x for x in [f"missing {missing}" if missing else "", f"used forbidden functions {forbidden}" if forbidden else "",
                                        f"showed figures {leaked}" if leaked else ""] if x) or "route/coverage behavior matched"
            results.append({"n": c["n"], "question": c["q"], "calculator_answer": "Route-only acceptance case", "calculator_figures": [],
                            "agent_sql": agent["sql"], "agent_text": agent["text"], "agent_rows": agent["rows"][:12],
                            "agent_pass": verdict, "agent_notes": why, "agent_first_turn": None,
                            "engine_governed_figures": [], "v4_expected_figures": [], "parity_v4": "NOT_APPLICABLE",
                            "conversation_id": agent["conversation_id"]})
            print(f"   route {verdict} ({why})", flush=True)
            continue
        if c.get("rate_card"):
            rr = sql(f"SELECT rate, status, source FROM {FQ}.dcp_rate_card WHERE provider='DATABRICKS' AND sku='ENTERPRISE_JOBS_SERVERLESS' AND region='US_EAST_1'")
            res = {"rate": float(rr[0][0]), "rate_status": rr[0][1], "rate_source": rr[0][2]} if rr else None
            g = calc.RegistryPricingProvider().get_rate("DATABRICKS", "ENTERPRISE_JOBS_SERVERLESS", "DBU", "US_EAST_1")
            gov = {"rate": g.rate, "rate_status": g.status, "rate_source": g.source}
        else:
            if c.get("request") is not None:
                c = {**c, "request": with_areas(c["request"])}
            res, gov = calculator(c), engine_governed(c)
        figs = [] if res is None else [round(v, 2) for v in resolve(c["figures"], res)] if res.get("status", "OK") not in ("REJECTED", "NEEDS_INPUT") or c.get("rate_card") else []
        words = [] if res is None else resolve(c.get("words"), res)
        agent = ask_genie(c["q"])
        first = None
        if c.get("request") is not None and c.get("followup", FOLLOWUP):
            # Turn one must request unresolved scope and any explicitly tested missing inputs, without figures.
            leaked = [v for v in dollars_in(agent["text"]) if v != 0]
            asked = re.search(r"not applicable|applicable|\barea", agent["text"], re.I) is not None
            expect_areas = c.get("expect_area_question", True)
            missing_first = [term for term in c.get("required_first_turn_words", []) if term.lower() not in agent["text"].lower()]
            direct_question = ("?" in agent["text"] or re.search(r"how many|what is|please provide|which", agent["text"], re.I) is not None)
            if c.get("direct_first_turn_question") and not direct_question:
                missing_first.append("a direct question")
            first_pass = (asked or not expect_areas) and not leaked and not missing_first
            first_notes = ["" if asked or not expect_areas else "did not ask for the area states",
                           f"figures shown before areas were confirmed {leaked}" if leaked else "",
                           f"missing first-turn requirements {missing_first}" if missing_first else ""]
            first = {"text": agent["text"], "sql": agent["sql"], "pass": first_pass,
                     "notes": "; ".join(x for x in first_notes if x) or "asked targeted questions without figures"}
            agent = ask_genie(c.get("followup", FOLLOWUP), agent["conversation_id"])
        nums = numbers_in(agent)
        blob = (agent["text"] + " " + json.dumps(agent["rows"])).lower().replace("_", " ")
        used_calc = any(("dcp_calc_" in s or "dcp_rate_card" in s) for s in agent["sql"])
        own_math = any(re.search(r"\b(?:recurring_monthly_usd|cost_usd|one_time_usd|first_year_usd)\s*[\*\+/-]", s) for s in agent["sql"])
        missing_figs = [f for f in figs if f not in nums]
        missing_words = [w for w in words if w.lower().replace("_", " ") not in blob] + [
            rx for rx in c.get("words_regex", []) if not re.search(rx, agent["text"] + " " + json.dumps(agent["rows"]), re.I)]
        actual_scenarios = {str(row.get("scenario")).upper() for row in agent["rows"] if row.get("scenario") is not None}
        if c.get("expected_scenarios") is not None and actual_scenarios != {x.upper() for x in c["expected_scenarios"]}:
            missing_words.append(f"scenario set {sorted(actual_scenarios)} != expected {sorted(c['expected_scenarios'])}")
        actual_ids = {str(row.get("component_id")) for row in agent["rows"] if row.get("component_id") is not None}
        missing_ids = [x for x in c.get("expected_component_ids", []) if x not in actual_ids]
        if missing_ids:
            missing_words.append(f"missing component ids {missing_ids}")
        if c.get("no_figures") and res is not None:   # NEEDS_INPUT: the agent must ask, not estimate
            cost_nums = [v for v in dollars_in(agent["text"]) if v != 0]
            verdict = "PASS" if used_calc and not own_math and not missing_words and not cost_nums else "FAIL"
            why = "; ".join(x for x in ["" if used_calc else "did not call the calculator",
                                        f"missing words {missing_words}" if missing_words else "",
                                        f"showed figures {sorted(cost_nums)[:6]}" if cost_nums else ""] if x) or "asked for the input, no figures"
        elif res is None:   # nothing to reproduce: the agent must not invent figures
            ok = not any(abs(v) > 0 for v in nums if v not in (2026.0,)) or "not loaded" in blob or "no " in blob
            verdict = "PASS" if (ok and not own_math) else "FAIL"
            why = "Use case not loaded; agent must say so without inventing figures"
        else:
            verdict = "PASS" if used_calc and not own_math and not missing_figs and not missing_words else "FAIL"
            why = "; ".join(x for x in [
                "" if used_calc else "did not call the calculator",
                "agent did its own arithmetic in SQL" if own_math else "",
                f"missing figures {missing_figs}" if missing_figs else "",
                f"missing words {missing_words}" if missing_words else ""] if x) or "matches the calculator"
        if first is not None and not first["pass"]:
            verdict, why = "FAIL", f"turn 1: {first['notes']}; turn 2: {why}"
        # engine parity with the workbook (governed rates)
        if c.get("rate_card"):
            gfigs = [gov["rate"]]
        elif gov is None:
            gfigs = []
        elif gov.get("status") == "REJECTED":
            gfigs = []
        else:
            gfigs = [round(v, 2) for v in resolve(c["figures"], gov)]
        v4 = c.get("v4") or []
        if c.get("rate_card"):
            parity = "MATCH" if abs(gov["rate"] - 0.3465) < 1e-9 and gov["rate_status"] == "GOVERNED" else "DIFFER"
        elif gov is None:
            parity = "NOT LOADED"
        elif gov.get("status") == "REJECTED":
            parity = "MATCH" if any(w.lower() in json.dumps(gov).lower() for w in c.get("v4_words", [])) else "DIFFER"
        elif not v4 and not c.get("v4_words"):
            parity = "NOT_APPLICABLE"
        elif v4 and gfigs and len(gfigs) == len(v4[:len(gfigs)]) and all(abs(a - b) < 0.005 for a, b in zip(gfigs, v4)):
            parity = "MATCH"
        elif not v4 and not gfigs and any(w.lower() in json.dumps(gov).lower() for w in c.get("v4_words", [])):
            parity = "MATCH"
        else:
            parity = "DIFFER"
        results.append({"n": c["n"], "question": c["q"], "calculator_answer": summarize_calc(c, res), "calculator_figures": figs,
                        "agent_sql": agent["sql"], "agent_text": agent["text"], "agent_rows": agent["rows"][:12], "agent_pass": verdict, "agent_notes": why, "agent_first_turn": first,
                        "engine_governed_figures": gfigs, "v4_expected_figures": v4, "parity_v4": parity, "conversation_id": agent["conversation_id"]})
        print(f"   agent {verdict} ({why}) | parity v4.0.2 {parity} gov={gfigs} v4={v4}", flush=True)
    os.makedirs(OUT, exist_ok=True)
    tag = time.strftime("%Y%m%d_%H%M%S")
    json.dump(results, open(os.path.join(OUT, f"benchmark_{tag}.json"), "w"), indent=1)
    print("wrote", os.path.join(OUT, f"benchmark_{tag}.json"))
    print("AGENT PASS", sum(r["agent_pass"] == "PASS" for r in results), "/", len(results))


if __name__ == "__main__":
    main()
