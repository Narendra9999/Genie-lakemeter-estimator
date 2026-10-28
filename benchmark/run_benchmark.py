"""DCP agent benchmark (workbook tab C, 20 questions).

For every question:
  1. CALCULATOR  - run the deployed Unity Catalog calculator (dcp_calc_run) on the benchmark inputs.
                   This is the reference answer for the agent.
  2. AGENT       - ask the Genie agent the question exactly as written (new conversation each time).
                   Ad-hoc questions do not say whether the other calculator areas apply, so the agent's FIRST reply must
                   give no figures and ask for the area states; the follow-up "the other calculator areas are not
                   applicable" is then sent in the same conversation and that reply is graded against the calculator.
  3. AGENT PASS  - the agent called the calculator and reproduced its figures exactly (to the cent),
                   showed PENDING items as PENDING, and showed validation errors verbatim.
  4. ENGINE vs v4.0.2 - the same request through calculator/dcp_estimator.py at the workbook's governed
                   rates, compared with the workbook's expected answer. This separates rate-card
                   differences (list vs governed) from engine-version differences (v3.8 vs v4.0.2).

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
ADHOC = dict(use_case_id="ADHOC", use_case_name="Ad-hoc", scenarios=SC4)
AREAS = ("processing", "sql", "transfer", "storage", "model_endpoints", "one_time_model_jobs")
FOLLOWUP = "The other calculator areas are not applicable."
def with_areas(req):
    """The calculator reference for an ad-hoc question: every area without inputs is NOT_APPLICABLE (the follow-up answer)."""
    return {**req, "_areas": {a: "NOT_APPLICABLE" for a in AREAS if not req.get(a)}}
def P(**kw): return {"component_id": "C01", "name": "Workload", **kw}
CASES = [
 dict(n=1, q="What is the High core monthly estimate for SEND_US01?", use_case="SEND_US01",
      figures=lambda r: [T(r, "HIGH", "core_monthly_usd")], v4=[531.78]),
 dict(n=2, q="Give the core monthly range Low / Expected / High / Stress for SEND_US01.", use_case="SEND_US01",
      figures=lambda r: [T(r, s, "core_monthly_usd") for s in SC4], v4=[146.76, 292.60, 531.78, 808.95], words=["sensitivity"]),
 dict(n=3, q="What is the High all-in monthly cost for SEND_US01 at 10 TB?", use_case="SEND_US01",
      figures=lambda r: [T(r, "HIGH", "recurring_monthly_usd")], v4=[762.81]),
 dict(n=4, q="What is the High first-year total for SEND_US01 at 10 TB, and what is the one-time part?", use_case="SEND_US01",
      figures=lambda r: [T(r, "HIGH", "first_year_usd"), T(r, "HIGH", "one_time_usd")], v4=[9278.76, 125.00]),
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
            # turn 1 must ask for the area states and show none of the figures; turn 2 is graded below
            # graded on what the agent WROTE: any dollar amount means it estimated before the areas were confirmed
            leaked = [v for v in dollars_in(agent["text"]) if v != 0]
            asked = re.search(r"not applicable|applicable|\barea", agent["text"], re.I) is not None
            first = {"text": agent["text"], "sql": agent["sql"], "pass": asked and not leaked,
                     "notes": "; ".join(x for x in ["" if asked else "did not ask for the area states",
                                                    f"figures shown before areas were confirmed {leaked}" if leaked else ""] if x) or "asked for the area states"}
            agent = ask_genie(c.get("followup", FOLLOWUP), agent["conversation_id"])
        nums = numbers_in(agent)
        blob = (agent["text"] + " " + json.dumps(agent["rows"])).lower().replace("_", " ")
        used_calc = any(("dcp_calc_" in s or "dcp_rate_card" in s) for s in agent["sql"])
        own_math = any(re.search(r"\b(?:recurring_monthly_usd|cost_usd|one_time_usd|first_year_usd)\s*[\*\+/-]", s) for s in agent["sql"])
        missing_figs = [f for f in figs if f not in nums]
        missing_words = [w for w in words if w.lower().replace("_", " ") not in blob] + [
            rx for rx in c.get("words_regex", []) if not re.search(rx, agent["text"] + " " + json.dumps(agent["rows"]), re.I)]
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
