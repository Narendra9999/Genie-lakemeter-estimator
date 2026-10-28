"""Generate setup_genie_dcp_agent.py - ONE notebook that builds the whole Genie Cost Estimator Agent in a workspace.

Embeds: the Lakemeter functions (sql/02_functions.sql), sizing (sql/08), governed prices (sql/09), the DCP calculator SQL (sql/06, which embeds
notebooks/dcp_estimator.py verbatim), the calculator rate registry, the named use cases (samples/requests) and the
validated Genie agent definition (genie/space_definition.json). The only external input is the pricing CSV folder.
Run calculator/build_sql.py first. Re-run all builders after any change.
"""
import json, os, re
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
rd = lambda *p: open(os.path.join(ROOT, *p)).read()
FUNCTIONS_SQL = "\n".join(l for l in rd("sql", "02_functions.sql").split("\n") if not l.startswith("--") or l == "-- @@").strip()
CALC_SQL = rd("sql", "06_dcp_calculator.sql").strip()
GOV_SQL = "\n".join(l for l in rd("sql", "09_governed_prices.sql").split("\n") if not l.startswith("--") or l == "-- @@").strip()
SIZING_SQL = "\n".join(l for l in rd("sql", "08_workload_sizing.sql").split("\n") if not l.startswith("--") or l == "-- @@").strip()
SEED = json.loads(rd("calculator", "rate_registry_seed.json"))
SPACE = json.loads(rd("genie", "space_definition.json"))
REQS = {f[:-5]: json.loads(rd("samples", "requests", f)) for f in sorted(os.listdir(os.path.join(ROOT, "samples", "requests"))) if f.endswith(".json")}
V = SEED["calculator_version"]
lit = lambda obj: f"json.loads({json.dumps(json.dumps(obj, indent=1))})"

nb = f'''# Databricks notebook source
# MAGIC %md
# MAGIC # Genie Cost Estimator Agent - one-notebook setup
# MAGIC Builds the complete agent in this workspace in one run:
# MAGIC
# MAGIC | Step | Creates |
# MAGIC |---|---|
# MAGIC | 1 | Schema, and the 10 Lakemeter pricing reference tables from the pricing CSV folder |
# MAGIC | 2 | 29 list-price functions (`estimate_*`, rate lookups), and workload sizing (`size_cluster`, `estimate_sized_workload`, `ref_sizing_*`) |
# MAGIC | 3 | The **DCP Smart Estimation Calculator v{V}** as Unity Catalog functions (`dcp_calc`, `dcp_calc_totals`, `dcp_calc_line_items`, `dcp_calc_details`, `dcp_request`) |
# MAGIC | 4 | Rates: `ref_dbu_price_card` (every Lakemeter and sizing DBU price) and `dcp_rate_card` (DCP calculator), both from `system.billing.account_prices` (governed) or `list_prices`; no price = PENDING |
# MAGIC | 5 | `dcp_requests`: named use cases (bundled presets, plus any request JSON files in `requests_path`) |
# MAGIC | 6 | The Genie agent (updated in place: by `space_id`, else the agent with the same title; created if none) |
# MAGIC | 7 | Optional grants for an agent-user group, and a smoke test through the Genie API |
# MAGIC
# MAGIC **Needs:** a UC-enabled cluster (DBR 13.3 LTS+) or serverless; `CREATE SCHEMA/TABLE/FUNCTION` on the catalog;
# MAGIC `SELECT` on `system.billing.account_prices` (or `list_prices`); a Pro or Serverless SQL warehouse for the agent;
# MAGIC the pricing CSVs in a workspace folder (`pricing_data/` from the repo). Re-running is safe: every step is idempotent.

# COMMAND ----------

dbutils.widgets.text("catalog", "", "1. Catalog")
dbutils.widgets.text("schema", "lakemeter", "2. Schema")
dbutils.widgets.text("warehouse_id", "", "3. SQL warehouse id (agent compute)")
dbutils.widgets.text("pricing_path", "", "4. Pricing CSV folder (blank = ./pricing_data)")
dbutils.widgets.dropdown("rate_source", "ACCOUNT_PRICES", ["ACCOUNT_PRICES", "LIST_PRICES"], "5. Databricks rate source (all estimates)")
dbutils.widgets.text("price_regions", "us-east-1", "5b. Regions to price (comma list, or ALL)")
dbutils.widgets.text("requests_path", "", "6. Extra use-case request JSON folder (optional)")
dbutils.widgets.text("space_id", "", "7. Genie agent id to update (blank = the agent with this title, else create)")
dbutils.widgets.text("space_title", "{SPACE["title"]}", "8. Agent title")
dbutils.widgets.text("user_group", "", "9. Group to grant agent access (optional)")
dbutils.widgets.dropdown("load_pricing_tables", "true", ["true", "false"], "10. Load pricing tables")
dbutils.widgets.dropdown("smoke_test", "true", ["true", "false"], "11. Ask the agent a test question")

# COMMAND ----------

# MAGIC %md
# MAGIC **First run:** the cell above only creates the parameter widgets at the top of the notebook. Fill in at least
# MAGIC `catalog` and `warehouse_id`, then **Run all** again. In a job, pass them as notebook parameters instead.

# COMMAND ----------

W = {{k: dbutils.widgets.get(k).strip() for k in ("catalog", "schema", "warehouse_id", "pricing_path", "rate_source", "price_regions", "requests_path",
                                                 "space_id", "space_title", "user_group", "load_pricing_tables", "smoke_test")}}
missing = [k for k in ("catalog", "warehouse_id") if not W[k]]
if missing:
    msg = f"Fill in the {{' and '.join(missing)}} widget(s) at the top of the notebook, then run all cells again (or pass them as job parameters)."
    print(msg); dbutils.notebook.exit(msg)
FQ = f"{{W['catalog']}}.{{W['schema']}}"
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
HOST = "https://" + spark.conf.get("spark.databricks.workspaceUrl")
HEADERS = {{"Authorization": f"Bearer {{ctx.apiToken().get()}}"}}
pricing_path = W["pricing_path"] or "/Workspace" + ctx.notebookPath().get().rsplit("/", 1)[0] + "/pricing_data"
if not pricing_path.startswith("/Workspace") and not pricing_path.startswith("/Volumes"):
    pricing_path = "/Workspace" + pricing_path
import json, os, glob, uuid, time, requests
for k, v in W.items(): print(f"{{k:20}} {{v}}")
print(f"{{'target':20}} {{FQ}}\\n{{'pricing_path':20}} {{pricing_path}}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Schema and pricing reference tables

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {{FQ}} COMMENT 'Genie Cost Estimator Agent: pricing data, list-price functions and the DCP calculator'")
TABLES = {{"ref_dbu_rates": "dbu-rates.csv", "ref_instance_dbu_rates": "instance-dbu-rates.csv", "ref_dbu_multipliers": "dbu-multipliers.csv",
          "ref_dbsql_rates": "dbsql-rates.csv", "ref_dbsql_warehouse_config": "dbsql-warehouse-config.csv",
          "ref_serverless_rates": "serverless-rates.csv", "ref_sku_region_map": "sku-region-map.csv",
          "ref_fmapi_databricks_rates": "fmapi-databricks-rates.csv", "ref_fmapi_proprietary_rates": "fmapi-proprietary-rates.csv",
          "ref_vm_costs": "vm-costs_part*.csv"}}
if W["load_pricing_tables"] == "true":
    scheme = "" if pricing_path.startswith("/Volumes") else "file:"
    for tbl, pattern in TABLES.items():
        df = spark.read.option("header", "true").option("inferSchema", "true").option("mode", "PERMISSIVE").csv(f"{{scheme}}{{pricing_path}}/{{pattern}}")
        df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{{FQ}}.{{tbl}}")
        print(f"  {{tbl:30}} {{df.count():>8}} rows")
else:
    missing = [t for t in TABLES if not spark.catalog.tableExists(f"{{FQ}}.{{t}}")]
    if missing: raise ValueError(f"load_pricing_tables=false but these tables are missing: {{missing}}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. List-price functions and workload sizing
# MAGIC Workload sizing turns a described workload (type, GB per day, runs per day) into a recommended cluster, then prices it
# MAGIC with the list-price functions. Throughput and start-time values in `ref_sizing_profiles` start as planning
# MAGIC assumptions: calibrate them from your job history with `notebooks/11_calibrate_sizing.py`, which can also load the
# MAGIC worker limit and allowed node types from a job cluster policy into `ref_sizing_policy`. Calibrated profiles are kept on re-run.

# COMMAND ----------

FUNCTIONS_SQL = {json.dumps(FUNCTIONS_SQL)}
stmts = [s.strip() for s in FUNCTIONS_SQL.split("\\n-- @@\\n") if s.strip()]
for s in stmts: spark.sql(s.replace("__FQ__", FQ))
print(f"{{len(stmts)}} list-price functions created")
GOV_SQL = {json.dumps(GOV_SQL)}
GOV_STMTS = [s.strip() for s in GOV_SQL.split("\\n-- @@\\n") if s.strip()]
PRICE_CARD_SQL = next(s for s in GOV_STMTS if s.startswith("CREATE OR REPLACE TABLE __FQ__.ref_dbu_price_card"))
for s in GOV_STMTS:
    if s is not PRICE_CARD_SQL: spark.sql(s.replace("__FQ__", FQ))   # price card table + get_dbu_price / get_dbu_price_status (governed)
print("get_dbu_price now reads ref_dbu_price_card (loaded in step 4)")
SIZING_SQL = {json.dumps(SIZING_SQL)}
if spark.catalog.tableExists(f"{{FQ}}.ref_sizing_profiles") and "gb_per_core_hour_expected" not in spark.table(f"{{FQ}}.ref_sizing_profiles").columns:
    spark.sql(f"DROP TABLE {{FQ}}.ref_sizing_profiles")   # pre-v2 sizing table (no throughput range): rebuild
for s in [s.strip() for s in SIZING_SQL.split("\\n-- @@\\n") if s.strip()]:
    spark.sql(s.replace("__FQ__", FQ))
print("Workload sizing created: ref_sizing_profiles, ref_sizing_node_families, size_cluster, estimate_sized_workload")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. DCP calculator v{V} (embedded verbatim in `dcp_calc`)

# COMMAND ----------

CALC_SQL = {json.dumps(CALC_SQL)}
for s in [s.strip() for s in CALC_SQL.split("\\n-- @@\\n") if s.strip()]:
    if s.startswith("CREATE OR REPLACE TABLE") and spark.catalog.tableExists(f"{{FQ}}.dcp_rate_card"):
        continue   # step 4 rewrites the rate card
    spark.sql(s.replace("__FQ__", FQ))
print("DCP calculator functions created")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. DCP rate card
# MAGIC Databricks DBU rows: exactly one current price per SKU from the chosen system table, else **PENDING** (no fallback).

# COMMAND ----------

# Lakemeter price card: every estimate_* and sizing DBU price
src_table = "system.billing.account_prices" if W["rate_source"] == "ACCOUNT_PRICES" else "system.billing.list_prices"
price = "pricing.default" if W["rate_source"] == "ACCOUNT_PRICES" else "coalesce(pricing.effective_list.default, pricing.default)"
status = "GOVERNED" if W["rate_source"] == "ACCOUNT_PRICES" else "REFERENCE_ONLY"
regions = [r.strip().lower() for r in W["price_regions"].split(",") if r.strip()]
region_filter = "true" if [r.upper() for r in regions] == ["ALL"] else "lower(region) IN (" + ", ".join(repr(r) for r in regions) + ")"
spark.sql(PRICE_CARD_SQL.replace("__FQ__", FQ).replace("{{{{RATE_TABLE}}}}", src_table).replace("{{{{PRICE_EXPR}}}}", price)
          .replace("{{{{STATUS}}}}", status).replace("{{{{REGION_FILTER}}}}", region_filter))
display(spark.sql(f"SELECT status, count(*) AS skus FROM {{FQ}}.ref_dbu_price_card GROUP BY 1"))
display(spark.sql(f"SELECT region, tier, product_type, billing_sku, price_per_dbu, status, list_price_reference, source FROM {{FQ}}.ref_dbu_price_card "
                  f"WHERE tier = 'ENTERPRISE' ORDER BY region, product_type"))

SEED = {lit(SEED)}
SKU_REGION = "US_EAST_N_VIRGINIA"
src_table = "system.billing.account_prices" if W["rate_source"] == "ACCOUNT_PRICES" else "system.billing.list_prices"
price = "pricing.default" if W["rate_source"] == "ACCOUNT_PRICES" else "coalesce(pricing.effective_list.default, pricing.default)"
status = "GOVERNED" if W["rate_source"] == "ACCOUNT_PRICES" else "REFERENCE_ONLY"
live = {{}}
for sku, bsku in SEED["billing_sku"].items():
    name = f"{{bsku}}_{{SKU_REGION}}"
    vals = [r[0] for r in spark.sql(f"""SELECT DISTINCT CAST({{price}} AS DOUBLE) FROM {{src_table}}
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
                 source=src_table if rate is not None else f"{{src_table}}: {{n}} current prices for {{bsku}} (exactly one required)",
                 effective_start="2026-01-01", effective_end=None,
                 notes="" if W["rate_source"] == "ACCOUNT_PRICES" else "List price used for testing; production estimates use system.billing.account_prices.")
    rows.append((r["provider"], r["service"], r["sku"], r["unit"], r["region"], r["rate"], r["status"], r["source"],
                 r["effective_start"], r["effective_end"], r["observed_rate"], r["notes"], bsku))
(spark.createDataFrame(rows, "provider string, service string, sku string, unit string, region string, rate double, status string, source string, "
                             "effective_start string, effective_end string, observed_rate double, notes string, billing_sku string")
      .selectExpr("*", "current_timestamp() AS refreshed_at").write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{{FQ}}.dcp_rate_card"))
display(spark.sql(f"SELECT provider, sku, unit, rate, status, observed_rate AS calculator_registry_rate, source FROM {{FQ}}.dcp_rate_card ORDER BY 1, 2"))
pend = [k for k, v in live.items() if v[0] is None]
if pend: print(f"NOTE: no single current {{src_table}} price for {{pend}} - estimates using them show PENDING.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Named use cases
# MAGIC Bundled: calculator presets and samples. Add your own: one request JSON per use case in `requests_path`
# MAGIC (format: `new_workload_template.json`, or `{{"preset": "<name>"}}`). Each is validated by the calculator first.

# COMMAND ----------

loaded = {lit(REQS)}
if W["requests_path"]:
    folder = W["requests_path"] if W["requests_path"].startswith("/Workspace") else "/Workspace" + W["requests_path"]
    loaded.update({{os.path.basename(p)[:-5]: json.load(open(p)) for p in sorted(glob.glob(os.path.join(folder, "*.json")))}})
rows = []
for name, req in loaded.items():
    res = json.loads(spark.sql(f"SELECT {{FQ}}.dcp_calc(:r, NULL, NULL, true) AS j", args={{"r": json.dumps(req)}}).first().j)
    if res["status"] in ("REJECTED", "NEEDS_INPUT"):
        raise ValueError(f"{{name}}: request rejected by the calculator - {{res['errors']}}")
    rows.append((res["header"]["use_case_id"] or name, res["header"]["use_case_name"], json.dumps(req), W["requests_path"] or "bundled"))
ids = ", ".join("'" + r[0].replace("'", "''") + "'" for r in rows)
spark.sql(f"DELETE FROM {{FQ}}.dcp_requests WHERE use_case_id IN ({{ids}})")
(spark.createDataFrame(rows, "use_case_id string, use_case_name string, request_json string, source string")
      .selectExpr("*", "current_timestamp() AS loaded_at").write.mode("append").saveAsTable(f"{{FQ}}.dcp_requests"))
display(spark.sql(f"SELECT use_case_id, use_case_name, source FROM {{FQ}}.dcp_requests ORDER BY 1"))
display(spark.sql(f"SELECT scenario, core_monthly_usd, recurring_monthly_usd, one_time_usd, first_year_usd, status, confidence, rate_card_source "
                  f"FROM {{FQ}}.dcp_calc_totals(p_use_case => '{{rows[0][0]}}')"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Genie agent
# MAGIC Built from the validated agent definition (tables with column synonyms, trusted functions, instructions,
# MAGIC example questions, sample questions). With `space_id` the existing agent is replaced in place (same id, same URL).

# COMMAND ----------

SPACE = {lit(SPACE)}
nid = lambda: uuid.uuid4().hex
sub = lambda o: json.loads(json.dumps(o).replace("__FQ__", FQ))
D = sub(SPACE)
serialized = {{
  "version": 2,
  "config": {{"sample_questions": sorted([{{"id": nid(), "question": q}} for q in D["sample_questions"]], key=lambda x: x["id"])}},
  "data_sources": {{"tables": sorted(D["tables"], key=lambda t: t["identifier"])}},
  "instructions": {{
    "text_instructions": [{{"id": nid(), "content": D["text_instructions"]}}],
    "example_question_sqls": sorted([{{"id": nid(), **e}} for e in D["example_question_sqls"]], key=lambda x: x["id"]),
    "sql_functions": sorted([{{"id": nid(), "identifier": f}} for f in D["sql_functions"]], key=lambda x: x["id"])}},
  # Benchmarks tab: question + answer SQL + grading note (question ids must not clash with sample question ids)
  "benchmarks": {{"questions": sorted([{{"id": nid(), "question": b["question"], "answer": [{{"format": "SQL", "content": b["sql"]}}],
                                      **({{"evaluation_note": b["evaluation_note"]}} if b.get("evaluation_note") else {{}})}}
                                     for b in D.get("benchmarks", [])], key=lambda x: x["id"])}}}}
body = {{"warehouse_id": W["warehouse_id"], "title": W["space_title"], "description": D["description"], "serialized_space": json.dumps(serialized)}}
if not W["space_id"]:
    # No id given: reuse the agent with the same title, so re-running this notebook (e.g. as a bundle job) never creates duplicates
    found, token = [], None
    while True:
        r = requests.get(f"{{HOST}}/api/2.0/genie/spaces", headers=HEADERS, params={{"page_size": 100, **({{"page_token": token}} if token else {{}})}})
        r.raise_for_status(); page = r.json()
        found += [x["space_id"] for x in page.get("spaces", []) if x.get("title") == W["space_title"]]
        token = page.get("next_page_token")
        if not token: break
    if len(found) > 1:
        raise ValueError(f"{{len(found)}} Genie agents are titled {{W['space_title']!r}}: {{found}}. Set space_id to the one to update.")
    if found:
        W["space_id"] = found[0]; print("Found existing agent with this title:", found[0])
if W["space_id"]:
    r = requests.patch(f"{{HOST}}/api/2.0/genie/spaces/{{W['space_id']}}", headers=HEADERS, json=body); r.raise_for_status(); space_id = W["space_id"]
    print("Genie agent updated:", space_id)
else:
    r = requests.post(f"{{HOST}}/api/2.0/genie/spaces", headers=HEADERS, json=body); r.raise_for_status(); space_id = r.json()["space_id"]
    print("Genie agent created:", space_id)
print("Open:", f"{{HOST}}/genie/rooms/{{space_id}}")
print("MCP endpoint for Copilot:", f"{{HOST}}/api/2.0/mcp/genie/{{space_id}}")
try:
    dbutils.jobs.taskValues.set(key="space_id", value=space_id)   # readable by later job tasks
except Exception:
    pass

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Access and smoke test

# COMMAND ----------

if W["user_group"]:
    g = W["user_group"]
    for s in (f"GRANT USE CATALOG ON CATALOG {{W['catalog']}} TO `{{g}}`", f"GRANT USE SCHEMA, SELECT, EXECUTE ON SCHEMA {{FQ}} TO `{{g}}`"):
        spark.sql(s); print(s)
    for path, level in ((f"genie/{{space_id}}", "CAN_RUN"), (f"sql/warehouses/{{W['warehouse_id']}}", "CAN_USE")):
        r = requests.patch(f"{{HOST}}/api/2.0/permissions/{{path}}", headers=HEADERS,
                           json={{"access_control_list": [{{"group_name": g, "permission_level": level}}]}})
        print(f"{{level}} on {{path}}:", "OK" if r.ok else f"{{r.status_code}} {{r.text[:200]}} (grant it in the UI)")
else:
    print("user_group blank -> no grants. Agent users need CAN RUN on the agent, CAN USE on the warehouse, and "
          f"USE CATALOG + USE SCHEMA, SELECT, EXECUTE on {{FQ}}.")
if W["smoke_test"] == "true":
    q = "Give me the DCP estimate for SEND_US01."
    r = requests.post(f"{{HOST}}/api/2.0/genie/spaces/{{space_id}}/start-conversation", headers=HEADERS, json={{"content": q}}); r.raise_for_status()
    cid, mid = r.json()["conversation_id"], r.json()["message_id"]
    for _ in range(60):
        m = requests.get(f"{{HOST}}/api/2.0/genie/spaces/{{space_id}}/conversations/{{cid}}/messages/{{mid}}", headers=HEADERS).json()
        if m["status"] in ("COMPLETED", "FAILED", "CANCELLED"): break
        time.sleep(3)
    print("Q:", q, "->", m["status"])
    for a in m.get("attachments", []):
        if "query" in a: print("SQL:", a["query"]["query"])
        if "text" in a: print(a["text"]["content"])
    if m["status"] != "COMPLETED": print("Error:", m.get("error"))
'''
open(os.path.join(ROOT, "setup_genie_dcp_agent.py"), "w").write(nb)
print(f"wrote setup_genie_dcp_agent.py (calculator v{V}, {len(SPACE['sql_functions'])} agent functions, {len(REQS)} bundled use cases, {len(nb)//1024} KB)")
