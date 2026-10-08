# Databricks notebook source
# MAGIC %md
# MAGIC # 06 · DCP named use cases via Excel/CSV intake
# MAGIC Define use cases in an **Excel/CSV** file (see `dcp_intake_template.csv`), load them into
# MAGIC `dcp_intake_components`, create the roll-up functions, and (optionally) register them on the
# MAGIC Genie Space. Then ask Genie: *"Give the DCP estimation for <use_case_id>"*.
# MAGIC
# MAGIC **Intake columns** (one row per workload component):
# MAGIC `use_case_id, use_case_name, component_id, component_name, workload_type,
# MAGIC quantity, runtime_low, runtime_expected, runtime_high, events_per_month,
# MAGIC concurrent_users, planning_days`. `workload_type` ∈ CONTINUOUS_STREAMING / EVENT_DRIVEN_BATCH / SQL_REPORTING.
# MAGIC Leave cells blank where not relevant (e.g. runtimes only for batch; concurrent_users/planning_days for SQL).
# MAGIC Requires 03 (functions) first. Reading .xlsx needs openpyxl (`%pip install openpyxl`); .csv needs nothing.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Target catalog")
dbutils.widgets.text("schema", "lakemeter", "Target schema")
dbutils.widgets.text("intake_path", "", "Workspace path to the intake .csv/.xlsx (blank = ./dcp_intake_template.csv next to this notebook)")
dbutils.widgets.text("space_id", "", "Genie Space id (optional - register the use-case skill)")
catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema").strip()
intake_path = dbutils.widgets.get("intake_path").strip()
space_id = dbutils.widgets.get("space_id").strip()
FQ = f"{catalog}.{schema}"
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
if not intake_path:
    intake_path = "/Workspace" + ctx.notebookPath().get().rsplit("/", 1)[0].rsplit("/", 1)[0] + "/dcp_intake_template.csv"
print("intake_path:", intake_path, "| target:", FQ)

# COMMAND ----------

from pyspark.sql import functions as F

TABLE_COLS = ["use_case_id","use_case_name","component_id","component_name","workload_type",
              "quantity","runtime_low","runtime_expected","runtime_high","events_per_month",
              "concurrent_users","planning_days"]
INT_COLS = ["quantity","concurrent_users","planning_days"]
DBL_COLS = ["runtime_low","runtime_expected","runtime_high","events_per_month"]

if intake_path.lower().endswith((".xlsx", ".xls")):
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        raise RuntimeError("Reading .xlsx requires openpyxl. Add a cell above with `%pip install openpyxl` and dbutils.library.restartPython(), or save the intake as .csv.")
    import pandas as pd
    pdf = pd.read_excel(intake_path, dtype=str)
    raw = spark.createDataFrame(pdf.where(pd.notnull(pdf), None).astype(object))
else:
    raw = spark.read.option("header", "true").csv(f"file:{intake_path}")

sel = []
for c in TABLE_COLS:
    if c in INT_COLS:   sel.append(F.expr(f"CAST(NULLIF(CAST(`{c}` AS STRING),'') AS INT) AS {c}"))
    elif c in DBL_COLS: sel.append(F.expr(f"CAST(NULLIF(CAST(`{c}` AS STRING),'') AS DOUBLE) AS {c}"))
    else:               sel.append(F.expr(f"CAST(`{c}` AS STRING) AS {c}"))
typed = raw.select(*sel)
display(typed)

# COMMAND ----------

# Create the table (if needed), then UPSERT the use cases found in the intake
spark.sql(f"""CREATE TABLE IF NOT EXISTS {FQ}.dcp_intake_components (
  use_case_id STRING, use_case_name STRING, component_id STRING, component_name STRING,
  workload_type STRING, quantity INT, runtime_low DOUBLE, runtime_expected DOUBLE, runtime_high DOUBLE,
  events_per_month DOUBLE, concurrent_users INT, planning_days INT)""")

ucs = [r[0] for r in typed.select("use_case_id").where("use_case_id IS NOT NULL").distinct().collect()]
if ucs:
    in_list = ",".join("'" + u.replace("'", "''") + "'" for u in ucs)
    spark.sql(f"DELETE FROM {FQ}.dcp_intake_components WHERE use_case_id IN ({in_list})")
typed.write.mode("append").saveAsTable(f"{FQ}.dcp_intake_components")
print("loaded use cases:", ucs)

# COMMAND ----------

# Create the roll-up functions (table now exists)
USECASE_SQL = r"""
CREATE TABLE IF NOT EXISTS __FQ__.dcp_intake_components (
  use_case_id STRING, use_case_name STRING, component_id STRING, component_name STRING,
  workload_type STRING, quantity INT, runtime_low DOUBLE, runtime_expected DOUBLE, runtime_high DOUBLE,
  events_per_month DOUBLE, concurrent_users INT, planning_days INT)
COMMENT 'DCP named use cases and their workload components (one row per component), loaded from the intake spreadsheet.'
-- @@
CREATE OR REPLACE FUNCTION __FQ__.dcp_estimate_usecase(p_use_case STRING)
RETURNS TABLE(use_case_id STRING, use_case_name STRING, scenario STRING, total_dbu DOUBLE, total_usd DOUBLE)
COMMENT 'DCP estimation for a NAMED use case in dcp_intake_components. p_use_case accepts EITHER the use_case_id OR the use_case_name (business name), case-insensitive. Rolls up all components per scenario (LOW/EXPECTED/HIGH/STRESS). Pass the user''s wording directly; no lookup needed.'
RETURN
  SELECT t.use_case_id, t.use_case_name, e.scenario, round(sum(e.estimated_dbu),2) AS total_dbu, round(sum(e.estimated_usd),2) AS total_usd
  FROM __FQ__.dcp_intake_components t,
       LATERAL __FQ__.dcp_estimate(t.workload_type,t.quantity,t.runtime_low,t.runtime_expected,t.runtime_high,t.events_per_month,t.concurrent_users,t.planning_days) e
  WHERE (upper(trim(t.use_case_id))=upper(trim(p_use_case)) OR upper(trim(t.use_case_name))=upper(trim(p_use_case)))
    AND e.estimated_dbu IS NOT NULL
  GROUP BY t.use_case_id, t.use_case_name, e.scenario
  ORDER BY t.use_case_id, CASE e.scenario WHEN 'LOW' THEN 1 WHEN 'EXPECTED' THEN 2 WHEN 'HIGH' THEN 3 ELSE 4 END
-- @@
CREATE OR REPLACE FUNCTION __FQ__.dcp_estimate_usecase_components(p_use_case STRING)
RETURNS TABLE(use_case_id STRING, component_id STRING, component_name STRING, workload_type STRING, scenario STRING, estimated_dbu DOUBLE, estimated_usd DOUBLE)
COMMENT 'Per-component DCP breakdown for a NAMED use case. p_use_case accepts EITHER the use_case_id OR the use_case_name (business name), case-insensitive.'
RETURN
  SELECT t.use_case_id, t.component_id, t.component_name, t.workload_type, e.scenario, e.estimated_dbu, e.estimated_usd
  FROM __FQ__.dcp_intake_components t,
       LATERAL __FQ__.dcp_estimate(t.workload_type,t.quantity,t.runtime_low,t.runtime_expected,t.runtime_high,t.events_per_month,t.concurrent_users,t.planning_days) e
  WHERE upper(trim(t.use_case_id))=upper(trim(p_use_case)) OR upper(trim(t.use_case_name))=upper(trim(p_use_case))
  ORDER BY t.component_id, CASE e.scenario WHEN 'LOW' THEN 1 WHEN 'EXPECTED' THEN 2 WHEN 'HIGH' THEN 3 ELSE 4 END
"""
for stmt in [s.strip() for s in USECASE_SQL.split("\n-- @@\n") if s.strip()]:
    spark.sql(stmt.replace("__FQ__", FQ))
print("created dcp_estimate_usecase + dcp_estimate_usecase_components")
for u in ucs:
    print("---", u)
    display(spark.sql(f"SELECT * FROM {FQ}.dcp_estimate_usecase('{u}')"))

# COMMAND ----------

# Optional: register the use-case skill on the Genie Space
import json, uuid, requests
def nid(): return uuid.uuid4().hex
if not space_id:
    print("space_id blank -> table + functions ready; skipping Genie registration.")
else:
    host = "https://" + spark.conf.get("spark.databricks.workspaceUrl")
    H = {"Authorization": f"Bearer {ctx.apiToken().get()}"}
    cur = requests.get(f"{host}/api/2.0/genie/spaces/{space_id}?include_serialized_space=true", headers=H); cur.raise_for_status(); cur = cur.json()
    space = json.loads(cur["serialized_space"]); instr = space["instructions"]
    tid = f"{FQ}.dcp_intake_components"
    if not any(t["identifier"] == tid for t in space["data_sources"]["tables"]):
        space["data_sources"]["tables"].append({"identifier": tid,
            "description": ["DCP named use cases and their workload components (one row per component), loaded from the intake spreadsheet. Find a use case by use_case_id or by its business name in use_case_name."],
            "column_configs": sorted([
                {"column_name": "use_case_id", "description": ["Use case identifier from the intake spreadsheet."], "synonyms": ["use case","usecase","solution id"], "enable_entity_matching": True},
                {"column_name": "use_case_name", "description": ["Business name of the use case."], "synonyms": ["use case name","project","initiative"], "enable_entity_matching": True},
                {"column_name": "workload_type", "description": ["CONTINUOUS_STREAMING / EVENT_DRIVEN_BATCH / SQL_REPORTING."], "enable_entity_matching": True},
                {"column_name": "component_name", "description": ["Human-readable component name."]}], key=lambda c: c["column_name"])})
        space["data_sources"]["tables"] = sorted(space["data_sources"]["tables"], key=lambda t: t["identifier"])
    have = {f["identifier"] for f in instr.get("sql_functions", [])}
    for fn in (f"{FQ}.dcp_estimate_usecase", f"{FQ}.dcp_estimate_usecase_components"):
        if fn not in have: instr.setdefault("sql_functions", []).append({"id": nid(), "identifier": fn})
    instr["sql_functions"] = sorted(instr["sql_functions"], key=lambda x: x["id"])
    note = ("NAMED USE CASES: use cases and their workload components are stored in dcp_intake_components, loaded from the intake spreadsheet. "
            "When the user names a use case (by its id OR its business name) instead of describing one workload, call "
            "dcp_estimate_usecase('<what the user said>') for the per-scenario roll-up, or dcp_estimate_usecase_components('<what the user said>') "
            "for the per-component breakdown. Both functions accept EITHER use_case_id OR use_case_name (case-insensitive), so pass the user's wording "
            "directly - do NOT look up the id first and do NOT add LIMIT. Always return all four scenarios. To list loaded use cases: "
            f"SELECT use_case_id, use_case_name FROM {FQ}.dcp_intake_components GROUP BY 1,2. "
            "If no rows come back, ask the user to load the use case or describe its workloads and use dcp_estimate per workload.")
    if instr.get("text_instructions"):
        content = [c for c in instr["text_instructions"][0].get("content", []) if not c.startswith("NAMED USE CASES")]
        instr["text_instructions"][0]["content"] = content + [note]
    else:
        instr["text_instructions"] = [{"id": nid(), "content": [note]}]
    if not any("dcp_intake_components GROUP BY" in " ".join(e.get("sql", [])) for e in instr.get("example_question_sqls", [])):
        instr.setdefault("example_question_sqls", []).append({"id": nid(), "question": ["Which DCP use cases are loaded?"],
            "sql": [f"SELECT use_case_id, use_case_name, count(*) AS components FROM {FQ}.dcp_intake_components GROUP BY use_case_id, use_case_name ORDER BY use_case_id"]})
        instr["example_question_sqls"].sort(key=lambda x: x["id"])
    r = requests.patch(f"{host}/api/2.0/genie/spaces/{space_id}", headers=H, json={"serialized_space": json.dumps(space), "warehouse_id": cur.get("warehouse_id")}); r.raise_for_status()
    print("registered named-use-case skill on space", space_id)