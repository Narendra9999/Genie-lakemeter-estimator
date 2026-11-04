"""Generate the one-notebook setup using only DCP source artifacts."""
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
rd = lambda path: (ROOT/path).read_text()
space = json.loads(rd('genie/space_definition.json'))
space['text_instructions'] = [rd('genie/instructions.md').strip()]
space['serialized_space']['instructions']['text_instructions'][0]['content'] = space['text_instructions']
seed = json.loads(rd('calculator/rate_registry_seed.json'))
requests = {path.stem:json.loads(path.read_text()) for path in sorted((ROOT/'samples/requests').glob('*.json'))}
notebook = '''# Databricks notebook source
# MAGIC %md
# MAGIC # DCP Cost Estimator setup
# MAGIC Installs the DCP calculator, input schema, rate card, named requests and Genie configuration.
# MAGIC Requires a UC-enabled compute environment, CREATE SCHEMA/TABLE/FUNCTION permissions, access to the selected system billing table, and a Genie-compatible SQL warehouse.
# MAGIC A blank space_id creates a new agent. Supply its ID on later runs to update it. This notebook does not change other agents.

# COMMAND ----------
import json
import re
from databricks.sdk import WorkspaceClient

dbutils.widgets.text("catalog", "", "Catalog")
dbutils.widgets.text("schema", "dcp_estimator", "Schema")
dbutils.widgets.text("aws_pricing_catalog", "", "AWS pricing catalog")
dbutils.widgets.text("aws_pricing_schema", "", "AWS pricing schema")
dbutils.widgets.text("aws_pricing_table_name", "aws_pricing_list", "Existing AWS pricing table name")
dbutils.widgets.text("warehouse_id", "", "SQL warehouse ID")
dbutils.widgets.dropdown("rate_source", "ACCOUNT_PRICES", ["ACCOUNT_PRICES", "LIST_PRICES"], "Billing source")
dbutils.widgets.text("sku_region", "US_EAST_N_VIRGINIA", "Billing SKU suffix")
dbutils.widgets.text("space_id", "", "Existing agent ID to update; blank creates")
dbutils.widgets.text("space_title", "DCP Cost Estimator", "Agent title")
W = {key: dbutils.widgets.get(key).strip() for key in ("aws_pricing_catalog", "aws_pricing_schema", "aws_pricing_table_name", "catalog", "schema", "warehouse_id", "rate_source", "sku_region", "space_id", "space_title")}
if not W["catalog"] or not W["warehouse_id"]:
    raise ValueError("Provide catalog and warehouse_id")
if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", W[key]) for key in ("catalog", "schema", "aws_pricing_catalog", "aws_pricing_schema", "aws_pricing_table_name")):
    raise ValueError("Provide simple SQL identifiers for target catalog/schema and all three AWS pricing fields")
FQ = W["catalog"] + "." + W["schema"]
AWS_PRICING_TABLE = ".".join(W[key] for key in ("aws_pricing_catalog", "aws_pricing_schema", "aws_pricing_table_name"))
# Read-only validation before creating calculator objects. Never load or mutate source pricing.
aws_columns = set(spark.table(AWS_PRICING_TABLE).columns)
aws_required = {"service_name", "region", "usage_type", "usage_quantity_unit", "s3_storage_class", "charge_type", "price_unit", "list_price", "billed_rate", "rate_status", "period_start", "period_end"}
if aws_required - aws_columns:
    raise ValueError("AWS pricing table missing columns: " + ", ".join(sorted(aws_required - aws_columns)))
# Existing twelve-column tables need no provenance schema changes.
AWS_PRICING_SOURCE = AWS_PRICING_TABLE if "source_row_id" in aws_columns else "(SELECT *, sha2(to_json(struct(*)), 256) AS source_row_id FROM " + AWS_PRICING_TABLE + ")"
def deployment_sql(statement):
    return statement.replace("__FQ__.aws_pricing_list", AWS_PRICING_SOURCE).replace("__FQ__", FQ)
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {FQ}")

# COMMAND ----------
'''
# The source table is externally managed; install only the lookup function.
aws_lookup = rd('sql/14_aws_pricing.sql').split('\n-- @@\n', 1)[1].strip()
notebook += 'AWS_PRICING_SQL = ' + repr(aws_lookup) + '\n'
notebook += 'spark.sql(deployment_sql(AWS_PRICING_SQL))\n\n# COMMAND ----------\n'
notebook += 'CALC_SQL = ' + repr(rd('sql/06_dcp_calculator.sql').strip()) + '\n'
notebook += 'ENGINE_INPUTS_SQL = ' + repr(rd('sql/12_dcp_engine_inputs.sql').strip()) + '\n'
notebook += '''for statement in CALC_SQL.split("\\n-- @@\\n"):
    if statement.strip(): spark.sql(deployment_sql(statement))
spark.sql(ENGINE_INPUTS_SQL.replace("__FQ__", FQ))

# COMMAND ----------
'''
notebook += 'SEED = json.loads(' + repr(json.dumps(seed)) + ')\n' + rd('calculator/rate_refresh.py')
notebook += '\nrefresh_rates(spark, FQ, SEED, W["rate_source"], W["sku_region"].upper())\n\n# COMMAND ----------\n'
notebook += 'REQUESTS = json.loads(' + repr(json.dumps(requests)) + ')\n'
notebook += '''# Re-running setup updates bundled presets while retaining user-created requests.
from pyspark.sql import functions as sf
rows = [(key, value.get("use_case_name", key), json.dumps(value), "bundled DCP request") for key, value in REQUESTS.items()]
frame = spark.createDataFrame(rows, "use_case_id string, use_case_name string, request_json string, source string").withColumn("loaded_at", sf.current_timestamp())
frame.createOrReplaceTempView("dcp_bundled_requests")
spark.sql(f"""MERGE INTO {FQ}.dcp_requests AS target USING dcp_bundled_requests AS source
ON target.use_case_id = source.use_case_id
WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *""")

# COMMAND ----------
'''
notebook += 'SPACE = json.loads(' + repr(json.dumps(space)) + ')\n'
notebook += '''def substitute(value):
    if isinstance(value, str): return value.replace("__FQ__.aws_pricing_list", AWS_PRICING_TABLE).replace("__FQ__", FQ)
    if isinstance(value, list): return [substitute(item) for item in value]
    if isinstance(value, dict): return {key: substitute(item) for key, item in value.items()}
    return value
D = substitute(SPACE)
D["serialized_space"]["data_sources"]["tables"].sort(key=lambda table: table["identifier"])
body = {"warehouse_id": W["warehouse_id"], "title": W["space_title"], "description": D["description"], "serialized_space": json.dumps(D["serialized_space"])}
client = WorkspaceClient()
if W["space_id"]:
    agent = client.api_client.do("PATCH", "/api/2.0/genie/spaces/" + W["space_id"], body=body)
else:
    body["parent_path"] = "/Users/" + client.current_user.me().user_name
    agent = client.api_client.do("POST", "/api/2.0/genie/spaces", body=body)
space_id = agent.get("space_id", W["space_id"])
dbutils.jobs.taskValues.set(key="space_id", value=space_id)
print("DCP agent:", client.config.host + "/genie/rooms/" + space_id)
print("Save this agent ID for subsequent setup runs:", space_id)
'''
(ROOT/'setup_genie_dcp_agent.py').write_text(notebook)
print('Generated DCP-only setup notebook')
