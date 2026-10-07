# Databricks notebook source
# MAGIC %md
# MAGIC # 04 · Add / refresh column synonyms on an existing Genie Space
# MAGIC Injects column_configs (descriptions, synonyms, value entity-matching) into the Space's tables and
# MAGIC PATCHes it in place. Use this to enrich a Space created outside of notebook 03.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Target catalog")
dbutils.widgets.text("schema", "lakemeter", "Target schema")
dbutils.widgets.text("space_id", "", "Existing Genie Space id")
catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema").strip()
space_id = dbutils.widgets.get("space_id").strip()
FQ = f"{catalog}.{schema}"
if not space_id:
    raise ValueError("space_id widget is required")
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()

# COMMAND ----------

import json, requests

def col(name, desc, syn=None, entity=False):
    c = {"column_name": name, "description": [desc]}
    if syn: c["synonyms"] = syn
    if entity: c["enable_entity_matching"] = True
    return c

COLS = {
    f"{FQ}.ref_dbu_rates": [
        col("cloud","Cloud provider (AWS/AZURE/GCP).",["provider","csp"],True),
        col("price_per_dbu","Price per DBU in USD.",["dbu price","dbu rate","rate","price"]),
        col("region","Cloud region code (e.g. us-east-1).",["region"],True),
        col("sku_name","Billing SKU / product type (JOBS_COMPUTE, ALL_PURPOSE_COMPUTE, SQL_COMPUTE).",["sku","product","product type"],True),
        col("tier","Pricing tier: STANDARD, PREMIUM, ENTERPRISE.",["pricing tier","plan","edition"],True),
    ],
    f"{FQ}.ref_vm_costs": [
        col("cloud","Cloud provider.",["provider","csp"],True),
        col("cost_per_hour","Cloud VM cost per hour in USD.",["vm cost","vm price","hourly cost","compute cost","instance cost"]),
        col("instance_type","VM / node instance type (e.g. m5.xlarge).",["vm","node","node type","machine type","instance"],True),
        col("payment_option","Payment option (NA, 1yr, 3yr).",["commitment"]),
        col("pricing_tier","VM pricing tier: on_demand, reserved, spot.",["on demand","reserved","spot","purchase option"],True),
        col("region","Cloud region code (e.g. us-east-1).",["region"],True),
    ],
    f"{FQ}.ref_instance_dbu_rates": [
        col("dbu_rate","DBU per hour for this instance type.",["dbu per hour","dbu rate"]),
        col("instance_type","VM / node instance type.",["vm","node","node type","machine type"],True),
        col("memory_gb","Memory in GB.",["ram","memory"]),
        col("vcpus","Virtual CPUs.",["cores","cpus","vcpu"]),
    ],
    f"{FQ}.ref_dbsql_rates": [
        col("dbu_per_hour","DBU per hour for the SQL warehouse size.",["dbu rate"]),
        col("includes_compute","Whether VM/compute is bundled in the DBU price.",["compute bundled","vm included"]),
        col("warehouse_size","SQL warehouse size (Small, Medium, Large, 2X-Large).",["sql warehouse size","t-shirt size"],True),
        col("warehouse_type","SQL warehouse type: classic, pro, serverless.",["sql warehouse type"],True),
    ],
    f"{FQ}.ref_dbsql_warehouse_config": [
        col("driver_instance_type","Driver VM instance type.",["driver vm"]),
        col("warehouse_size","NOTE: holds the warehouse TYPE (classic/pro/serverless) despite the name.",["warehouse type"],True),
        col("warehouse_type","NOTE: holds the warehouse SIZE (e.g. 2X-Large) despite the name.",["warehouse size"],True),
        col("worker_count","Number of worker nodes.",["workers","num workers"]),
        col("worker_instance_type","Worker VM instance type.",["worker vm"]),
    ],
    f"{FQ}.ref_serverless_rates": [
        col("dbu_rate","DBU rate for the serverless product/size.",["dbu rate"]),
        col("product","Serverless product: model_serving, vector_search.",["serverless product"],True),
        col("size_or_model","GPU/CPU size (model serving) or mode (vector search).",["gpu type","mode","vector search mode"],True),
    ],
    f"{FQ}.ref_sku_region_map": [
        col("region_code","Cloud region code (e.g. us-east-1).",["region","cloud region"],True),
        col("sku_region","SKU region name (e.g. US_EAST_N_VIRGINIA).",["sku region"],True),
    ],
    f"{FQ}.ref_fmapi_databricks_rates": [
        col("dbu_rate","DBU rate per unit (per input_divisor tokens, or per hour).",["rate","dbu rate"]),
        col("input_divisor","Token divisor the rate applies per (usually 1,000,000).",["token divisor"]),
        col("is_hourly","Whether the rate is hourly vs token-based.",["hourly"]),
        col("model","Databricks-hosted foundation model name (bge-large, ...).",["foundation model","llm","model name"],True),
        col("rate_type","Rate type: input_token, output_token, provisioned.",["token type"],True),
    ],
    f"{FQ}.ref_fmapi_proprietary_rates": [
        col("context_length","Context length bucket (e.g. all).",["context window"]),
        col("dbu_rate","DBU rate per unit.",["rate","dbu rate"]),
        col("endpoint_type","Endpoint type (e.g. global).",["endpoint"]),
        col("model","Proprietary model name (claude-haiku-4-5, ...).",["foundation model","llm","model name"],True),
        col("provider","Model provider: anthropic, openai, google.",["model provider","vendor"],True),
        col("rate_type","Rate type: input_token, output_token, batch_inference.",["token type"],True),
    ],
    f"{FQ}.ref_dbu_multipliers": [
        col("feature","Feature the multiplier applies to (e.g. photon).",["feature"],True),
        col("multiplier","DBU multiplier value.",["dbu multiplier","factor"]),
        col("sku_type","SKU type the multiplier applies to.",["sku","product type"],True),
    ],
}

host = "https://" + spark.conf.get("spark.databricks.workspaceUrl")
token = ctx.apiToken().get()
H = {"Authorization": f"Bearer {token}"}
cur = requests.get(f"{host}/api/2.0/genie/spaces/{space_id}?include_serialized_space=true", headers=H)
cur.raise_for_status()
cur = cur.json()
etag = cur["etag"]
space = json.loads(cur["serialized_space"])
n = 0
for t in space["data_sources"]["tables"]:
    cfgs = COLS.get(t["identifier"])
    if cfgs:
        t["column_configs"] = sorted(cfgs, key=lambda c: c["column_name"]); n += len(t["column_configs"])
body = {"serialized_space": json.dumps(space), "etag": etag, "warehouse_id": cur.get("warehouse_id")}
r = requests.patch(f"{host}/api/2.0/genie/spaces/{space_id}", headers=H, json=body)
r.raise_for_status()
print(f"updated space {space_id}: {n} column_configs across {sum(1 for t in space['data_sources']['tables'] if t.get('column_configs'))} tables")