"""Generate the standalone DCP rate-refresh notebook."""
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
seed = json.loads((ROOT/'calculator/rate_registry_seed.json').read_text())
source = (ROOT/'calculator/rate_refresh.py').read_text()
notebook = '''# Databricks notebook source
# MAGIC %md
# MAGIC # Refresh DCP rates
# MAGIC Databricks DBU prices come from the selected system billing table. Missing or ambiguous prices stay PENDING. AWS rates retain their calculator-registry basis.

# COMMAND ----------
import json
import re

dbutils.widgets.text("catalog", "", "Catalog")
dbutils.widgets.text("schema", "dcp_estimator", "Schema")
dbutils.widgets.dropdown("rate_source", "ACCOUNT_PRICES", ["ACCOUNT_PRICES", "LIST_PRICES"], "Billing source")
dbutils.widgets.text("sku_region", "US_EAST_N_VIRGINIA", "Billing SKU suffix")
parts = [dbutils.widgets.get(key).strip() for key in ("catalog", "schema")]
if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
    raise ValueError("Provide catalog and schema as simple SQL identifiers")
FQ = ".".join(parts)

# COMMAND ----------
'''
notebook += 'SEED = json.loads(' + repr(json.dumps(seed)) + ')\n' + source
notebook += '\nrefresh_rates(spark, FQ, SEED, dbutils.widgets.get("rate_source"), dbutils.widgets.get("sku_region").strip().upper())\n'
notebook += 'display(spark.sql(f"SELECT provider, sku, rate, status, source FROM {FQ}.dcp_rate_card ORDER BY provider, sku"))\n'
(ROOT/'notebooks/09_load_rate_card.py').write_text(notebook)
print('Generated DCP rate-refresh notebook')
