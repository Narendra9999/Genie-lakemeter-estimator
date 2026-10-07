# Databricks notebook source
# MAGIC %md
# MAGIC # 03 · Create the Genie Space
# MAGIC Builds the "Lakemeter Cost Estimator" Genie Space over the tables, with the estimate_* functions
# MAGIC as trusted assets, column synonyms, instructions, example SQL, and sample questions.
# MAGIC Run after 01 (tables) and 02 (functions).

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Target catalog")
dbutils.widgets.text("schema", "lakemeter", "Target schema")
dbutils.widgets.text("warehouse_id", "", "SQL warehouse id (Pro/Serverless)")
dbutils.widgets.text("space_title", "Lakemeter Cost Estimator", "Genie Space title")
catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema").strip()
warehouse_id = dbutils.widgets.get("warehouse_id").strip()
space_title = dbutils.widgets.get("space_title").strip()
FQ = f"{catalog}.{schema}"
create_genie_space = True
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
if not warehouse_id:
    raise ValueError("warehouse_id widget is required")

# COMMAND ----------

import json, uuid, requests

def nid():
    return uuid.uuid4().hex

def col(name, desc, syn=None, entity=False):
    c = {"column_name": name, "description": [desc]}
    if syn:
        c["synonyms"] = syn
    if entity:
        c["enable_entity_matching"] = True
    return c

# --- tables + descriptions ---
tbl_desc = [
    (f"{FQ}.ref_dbsql_rates", "DBU/hour rates per DBSQL warehouse type (classic/pro/serverless) and size; includes_compute flags whether VM is bundled."),
    (f"{FQ}.ref_dbsql_warehouse_config", "DBSQL warehouse instance configs. IMPORTANT: 'warehouse_size' column holds the TYPE (classic/pro/serverless) and 'warehouse_type' holds the SIZE (e.g. 2X-Large)."),
    (f"{FQ}.ref_dbu_multipliers", "DBU multipliers (e.g. photon) by cloud and sku_type."),
    (f"{FQ}.ref_dbu_rates", "Price per DBU (USD) by cloud, region, tier and SKU (sku_name)."),
    (f"{FQ}.ref_fmapi_databricks_rates", "Foundation Model API (Databricks-hosted) token/hourly DBU rates per model; input_divisor, is_hourly."),
    (f"{FQ}.ref_fmapi_proprietary_rates", "Foundation Model API (proprietary provider models) DBU rates per provider/model/endpoint_type/context_length."),
    (f"{FQ}.ref_instance_dbu_rates", "DBU/hour per VM instance type (classic/serverless compute sizing)."),
    (f"{FQ}.ref_serverless_rates", "Serverless product DBU rates: product in model_serving (sizes cpu/gpu_*), vector_search (standard/storage_optimized)."),
    (f"{FQ}.ref_sku_region_map", "Mapping between SKU region names and cloud region codes (e.g. US_EAST_N_VIRGINIA -> us-east-1)."),
    (f"{FQ}.ref_vm_costs", "Cloud VM $/hour by cloud, region, instance_type, pricing_tier, payment_option."),
]
tbl_desc.sort(key=lambda t: t[0])

COLS = {
    f"{FQ}.ref_dbu_rates": [
        col("cloud", "Cloud provider (AWS/AZURE/GCP).", ["provider", "csp"], True),
        col("price_per_dbu", "Price per DBU in USD.", ["dbu price", "dbu rate", "rate", "price"]),
        col("region", "Cloud region code (e.g. us-east-1).", ["region"], True),
        col("sku_name", "Billing SKU / product type, e.g. JOBS_COMPUTE, ALL_PURPOSE_COMPUTE, SQL_COMPUTE.", ["sku", "product", "product type"], True),
        col("tier", "Pricing tier: STANDARD, PREMIUM, ENTERPRISE.", ["pricing tier", "plan", "edition"], True),
    ],
    f"{FQ}.ref_vm_costs": [
        col("cloud", "Cloud provider.", ["provider", "csp"], True),
        col("cost_per_hour", "Cloud VM cost per hour in USD.", ["vm cost", "vm price", "hourly cost", "compute cost", "instance cost"]),
        col("instance_type", "VM / node instance type (e.g. m5.xlarge).", ["vm", "node", "node type", "machine type", "instance"], True),
        col("payment_option", "Payment option (e.g. NA, 1yr, 3yr).", ["commitment"]),
        col("pricing_tier", "VM pricing tier: on_demand, reserved, spot.", ["on demand", "reserved", "spot", "purchase option"], True),
        col("region", "Cloud region code (e.g. us-east-1).", ["region"], True),
    ],
    f"{FQ}.ref_instance_dbu_rates": [
        col("dbu_rate", "DBU per hour for this instance type.", ["dbu per hour", "dbu rate"]),
        col("instance_type", "VM / node instance type.", ["vm", "node", "node type", "machine type"], True),
        col("memory_gb", "Memory in GB.", ["ram", "memory"]),
        col("vcpus", "Virtual CPUs.", ["cores", "cpus", "vcpu"]),
    ],
    f"{FQ}.ref_dbsql_rates": [
        col("dbu_per_hour", "DBU per hour for the SQL warehouse size.", ["dbu rate"]),
        col("includes_compute", "Whether VM/compute is bundled in the DBU price.", ["compute bundled", "vm included"]),
        col("warehouse_size", "SQL warehouse size (e.g. Small, Medium, Large, 2X-Large).", ["sql warehouse size", "t-shirt size"], True),
        col("warehouse_type", "SQL warehouse type: classic, pro, serverless.", ["sql warehouse type"], True),
    ],
    f"{FQ}.ref_dbsql_warehouse_config": [
        col("driver_instance_type", "Driver VM instance type for the warehouse.", ["driver vm"]),
        col("warehouse_size", "NOTE: holds the warehouse TYPE (classic/pro/serverless) despite the name.", ["warehouse type"], True),
        col("warehouse_type", "NOTE: holds the warehouse SIZE (e.g. 2X-Large) despite the name.", ["warehouse size"], True),
        col("worker_count", "Number of worker nodes.", ["workers", "num workers"]),
        col("worker_instance_type", "Worker VM instance type for the warehouse.", ["worker vm"]),
    ],
    f"{FQ}.ref_serverless_rates": [
        col("dbu_rate", "DBU rate for the serverless product/size.", ["dbu rate"]),
        col("product", "Serverless product: model_serving, vector_search.", ["serverless product"], True),
        col("size_or_model", "GPU/CPU size for model serving, or vector search mode (standard/storage_optimized).", ["gpu type", "mode", "vector search mode"], True),
    ],
    f"{FQ}.ref_sku_region_map": [
        col("region_code", "Cloud region code (e.g. us-east-1).", ["region", "cloud region"], True),
        col("sku_region", "SKU region name (e.g. US_EAST_N_VIRGINIA).", ["sku region"], True),
    ],
    f"{FQ}.ref_fmapi_databricks_rates": [
        col("dbu_rate", "DBU rate per unit (per input_divisor tokens, or per hour).", ["rate", "dbu rate"]),
        col("input_divisor", "Token divisor the rate applies per (usually 1,000,000).", ["token divisor"]),
        col("is_hourly", "Whether the rate is hourly (provisioned) vs token-based.", ["hourly"]),
        col("model", "Databricks-hosted foundation model name (e.g. bge-large).", ["foundation model", "llm", "model name"], True),
        col("rate_type", "Rate type: input_token, output_token, provisioned, etc.", ["token type"], True),
    ],
    f"{FQ}.ref_fmapi_proprietary_rates": [
        col("context_length", "Context length bucket (e.g. all).", ["context window"]),
        col("dbu_rate", "DBU rate per unit (per input_divisor tokens, or per hour).", ["rate", "dbu rate"]),
        col("endpoint_type", "Endpoint type (e.g. global).", ["endpoint"]),
        col("model", "Proprietary model name (e.g. claude-haiku-4-5).", ["foundation model", "llm", "model name"], True),
        col("provider", "Model provider: anthropic, openai, google.", ["model provider", "vendor"], True),
        col("rate_type", "Rate type: input_token, output_token, batch_inference, etc.", ["token type"], True),
    ],
    f"{FQ}.ref_dbu_multipliers": [
        col("feature", "Feature the multiplier applies to (e.g. photon).", ["feature"], True),
        col("multiplier", "DBU multiplier value.", ["dbu multiplier", "factor"]),
        col("sku_type", "SKU type the multiplier applies to.", ["sku", "product type"], True),
    ],
}

data_sources = {"tables": [
    {"identifier": i, "description": [d], "column_configs": sorted(COLS.get(i, []), key=lambda c: c["column_name"])}
    for i, d in tbl_desc
]}

est = ["estimate_jobs_classic_cost", "estimate_all_purpose_classic_cost", "estimate_dlt_classic_cost",
       "estimate_serverless_compute_cost", "estimate_dbsql_cost", "estimate_vector_search_cost",
       "estimate_model_serving_cost", "estimate_lakebase_cost", "estimate_databricks_apps_cost",
       "estimate_fmapi_databricks_cost", "estimate_fmapi_proprietary_cost", "estimate_ai_parse_cost",
       "estimate_ai_classify_cost", "estimate_ai_extract_cost", "get_dbu_price", "get_vm_cost_per_hour", "get_serverless_rate"]
sql_functions = sorted([{"id": nid(), "identifier": f"{FQ}.{n}"} for n in est], key=lambda x: x["id"])

text = [
    f"This space estimates Databricks workload monthly costs, ported from the Lakemeter OSS cost engine. ALWAYS prefer the estimate_* SQL table functions below for cost math instead of deriving formulas from the raw ref_ tables. Call them as table functions, e.g. SELECT * FROM {FQ}.estimate_jobs_classic_cost(...).",
    "Every estimate_* function returns one row: workload, dbu_per_hour, hours_per_month, dbu_per_month, dbu_price, dbu_cost_per_month, vm_cost_per_month, total_cost_per_month. total = dbu_cost + vm_cost. Serverless and token/usage-based workloads have vm_cost_per_month = 0.",
    "COMPUTE: estimate_jobs_classic_cost(cloud,region,tier,driver_node_type,worker_node_type,num_workers,photon_enabled,runs_per_day,avg_runtime_minutes,days_per_month); estimate_all_purpose_classic_cost(same); estimate_dlt_classic_cost(cloud,region,tier,dlt_edition[CORE/PRO/ADVANCED],driver,worker,num_workers,photon_enabled,runs_per_day,avg_runtime_minutes,days_per_month); estimate_serverless_compute_cost(cloud,region,tier,workload_type[JOBS/ALL_PURPOSE],driver,worker,num_workers,serverless_mode[standard/performance],runs_per_day,avg_runtime_minutes,days_per_month).",
    "SQL: estimate_dbsql_cost(cloud,region,tier,warehouse_type[classic/pro/serverless],warehouse_size,num_clusters,runs_per_day,avg_runtime_minutes,days_per_month,hours_per_month). Pass hours_per_month directly if known, else NULL.",
    "AI/SERVING: estimate_model_serving_cost(cloud,region,tier,serverless_size[cpu,gpu_small_t4,...],concurrency,days_per_month); estimate_vector_search_cost(cloud,region,tier,mode[standard/storage_optimized],capacity_millions,days_per_month); estimate_fmapi_databricks_cost(cloud,region,tier,model,rate_type,quantity); estimate_fmapi_proprietary_cost(cloud,region,tier,provider,model,endpoint_type,context_length,rate_type,quantity).",
    "DOCUMENT AI (dbu per 1000 units): estimate_ai_parse_cost(cloud,region,tier,mode[pages/dbu],pages_thousands,complexity[low_text/low_images/medium/high],hours_per_month); estimate_ai_classify_cost(cloud,region,tier,document_type[short_text/rental_contract/custom],num_docs,custom_rate_per_1000); estimate_ai_extract_cost(cloud,region,tier,document_type[short_text/invoice/complex_reasoning/deep_nesting/custom],num_inputs,custom_rate_per_1000).",
    "PLATFORM: estimate_lakebase_cost(cloud,region,tier,capacity_units,ha_nodes,days_per_month) [DBU/hr = CU x HA]; estimate_databricks_apps_cost(cloud,region,tier,app_size[medium/large],num_apps,hours_per_month[default 730]).",
    "Always-on workloads (Model Serving, Vector Search, Lakebase) bill 24 x days_per_month hours. FM API and Document AI are usage-based, so dbu_per_hour/hours are 0 in the output.",
    "Defaults when unspecified: cloud='AWS', region='us-east-1', tier='ENTERPRISE', days_per_month=30, num_clusters=1, concurrency=4, photon_enabled=true for Jobs/DLT. tier in STANDARD/PREMIUM/ENTERPRISE; region is a cloud region code. Round money to 2 decimals.",
]
text_instructions = sorted([{"id": nid(), "content": text}], key=lambda x: x["id"])

examples = [
    (["Estimate the monthly cost of a classic Jobs cluster on AWS us-east-1 with an m5.xlarge driver and 10 m5.xlarge workers, Photon on, running 1 hour per day."],
     [f"SELECT * FROM {FQ}.estimate_jobs_classic_cost('AWS','us-east-1','ENTERPRISE','m5.xlarge','m5.xlarge',10,true,1,60,30)"]),
    (["How much does a serverless SQL Medium warehouse cost if it runs about 160 hours a month?"],
     [f"SELECT * FROM {FQ}.estimate_dbsql_cost('AWS','us-east-1','ENTERPRISE','serverless','Medium',1,NULL,NULL,NULL,160)"]),
    (["What does a Vector Search standard endpoint with 10 million vectors cost per month?"],
     [f"SELECT * FROM {FQ}.estimate_vector_search_cost('AWS','us-east-1','ENTERPRISE','standard',10,30)"]),
    (["Estimate a GPU model serving endpoint (gpu_small_t4) with concurrency 4."],
     [f"SELECT * FROM {FQ}.estimate_model_serving_cost('AWS','us-east-1','ENTERPRISE','gpu_small_t4',4,30)"]),
    (["Monthly cost of a Lakebase instance with 4 capacity units and 2 HA nodes?"],
     [f"SELECT * FROM {FQ}.estimate_lakebase_cost('AWS','us-east-1','ENTERPRISE',4,2,30)"]),
    (["Estimate FM API cost for 1 billion input tokens to the Databricks bge-large model."],
     [f"SELECT * FROM {FQ}.estimate_fmapi_databricks_cost('AWS','us-east-1','ENTERPRISE','bge-large','input_token',1000000000)"]),
]
example_question_sqls = sorted([{"id": nid(), "question": q, "sql": s} for q, s in examples], key=lambda x: x["id"])

sample_q = [
    "Estimate the monthly cost of a classic Jobs cluster: m5.xlarge driver + 10 m5.xlarge workers, Photon, 1 hour/day.",
    "How much does a serverless SQL Medium warehouse cost for 160 hours a month?",
    "What does a Vector Search endpoint with 10 million vectors cost per month?",
    "Monthly cost of a Lakebase instance with 4 capacity units and 2 HA nodes?",
    "Cost to extract fields from 500,000 invoices with ai_extract?",
    "Compare serverless Jobs vs classic Jobs monthly cost for the same cluster.",
]
sample_questions = sorted([{"id": nid(), "question": [q]} for q in sample_q], key=lambda x: x["id"])

space = {"version": 2, "config": {"sample_questions": sample_questions}, "data_sources": data_sources,
         "instructions": {"text_instructions": text_instructions, "example_question_sqls": example_question_sqls, "sql_functions": sql_functions}}

if create_genie_space:
    if not warehouse_id:
        raise ValueError("warehouse_id widget is required to create the Genie Space")
    host = "https://" + spark.conf.get("spark.databricks.workspaceUrl")
    token = ctx.apiToken().get()
    body = {"warehouse_id": warehouse_id, "title": space_title,
            "description": "Estimate Databricks workload costs across compute, SQL, AI/serving, FM API, Document AI, Lakebase and Apps using natural language.",
            "serialized_space": json.dumps(space)}
    r = requests.post(f"{host}/api/2.0/genie/spaces", headers={"Authorization": f"Bearer {token}"}, json=body)
    r.raise_for_status()
    sid = r.json()["space_id"]
    print("Genie Space created:", sid)
    print("Open:", f"{host}/genie/rooms/{sid}")
else:
    print("create_genie_space=false -> tables + functions created; skipping Genie Space.")