# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Load Lakemeter pricing tables
# MAGIC Loads the 11 pricing CSVs from a **workspace folder** into Delta tables. Runs on a dedicated (or serverless) cluster.

# COMMAND ----------

dbutils.widgets.text("catalog", "main", "Target catalog")
dbutils.widgets.text("schema", "lakemeter", "Target schema")
dbutils.widgets.text("pricing_path", "", "Workspace folder with pricing CSVs (blank = ../pricing_data relative to this notebook)")
catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema").strip()
pricing_path = dbutils.widgets.get("pricing_path").strip()
load_tables = True
FQ = f"{catalog}.{schema}"
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
nb_path = ctx.notebookPath().get()
if not pricing_path:
    repo_dir = nb_path.rsplit("/", 1)[0].rsplit("/", 1)[0]  # parent of notebooks/
    pricing_path = "/Workspace" + repo_dir + "/pricing_data"
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {FQ} COMMENT 'Lakemeter cost-estimation data + functions'")
print("schema :", FQ)
print("pricing:", pricing_path)

# COMMAND ----------

TABLES = {
    "ref_dbu_rates": "dbu-rates.csv",
    "ref_instance_dbu_rates": "instance-dbu-rates.csv",
    "ref_dbu_multipliers": "dbu-multipliers.csv",
    "ref_dbsql_rates": "dbsql-rates.csv",
    "ref_dbsql_warehouse_config": "dbsql-warehouse-config.csv",
    "ref_serverless_rates": "serverless-rates.csv",
    "ref_sku_region_map": "sku-region-map.csv",
    "ref_fmapi_databricks_rates": "fmapi-databricks-rates.csv",
    "ref_fmapi_proprietary_rates": "fmapi-proprietary-rates.csv",
    "ref_vm_costs": "vm-costs_part*.csv",  # glob: part1 + part2
}

if load_tables:
    for tbl, pattern in TABLES.items():
        src = f"file:{pricing_path}/{pattern}"
        df = (spark.read.option("header", "true").option("inferSchema", "true")
              .option("mode", "PERMISSIVE").csv(src))
        df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{FQ}.{tbl}")
        print(f"  {tbl:32s} {df.count():>8d} rows")
    print("tables loaded.")
else:
    print("load_tables=false -> skipping table load")