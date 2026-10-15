"""Generate notebooks/09_load_rate_card.py and notebooks/10_deploy_dcp_calculator.py.
Run calculator/build_sql.py first. Re-run both after replacing notebooks/dcp_estimator.py."""
import json, os
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sql = open(os.path.join(ROOT, "sql", "06_dcp_calculator.sql")).read()
skill = json.load(open(os.path.join(ROOT, "genie", "dcp_calculator_skill.json")))
seed = json.load(open(os.path.join(ROOT, "calculator", "rate_registry_seed.json")))
reqs = {f[:-5]: json.load(open(os.path.join(ROOT, "samples", "requests", f)))
        for f in sorted(os.listdir(os.path.join(ROOT, "samples", "requests"))) if f.endswith(".json")}
V = seed["calculator_version"]
gov = "\n".join(l for l in open(os.path.join(ROOT, "sql", "09_governed_prices.sql")).read().split("\n") if not l.startswith("--") or l == "-- @@")
PRICE_CARD_SQL = next(x.strip() for x in gov.split("\n-- @@\n") if x.strip().startswith("CREATE OR REPLACE TABLE __FQ__.ref_dbu_price_card"))

nb09 = f'''# Databricks notebook source
# MAGIC %md
# MAGIC # 09 · Load the rate cards (Lakemeter price card + DCP rate card)
# MAGIC Refreshes **both** rate cards from the same system table, so every estimate uses the same governed price:
# MAGIC `ref_dbu_price_card` (every Lakemeter `estimate_*` and sizing DBU price, for the regions in `price_regions`) and
# MAGIC `dcp_rate_card` (DCP calculator). No single current price = PENDING (NULL), never a fallback.
# MAGIC
# MAGIC ### DCP rate card
# MAGIC The DCP calculator (v{V}) prices **only** with `dcp_rate_card`: one row per calculator rate record
# MAGIC (provider, sku, unit, region). This notebook seeds it with the calculator's reference registry and then
# MAGIC re-prices every Databricks DBU row from the chosen system table:
# MAGIC
# MAGIC | `rate_source` | Databricks DBU rates from | `status` | Use |
# MAGIC |---|---|---|---|
# MAGIC | `ACCOUNT_PRICES` | `system.billing.account_prices` (negotiated) | `GOVERNED` | Production |
# MAGIC | `LIST_PRICES` | `system.billing.list_prices` | `REFERENCE_ONLY` | Test workspaces without account prices |
# MAGIC
# MAGIC A Databricks SKU with no single current price is written as **PENDING** (rate NULL): the calculator then
# MAGIC returns those lines as PENDING. The seed value is never used as a fallback. AWS rows (DataSync, S3, KMS)
# MAGIC are the calculator's registry values. Run as a principal with `SELECT` on the chosen system table; schedule it
# MAGIC to keep rates current. Compare the output with `observed_rate` (the registry value) before go-live.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Target catalog")
dbutils.widgets.text("schema", "lakemeter", "Target schema")
dbutils.widgets.dropdown("rate_source", "ACCOUNT_PRICES", ["ACCOUNT_PRICES", "LIST_PRICES"], "Databricks rate source")
dbutils.widgets.text("sku_region", "US_EAST_N_VIRGINIA", "Billing SKU region suffix for US_EAST_1")
dbutils.widgets.text("price_regions", "us-east-1", "Lakemeter price card regions (comma list, or ALL)")
FQ = f"{{dbutils.widgets.get('catalog').strip()}}.{{dbutils.widgets.get('schema').strip()}}"
SRC = dbutils.widgets.get("rate_source"); SKU_REGION = dbutils.widgets.get("sku_region").strip().upper()

# COMMAND ----------

import json
SEED = json.loads({json.dumps(json.dumps(seed, indent=1))})
table = "system.billing.account_prices" if SRC == "ACCOUNT_PRICES" else "system.billing.list_prices"
price = "pricing.default" if SRC == "ACCOUNT_PRICES" else "coalesce(pricing.effective_list.default, pricing.default)"
status = "GOVERNED" if SRC == "ACCOUNT_PRICES" else "REFERENCE_ONLY"
live = {{}}
for sku, bsku in SEED["billing_sku"].items():
    name = f"{{bsku}}_{{SKU_REGION}}"
    vals = [r[0] for r in spark.sql(f"""
        SELECT DISTINCT CAST({{price}} AS DOUBLE) FROM {{table}}
        WHERE upper(sku_name) = '{{name}}' AND upper(cloud) = 'AWS' AND usage_unit = 'DBU' AND currency_code = 'USD'
          AND price_start_time <= current_timestamp() AND (price_end_time IS NULL OR price_end_time > current_timestamp())
          AND {{price}} IS NOT NULL""").collect()]
    live[sku] = (vals[0] if len(vals) == 1 else None, name, len(vals))
rows = []
for r in SEED["rows"]:
    r = dict(r); bsku = None
    if r["provider"] == "DATABRICKS" and r["unit"] == "DBU" and r["region"] == "US_EAST_1" and r["sku"] in live:
        rate, bsku, n = live[r["sku"]]
        r.update(observed_rate=r["rate"], rate=rate, status=status if rate is not None else "PENDING",
                 source=table if rate is not None else f"{{table}}: {{n}} current prices for {{bsku}} (exactly one required)",
                 effective_start="2026-01-01", effective_end=None,
                 notes="" if SRC == "ACCOUNT_PRICES" else "List price used for testing; production estimates use system.billing.account_prices.")
    rows.append((r["provider"], r["service"], r["sku"], r["unit"], r["region"], r["rate"], r["status"], r["source"],
                 r["effective_start"], r["effective_end"], r["observed_rate"], r["notes"], bsku))
df = spark.createDataFrame(rows, "provider string, service string, sku string, unit string, region string, rate double, status string, "
                                 "source string, effective_start string, effective_end string, observed_rate double, notes string, billing_sku string")
df.selectExpr("*", "current_timestamp() AS refreshed_at").write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{{FQ}}.dcp_rate_card")
display(spark.sql(f"SELECT provider, sku, unit, region, rate, status, observed_rate AS calculator_registry_rate, source FROM {{FQ}}.dcp_rate_card ORDER BY 1, 2"))
# Lakemeter price card (estimate_* and sizing functions)
regions = [r.strip().lower() for r in dbutils.widgets.get("price_regions").split(",") if r.strip()]
region_filter = "true" if [r.upper() for r in regions] == ["ALL"] else "lower(region) IN (" + ", ".join(repr(r) for r in regions) + ")"
PRICE_CARD_SQL = {json.dumps(PRICE_CARD_SQL)}
spark.sql(PRICE_CARD_SQL.replace("__FQ__", FQ).replace("{{{{RATE_TABLE}}}}", table).replace("{{{{PRICE_EXPR}}}}", price)
          .replace("{{{{STATUS}}}}", status).replace("{{{{REGION_FILTER}}}}", region_filter))
display(spark.sql(f"SELECT status, count(*) AS skus FROM {{FQ}}.ref_dbu_price_card GROUP BY 1"))
display(spark.sql(f"SELECT region, tier, product_type, billing_sku, price_per_dbu, status, list_price_reference, source FROM {{FQ}}.ref_dbu_price_card ORDER BY 1, 2, 3"))
pending = [k for k, v in live.items() if v[0] is None]
if pending:
    print(f"WARNING: no single current {{table}} price for {{pending}} - those lines will be PENDING.")
'''
open(os.path.join(ROOT, "notebooks", "09_load_rate_card.py"), "w").write(nb09)

nb10 = f'''# Databricks notebook source
# MAGIC %md
# MAGIC # 10 · Deploy the DCP calculator and register it on the Genie agent
# MAGIC The DCP Smart Estimation Calculator v{V} (`notebooks/dcp_estimator.py`, embedded verbatim) runs as the Unity
# MAGIC Catalog Python function `dcp_calc`. Genie only calls `dcp_calc_totals`, `dcp_calc_line_items` and
# MAGIC `dcp_calc_details` (and builds ad-hoc requests with `dcp_request`). The calculator does all the math, so Genie,
# MAGIC the SQL API and the notebook return the same numbers.
# MAGIC
# MAGIC 1. Creates `dcp_rate_card`, `dcp_requests` and the calculator functions. **Then run 09** to load rates.
# MAGIC 2. Loads named use cases into `dcp_requests` from a folder of request JSON files (`requests_path`) or the bundled
# MAGIC    samples. A file is either `{{"preset": "SEND_US01"}}` or a full EstimateRequest (see `new_workload_template()`).
# MAGIC 3. If `space_id` is set: retires the older SQL-math DCP functions from the agent (they stay in Unity Catalog),
# MAGIC    registers the calculator functions, tables, examples and instructions. The agent's other instructions are kept.
# MAGIC
# MAGIC Re-generate with `calculator/build_sql.py` and `calculator/build_notebook.py` when the calculator changes.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Target catalog")
dbutils.widgets.text("schema", "lakemeter", "Target schema")
dbutils.widgets.text("requests_path", "", "Workspace folder with request JSON files (blank = bundled samples)")
dbutils.widgets.text("space_id", "", "Genie Space id (optional)")
catalog = dbutils.widgets.get("catalog").strip(); schema = dbutils.widgets.get("schema").strip()
requests_path = dbutils.widgets.get("requests_path").strip(); space_id = dbutils.widgets.get("space_id").strip()
FQ = f"{{catalog}}.{{schema}}"
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()

# COMMAND ----------

CALC_SQL = {json.dumps(sql.strip())}
for stmt in [s.strip() for s in CALC_SQL.split("\\n-- @@\\n") if s.strip()]:
    if stmt.startswith("CREATE OR REPLACE TABLE") and spark.catalog.tableExists(f"{{FQ}}.dcp_rate_card"):
        continue   # keep loaded rates on redeploy; notebook 09 owns the rate card
    spark.sql(stmt.replace("__FQ__", FQ))
print("DCP calculator v{V} deployed to", FQ)

# COMMAND ----------

import json, os, glob
SAMPLE_REQUESTS = json.loads({json.dumps(json.dumps(reqs, indent=1))})
if requests_path:
    folder = requests_path if requests_path.startswith("/Workspace") else "/Workspace" + requests_path
    loaded = {{os.path.basename(p)[:-5]: json.load(open(p)) for p in sorted(glob.glob(os.path.join(folder, "*.json")))}}
else:
    loaded = SAMPLE_REQUESTS
rows = []
for name, req in loaded.items():
    res = json.loads(spark.sql(f"SELECT {{FQ}}.dcp_calc(:r, NULL, NULL) AS j", args={{"r": json.dumps(req)}}).first().j)
    if res["status"] == "REJECTED":
        raise ValueError(f"{{name}}: request rejected by the calculator - {{res['errors']}}")
    uc_id = res["header"]["use_case_id"] or name
    rows.append((uc_id, res["header"]["use_case_name"], json.dumps(req), requests_path or "bundled samples"))
if rows:
    ids = ", ".join("'" + r[0].replace("'", "''") + "'" for r in rows)
    spark.sql(f"DELETE FROM {{FQ}}.dcp_requests WHERE use_case_id IN ({{ids}})")
    spark.createDataFrame(rows, "use_case_id string, use_case_name string, request_json string, source string") \\
        .selectExpr("*", "current_timestamp() AS loaded_at").write.mode("append").saveAsTable(f"{{FQ}}.dcp_requests")
display(spark.sql(f"SELECT use_case_id, use_case_name, source, loaded_at FROM {{FQ}}.dcp_requests ORDER BY 1"))

# COMMAND ----------

import uuid, requests
SKILL = json.loads({json.dumps(json.dumps(skill, indent=1))})
def nid(): return uuid.uuid4().hex
if not space_id:
    print("space_id blank -> calculator deployed, Genie agent not changed.")
else:
    host = "https://" + spark.conf.get("spark.databricks.workspaceUrl")
    H = {{"Authorization": f"Bearer {{ctx.apiToken().get()}}"}}
    cur = requests.get(f"{{host}}/api/2.0/genie/spaces/{{space_id}}?include_serialized_space=true", headers=H); cur.raise_for_status(); cur = cur.json()
    space = json.loads(cur["serialized_space"]); instr = space["instructions"]
    sub = lambda x: x.replace("__FQ__", FQ)
    instr["sql_functions"] = [f for f in instr.get("sql_functions", []) if f["identifier"].split(".")[-1] not in SKILL["retire_functions"]]
    have = {{f["identifier"] for f in instr["sql_functions"]}}
    for fn in SKILL["functions"]:
        if f"{{FQ}}.{{fn}}" not in have: instr["sql_functions"].append({{"id": nid(), "identifier": f"{{FQ}}.{{fn}}"}})
    instr["sql_functions"].sort(key=lambda x: x["id"])
    tables = space.setdefault("data_sources", {{}}).setdefault("tables", [])
    tables[:] = [t for t in tables if t["identifier"].split(".")[-1] not in SKILL["retire_tables"] + list(SKILL["tables"])]
    for t, desc in SKILL["tables"].items():
        tables.append({{"identifier": f"{{FQ}}.{{t}}", "description": [desc]}})
    tables.sort(key=lambda x: x["identifier"])
    retired = SKILL["retire_functions"] + SKILL["retire_tables"]
    calc_assets = ("dcp_calc_", "dcp_requests", "dcp_rate_card")
    ex = [e for e in instr.get("example_question_sqls", [])
          if not any(f".{{r}}(" in e["sql"][0] or f".{{r}} " in e["sql"][0] for r in retired) and not any(a in e["sql"][0] for a in calc_assets)]
    for q, s in SKILL["examples"]:
        ex.append({{"id": nid(), "question": [q], "sql": [sub(s)]}})
    instr["example_question_sqls"] = sorted(ex, key=lambda x: x["id"])
    cfg = space.setdefault("config", {{}}); sq = [x for x in cfg.get("sample_questions", []) if "DCP" not in x["question"][0]]
    for q in SKILL["samples"]:
        if q not in {{x["question"][0] for x in sq}}: sq.append({{"id": nid(), "question": [q]}})
    cfg["sample_questions"] = sorted(sq, key=lambda x: x["id"])
    # one text-instruction block: keep the agent's non-DCP text, replace all DCP text with the calculator rules
    text = sub(SKILL["instructions"])
    if instr.get("text_instructions"):
        joined = "".join(instr["text_instructions"][0].get("content", []))
        for marker in (SKILL["instructions_marker"].strip(), "DCP ESTIMATION (", "NAMED USE CASES:", "SIZING GUIDANCE"):
            if marker in joined: joined = joined[:joined.index(marker)].rstrip()
        instr["text_instructions"][0]["content"] = [joined + text]
    else:
        instr["text_instructions"] = [{{"id": nid(), "content": [text.strip()]}}]
    r = requests.patch(f"{{host}}/api/2.0/genie/spaces/{{space_id}}", headers=H,
                       json={{"serialized_space": json.dumps(space), "warehouse_id": cur.get("warehouse_id"), "description": SKILL["description"]}})
    r.raise_for_status(); print("DCP calculator registered on Genie space", space_id)
'''
open(os.path.join(ROOT, "notebooks", "10_deploy_dcp_calculator.py"), "w").write(nb10)
print("wrote notebooks/09_load_rate_card.py and notebooks/10_deploy_dcp_calculator.py (calculator v" + V + ")")
