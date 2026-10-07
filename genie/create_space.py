#!/usr/bin/env python3
import json, uuid, subprocess, sys
CAT="fevm_catalog_naren"; SCH="lakemeter"; FQ=f"{CAT}.{SCH}"
WID="2b169603bdabc084"; SPACE="01f1c274b84319229082a9b46e9f12e6"; PROFILE="fevm-naren"
def nid(): return uuid.uuid4().hex
def api(method, path):
    r=subprocess.run(["databricks","api",method,path,"--profile",PROFILE],capture_output=True,text=True)
    return json.loads(r.stdout)

tables=[
 (f"{FQ}.ref_dbsql_rates","DBU/hour rates per DBSQL warehouse type (classic/pro/serverless) and size; includes_compute flags whether VM is bundled."),
 (f"{FQ}.ref_dbsql_warehouse_config","DBSQL warehouse instance configs. IMPORTANT: 'warehouse_size' column holds the TYPE (classic/pro/serverless) and 'warehouse_type' holds the SIZE (e.g. 2X-Large)."),
 (f"{FQ}.ref_dbu_multipliers","DBU multipliers (e.g. photon) by cloud and sku_type."),
 (f"{FQ}.ref_dbu_rates","Price per DBU (USD) by cloud, region, tier and SKU (sku_name)."),
 (f"{FQ}.ref_fmapi_databricks_rates","Foundation Model API (Databricks-hosted) token/hourly DBU rates per model; input_divisor, is_hourly."),
 (f"{FQ}.ref_fmapi_proprietary_rates","Foundation Model API (proprietary provider models) DBU rates per provider/model/endpoint_type/context_length."),
 (f"{FQ}.ref_instance_dbu_rates","DBU/hour per VM instance type (classic/serverless compute sizing)."),
 (f"{FQ}.ref_serverless_rates","Serverless product DBU rates: product in model_serving (sizes cpu/gpu_*), vector_search (standard/storage_optimized)."),
 (f"{FQ}.ref_sku_region_map","Mapping between SKU region names and cloud region codes (e.g. US_EAST_N_VIRGINIA -> us-east-1)."),
 (f"{FQ}.ref_vm_costs","Cloud VM $/hour by cloud, region, instance_type, pricing_tier, payment_option."),
]
tables.sort(key=lambda t:t[0])
data_sources={"tables":[{"identifier":i,"description":[d]} for i,d in tables]}

funcs=[f"{FQ}."+n for n in [
 "estimate_jobs_classic_cost","estimate_all_purpose_classic_cost","estimate_dlt_classic_cost",
 "estimate_serverless_compute_cost","estimate_dbsql_cost","estimate_vector_search_cost",
 "estimate_model_serving_cost","estimate_lakebase_cost","estimate_databricks_apps_cost",
 "estimate_fmapi_databricks_cost","estimate_fmapi_proprietary_cost","estimate_ai_parse_cost",
 "estimate_ai_classify_cost","estimate_ai_extract_cost","get_dbu_price","get_vm_cost_per_hour","get_serverless_rate"]]
sql_functions=sorted([{"id":nid(),"identifier":f} for f in funcs], key=lambda x:x["id"])

text=[
 "This space estimates Databricks workload monthly costs, ported from the Lakemeter OSS cost engine. ALWAYS prefer the estimate_* SQL table functions below for cost math instead of deriving formulas from the raw ref_ tables. Call them as table functions, e.g. SELECT * FROM fevm_catalog_naren.lakemeter.estimate_jobs_classic_cost(...).",
 "Every estimate_* function returns one row: workload, dbu_per_hour, hours_per_month, dbu_per_month, dbu_price, dbu_cost_per_month, vm_cost_per_month, total_cost_per_month. total = dbu_cost + vm_cost. Serverless and token/usage-based workloads have vm_cost_per_month = 0.",
 "COMPUTE: estimate_jobs_classic_cost(cloud,region,tier,driver_node_type,worker_node_type,num_workers,photon_enabled,runs_per_day,avg_runtime_minutes,days_per_month); estimate_all_purpose_classic_cost(same signature); estimate_dlt_classic_cost(cloud,region,tier,dlt_edition[CORE/PRO/ADVANCED],driver,worker,num_workers,photon_enabled,runs_per_day,avg_runtime_minutes,days_per_month); estimate_serverless_compute_cost(cloud,region,tier,workload_type[JOBS/ALL_PURPOSE],driver,worker,num_workers,serverless_mode[standard/performance],runs_per_day,avg_runtime_minutes,days_per_month).",
 "SQL: estimate_dbsql_cost(cloud,region,tier,warehouse_type[classic/pro/serverless],warehouse_size[e.g. Medium,Large,2X-Large],num_clusters,runs_per_day,avg_runtime_minutes,days_per_month,hours_per_month). Pass hours_per_month directly if known, else NULL and use runs/runtime/days.",
 "AI / SERVING: estimate_model_serving_cost(cloud,region,tier,serverless_size[cpu,gpu_small_t4,gpu_medium_a10g_1x,...],concurrency,days_per_month); estimate_vector_search_cost(cloud,region,tier,mode[standard/storage_optimized],capacity_millions,days_per_month); estimate_fmapi_databricks_cost(cloud,region,tier,model,rate_type[input_token/output_token/...],quantity[tokens or hours]); estimate_fmapi_proprietary_cost(cloud,region,tier,provider[anthropic/openai/google],model,endpoint_type[global],context_length[all],rate_type,quantity).",
 "DOCUMENT AI (usage-based, dbu per 1000 units): estimate_ai_parse_cost(cloud,region,tier,mode[pages/dbu],pages_thousands,complexity[low_text/low_images/medium/high],hours_per_month); estimate_ai_classify_cost(cloud,region,tier,document_type[short_text/rental_contract/custom],num_docs,custom_rate_per_1000); estimate_ai_extract_cost(cloud,region,tier,document_type[short_text/invoice/complex_reasoning/deep_nesting/custom],num_inputs,custom_rate_per_1000).",
 "PLATFORM: estimate_lakebase_cost(cloud,region,tier,capacity_units,ha_nodes,days_per_month) [DBU/hr = CU x HA, always-on]; estimate_databricks_apps_cost(cloud,region,tier,app_size[medium/large],num_apps,hours_per_month[default 730]).",
 "Always-on workloads (Model Serving, Vector Search, Lakebase) bill 24 x days_per_month hours. FM API and Document AI are usage-based (quantity x rate), so dbu_per_hour/hours are 0 in the output.",
 "Defaults when unspecified: cloud='AWS', region='us-east-1', tier='ENTERPRISE', days_per_month=30, num_clusters=1, concurrency=4, photon_enabled=true for Jobs/DLT. tier in STANDARD/PREMIUM/ENTERPRISE; region is a cloud region code (us-east-1). For raw rate lookups use get_dbu_price / get_vm_cost_per_hour / get_serverless_rate or the ref_ tables. Round money to 2 decimals.",
]
text_instructions=sorted([{"id":nid(),"content":text}], key=lambda x:x["id"])

examples=[
 (["Estimate the monthly cost of a classic Jobs cluster on AWS us-east-1 with an m5.xlarge driver and 10 m5.xlarge workers, Photon on, running 1 hour per day."],
  ["SELECT * FROM fevm_catalog_naren.lakemeter.estimate_jobs_classic_cost('AWS','us-east-1','ENTERPRISE','m5.xlarge','m5.xlarge',10,true,1,60,30)"]),
 (["How much does a serverless SQL Medium warehouse cost if it runs about 160 hours a month?"],
  ["SELECT * FROM fevm_catalog_naren.lakemeter.estimate_dbsql_cost('AWS','us-east-1','ENTERPRISE','serverless','Medium',1,NULL,NULL,NULL,160)"]),
 (["Estimate an ADVANCED DLT pipeline on AWS us-east-1 with 5 m5.xlarge workers and an m5.xlarge driver, Photon, running 2 hours daily."],
  ["SELECT * FROM fevm_catalog_naren.lakemeter.estimate_dlt_classic_cost('AWS','us-east-1','ENTERPRISE','ADVANCED','m5.xlarge','m5.xlarge',5,true,1,120,30)"]),
 (["What does a Vector Search standard endpoint with 10 million vectors cost per month on AWS us-east-1?"],
  ["SELECT * FROM fevm_catalog_naren.lakemeter.estimate_vector_search_cost('AWS','us-east-1','ENTERPRISE','standard',10,30)"]),
 (["Estimate a GPU model serving endpoint (gpu_small_t4) with concurrency 4 on AWS us-east-1."],
  ["SELECT * FROM fevm_catalog_naren.lakemeter.estimate_model_serving_cost('AWS','us-east-1','ENTERPRISE','gpu_small_t4',4,30)"]),
 (["What is the monthly cost of a Lakebase instance with 4 capacity units and 2 HA nodes?"],
  ["SELECT * FROM fevm_catalog_naren.lakemeter.estimate_lakebase_cost('AWS','us-east-1','ENTERPRISE',4,2,30)"]),
 (["Cost of 2 medium Databricks Apps running continuously (730 hours) on AWS us-east-1."],
  ["SELECT * FROM fevm_catalog_naren.lakemeter.estimate_databricks_apps_cost('AWS','us-east-1','ENTERPRISE','medium',2,730)"]),
 (["Estimate FM API cost for 1 billion input tokens to the Databricks-hosted bge-large model."],
  ["SELECT * FROM fevm_catalog_naren.lakemeter.estimate_fmapi_databricks_cost('AWS','us-east-1','ENTERPRISE','bge-large','input_token',1000000000)"]),
 (["Estimate cost to classify 1,000,000 short_text documents with ai_classify."],
  ["SELECT * FROM fevm_catalog_naren.lakemeter.estimate_ai_classify_cost('AWS','us-east-1','ENTERPRISE','short_text',1000000,NULL)"]),
 (["Compare serverless Jobs vs classic Jobs monthly cost for the same m5.xlarge driver + 10 m5.xlarge workers running 1 hour/day."],
  ["SELECT 'serverless' AS mode, total_cost_per_month FROM fevm_catalog_naren.lakemeter.estimate_serverless_compute_cost('AWS','us-east-1','ENTERPRISE','JOBS','m5.xlarge','m5.xlarge',10,'standard',1,60,30) UNION ALL SELECT 'classic', total_cost_per_month FROM fevm_catalog_naren.lakemeter.estimate_jobs_classic_cost('AWS','us-east-1','ENTERPRISE','m5.xlarge','m5.xlarge',10,false,1,60,30)"]),
]
example_question_sqls=sorted([{"id":nid(),"question":q,"sql":s} for q,s in examples], key=lambda x:x["id"])

sample_q=[
 "Estimate the monthly cost of a classic Jobs cluster: AWS us-east-1, m5.xlarge driver + 10 m5.xlarge workers, Photon, 1 hour/day.",
 "How much does a serverless SQL Medium warehouse cost for 160 hours a month?",
 "Cost of an ADVANCED DLT pipeline on 5 m5.xlarge workers running 2 hours daily.",
 "What does a Vector Search endpoint with 10 million vectors cost per month?",
 "Estimate a GPU model serving endpoint (gpu_small_t4) at concurrency 4.",
 "Monthly cost of a Lakebase instance with 4 capacity units and 2 HA nodes?",
 "Cost of 2 medium Databricks Apps running continuously?",
 "FM API cost for 1 billion input tokens to Databricks claude or bge-large?",
 "Cost to extract fields from 500,000 invoices with ai_extract?",
]
sample_questions=sorted([{"id":nid(),"question":[q]} for q in sample_q], key=lambda x:x["id"])

space={"version":2,"config":{"sample_questions":sample_questions},"data_sources":data_sources,
 "instructions":{"text_instructions":text_instructions,"example_question_sqls":example_question_sqls,"sql_functions":sql_functions}}

req={"warehouse_id":WID,
 "title":"Lakemeter Cost Estimator",
 "description":"Estimate Databricks workload costs (compute, SQL, AI/serving, FM API, Document AI, Lakebase, Apps) from Lakemeter pricing data using natural language. Backed by UC pricing tables and 14 estimate_* SQL functions.",
 "serialized_space":json.dumps(space)}
open("/tmp/create_space.json","w").write(json.dumps(req,indent=2))
print("functions:",len(sql_functions),"examples:",len(example_question_sqls),"samples:",len(sample_questions))
out=subprocess.run(["databricks","genie","create-space","--json","@/tmp/create_space.json","--profile",PROFILE,"-o","json"],capture_output=True,text=True)
print("STDOUT:",out.stdout[:300]); print("STDERR:",out.stderr[:300])
