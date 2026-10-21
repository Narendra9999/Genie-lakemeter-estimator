"""Generate notebooks/09_load_rate_card.py (the rate-refresh job).
Run calculator/build_sql.py first. Re-run both after replacing notebooks/dcp_estimator.py."""
import json, os
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sql = open(os.path.join(ROOT, "sql", "06_dcp_calculator.sql")).read()
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

print("wrote notebooks/09_load_rate_card.py (calculator v" + V + ")")
