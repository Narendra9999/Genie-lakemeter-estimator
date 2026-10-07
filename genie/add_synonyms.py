#!/usr/bin/env python3
import json, subprocess
FQ="fevm_catalog_naren.lakemeter"; SPACE="01f1c274b84319229082a9b46e9f12e6"; WID="2b169603bdabc084"; PROFILE="fevm-naren"
def api(method, path):
    return json.loads(subprocess.run(["databricks","api",method,path,"--profile",PROFILE],capture_output=True,text=True).stdout)

def col(name, desc, syn=None, entity=False):
    c={"column_name":name,"description":[desc]}
    if syn: c["synonyms"]=syn
    if entity: c["enable_entity_matching"]=True
    return c

# column_configs per table (will be sorted by column_name)
COLS={
 f"{FQ}.ref_dbu_rates":[
   col("cloud","Cloud provider (AWS/AZURE/GCP).",["provider","csp"],True),
   col("price_per_dbu","Price per DBU in USD.",["dbu price","dbu rate","rate","price","dollars per dbu"]),
   col("region","Cloud region code (e.g. us-east-1).",["region"],True),
   col("sku_name","Billing SKU / product type, e.g. JOBS_COMPUTE, ALL_PURPOSE_COMPUTE, SQL_COMPUTE.",["sku","product","product type","workload sku"],True),
   col("tier","Pricing tier: STANDARD, PREMIUM, ENTERPRISE.",["pricing tier","plan","edition"],True),
 ],
 f"{FQ}.ref_vm_costs":[
   col("cloud","Cloud provider.",["provider","csp"],True),
   col("cost_per_hour","Cloud VM cost per hour in USD.",["vm cost","vm price","hourly cost","compute cost","instance cost","dollars per hour"]),
   col("instance_type","VM / node instance type (e.g. m5.xlarge).",["vm","node","node type","machine type","instance","ec2 type"],True),
   col("payment_option","Payment option (e.g. NA, 1yr, 3yr).",["commitment"]),
   col("pricing_tier","VM pricing tier: on_demand, reserved, spot.",["on demand","reserved","spot","purchase option"],True),
   col("region","Cloud region code (e.g. us-east-1).",["region"],True),
 ],
 f"{FQ}.ref_instance_dbu_rates":[
   col("dbu_rate","DBU per hour for this instance type.",["dbu per hour","dbus per hour","dbu rate"]),
   col("instance_type","VM / node instance type.",["vm","node","node type","machine type"],True),
   col("memory_gb","Memory in GB.",["ram","memory"]),
   col("vcpus","Virtual CPUs.",["cores","cpus","vcpu"]),
 ],
 f"{FQ}.ref_dbsql_rates":[
   col("dbu_per_hour","DBU per hour for the SQL warehouse size.",["dbu rate","dbus per hour"]),
   col("includes_compute","Whether VM/compute is bundled in the DBU price.",["compute bundled","vm included"]),
   col("warehouse_size","SQL warehouse size (e.g. Small, Medium, Large, 2X-Large).",["sql warehouse size","t-shirt size"],True),
   col("warehouse_type","SQL warehouse type: classic, pro, serverless.",["sql warehouse type"],True),
 ],
 f"{FQ}.ref_dbsql_warehouse_config":[
   col("driver_instance_type","Driver VM instance type for the warehouse.",["driver vm","driver node"]),
   col("warehouse_size","NOTE: holds the warehouse TYPE (classic/pro/serverless) despite the name.",["warehouse type"],True),
   col("warehouse_type","NOTE: holds the warehouse SIZE (e.g. 2X-Large) despite the name.",["warehouse size","t-shirt size"],True),
   col("worker_count","Number of worker nodes.",["workers","num workers"]),
   col("worker_instance_type","Worker VM instance type for the warehouse.",["worker vm","worker node"]),
 ],
 f"{FQ}.ref_serverless_rates":[
   col("dbu_rate","DBU rate for the serverless product/size.",["dbu rate","dbus per hour"]),
   col("product","Serverless product: model_serving, vector_search.",["serverless product"],True),
   col("size_or_model","GPU/CPU size for model serving, or vector search mode (standard/storage_optimized).",["gpu type","instance size","mode","vector search mode"],True),
 ],
 f"{FQ}.ref_sku_region_map":[
   col("region_code","Cloud region code (e.g. us-east-1).",["region","cloud region"],True),
   col("sku_region","SKU region name (e.g. US_EAST_N_VIRGINIA).",["sku region"],True),
 ],
 f"{FQ}.ref_fmapi_databricks_rates":[
   col("dbu_rate","DBU rate per unit (per input_divisor tokens, or per hour).",["rate","dbu rate"]),
   col("input_divisor","Token divisor the rate applies per (usually 1,000,000).",["token divisor","per tokens"]),
   col("is_hourly","Whether the rate is hourly (provisioned) vs token-based.",["hourly"]),
   col("model","Databricks-hosted foundation model name (e.g. bge-large).",["foundation model","llm","embedding model","model name"],True),
   col("rate_type","Rate type: input_token, output_token, provisioned, etc.",["token type"],True),
 ],
 f"{FQ}.ref_fmapi_proprietary_rates":[
   col("context_length","Context length bucket (e.g. all).",["context window"]),
   col("dbu_rate","DBU rate per unit (per input_divisor tokens, or per hour).",["rate","dbu rate"]),
   col("endpoint_type","Endpoint type (e.g. global).",["endpoint"]),
   col("model","Proprietary model name (e.g. claude-haiku-4-5).",["foundation model","llm","model name"],True),
   col("provider","Model provider: anthropic, openai, google.",["model provider","vendor"],True),
   col("rate_type","Rate type: input_token, output_token, batch_inference, etc.",["token type"],True),
 ],
 f"{FQ}.ref_dbu_multipliers":[
   col("feature","Feature the multiplier applies to (e.g. photon).",["feature"],True),
   col("multiplier","DBU multiplier value.",["dbu multiplier","factor"]),
   col("sku_type","SKU type the multiplier applies to.",["sku","product type"],True),
 ],
}

cur=api("get", f"/api/2.0/genie/spaces/{SPACE}?include_serialized_space=true")
etag=cur["etag"]
space=json.loads(cur["serialized_space"])
n=0
for t in space["data_sources"]["tables"]:
    cfgs=COLS.get(t["identifier"])
    if cfgs:
        t["column_configs"]=sorted(cfgs, key=lambda c:c["column_name"])
        n+=len(t["column_configs"])
req={"warehouse_id":WID,"etag":etag,"serialized_space":json.dumps(space)}
cmd=["databricks","genie","update-space",SPACE,"--serialized-space",req["serialized_space"],"--etag",etag,"--warehouse-id",WID,"--profile",PROFILE,"-o","json"]
out=subprocess.run(cmd,capture_output=True,text=True)
try:
    d=json.loads(out.stdout); print(f"updated OK | column_configs added: {n} across {sum(1 for t in space['data_sources']['tables'] if t.get('column_configs'))} tables | update_time:", d.get("update_time"))
except Exception:
    print("STDOUT:",out.stdout[:300]); print("STDERR:",out.stderr[:400])
