# Databricks notebook source
# MAGIC %md
# MAGIC # 09 · Load the rate cards (Lakemeter price card + DCP rate card)
# MAGIC Refreshes **both** rate cards from the same system table, so every estimate uses the same governed price:
# MAGIC `ref_dbu_price_card` (every Lakemeter `estimate_*` and sizing DBU price, for the regions in `price_regions`) and
# MAGIC `dcp_rate_card` (DCP calculator). No single current price = PENDING (NULL), never a fallback.
# MAGIC
# MAGIC ### DCP rate card
# MAGIC The DCP calculator (v4.0.2) prices **only** with `dcp_rate_card`: one row per calculator rate record
# MAGIC (provider, sku, unit, region). This notebook seeds it with the calculator's reference registry and then
# MAGIC re-prices every Databricks DBU row from the chosen system table:
# MAGIC
# MAGIC | `rate_source` | Databricks DBU rates from | `status` | Use |
# MAGIC |---|---|---|---|
# MAGIC | `ACCOUNT_PRICES` | `system.billing.account_prices` (negotiated) | `GOVERNED` | Mastercard |
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
FQ = f"{dbutils.widgets.get('catalog').strip()}.{dbutils.widgets.get('schema').strip()}"
SRC = dbutils.widgets.get("rate_source"); SKU_REGION = dbutils.widgets.get("sku_region").strip().upper()

# COMMAND ----------

import json
SEED = json.loads("{\n \"calculator_version\": \"4.0.2\",\n \"billing_sku\": {\n  \"ENTERPRISE_JOBS_SERVERLESS\": \"ENTERPRISE_JOBS_SERVERLESS_COMPUTE\",\n  \"SQL_PRO\": \"ENTERPRISE_SQL_PRO_COMPUTE\",\n  \"ENTERPRISE_ALL_PURPOSE_COMPUTE\": \"ENTERPRISE_ALL_PURPOSE_COMPUTE\",\n  \"ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE\": \"ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE\",\n  \"ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE\": \"ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE\"\n },\n \"rows\": [\n  {\n   \"provider\": \"DATABRICKS\",\n   \"service\": \"JOBS\",\n   \"sku\": \"ENTERPRISE_JOBS_SERVERLESS\",\n   \"unit\": \"DBU\",\n   \"region\": \"US_EAST_1\",\n   \"rate\": 0.3465,\n   \"status\": \"GOVERNED\",\n   \"source\": \"system.billing.account_prices\",\n   \"effective_start\": \"2026-01-01\",\n   \"effective_end\": null,\n   \"observed_rate\": null,\n   \"notes\": \"\"\n  },\n  {\n   \"provider\": \"DATABRICKS\",\n   \"service\": \"SQL\",\n   \"sku\": \"SQL_PRO\",\n   \"unit\": \"DBU\",\n   \"region\": \"US_EAST_1\",\n   \"rate\": 0.4235,\n   \"status\": \"GOVERNED\",\n   \"source\": \"system.billing.account_prices\",\n   \"effective_start\": \"2026-01-01\",\n   \"effective_end\": null,\n   \"observed_rate\": null,\n   \"notes\": \"\"\n  },\n  {\n   \"provider\": \"DATABRICKS\",\n   \"service\": \"ALL_PURPOSE\",\n   \"sku\": \"ENTERPRISE_ALL_PURPOSE_COMPUTE\",\n   \"unit\": \"DBU\",\n   \"region\": \"US_EAST_1\",\n   \"rate\": 0.5005,\n   \"status\": \"GOVERNED\",\n   \"source\": \"system.billing.account_prices\",\n   \"effective_start\": \"2026-01-01\",\n   \"effective_end\": null,\n   \"observed_rate\": null,\n   \"notes\": \"\"\n  },\n  {\n   \"provider\": \"DATABRICKS\",\n   \"service\": \"ALL_PURPOSE\",\n   \"sku\": \"ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE\",\n   \"unit\": \"DBU\",\n   \"region\": \"US_EAST_1\",\n   \"rate\": 0.7315,\n   \"status\": \"GOVERNED\",\n   \"source\": \"system.billing.account_prices\",\n   \"effective_start\": \"2026-01-01\",\n   \"effective_end\": null,\n   \"observed_rate\": null,\n   \"notes\": \"\"\n  },\n  {\n   \"provider\": \"DATABRICKS\",\n   \"service\": \"FOUNDATION_MODEL_API\",\n   \"sku\": \"ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE\",\n   \"unit\": \"DBU\",\n   \"region\": \"US_EAST_1\",\n   \"rate\": 0.07,\n   \"status\": \"REFERENCE_ONLY\",\n   \"source\": \"Databricks public pricing cross-check\",\n   \"effective_start\": \"2026-09-21\",\n   \"effective_end\": null,\n   \"observed_rate\": null,\n   \"notes\": \"Model account rate $0.07/DBU is a planning fallback until the exact account rate is resolved.\"\n  },\n  {\n   \"provider\": \"AWS\",\n   \"service\": \"DATASYNC\",\n   \"sku\": \"DataSync-Bytes\",\n   \"unit\": \"GB\",\n   \"region\": \"US_EAST_1\",\n   \"rate\": 0.0125,\n   \"status\": \"VALIDATED_OBSERVED\",\n   \"source\": \"Cloudability validated sample\",\n   \"effective_start\": \"2026-08-01\",\n   \"effective_end\": null,\n   \"observed_rate\": 0.0095,\n   \"notes\": \"\"\n  },\n  {\n   \"provider\": \"AWS\",\n   \"service\": \"S3\",\n   \"sku\": \"TimedStorage-ByteHrs\",\n   \"unit\": \"GB_MONTH\",\n   \"region\": \"US_EAST_1\",\n   \"rate\": 0.023,\n   \"status\": \"REFERENCE_ONLY\",\n   \"source\": \"AWS S3 Standard list planning rate\",\n   \"effective_start\": \"2026-08-01\",\n   \"effective_end\": null,\n   \"observed_rate\": 0.02105655,\n   \"notes\": \"S3 storage uses the $0.023 list rate (matches the presented SEND figure); Cloudability observed $0.0211. Standard to be decided.\"\n  },\n  {\n   \"provider\": \"AWS\",\n   \"service\": \"S3\",\n   \"sku\": \"Requests-Tier1\",\n   \"unit\": \"REQUEST_1000\",\n   \"region\": \"US_EAST_1\",\n   \"rate\": 0.005,\n   \"status\": \"VALIDATED_OBSERVED\",\n   \"source\": \"Cloudability August 2026 export\",\n   \"effective_start\": \"2026-08-01\",\n   \"effective_end\": null,\n   \"observed_rate\": 0.0038,\n   \"notes\": \"Write, copy, list requests.\"\n  },\n  {\n   \"provider\": \"AWS\",\n   \"service\": \"S3\",\n   \"sku\": \"Requests-Tier2\",\n   \"unit\": \"REQUEST_1000\",\n   \"region\": \"US_EAST_1\",\n   \"rate\": 0.0004,\n   \"status\": \"VALIDATED_OBSERVED\",\n   \"source\": \"Cloudability August 2026 export\",\n   \"effective_start\": \"2026-08-01\",\n   \"effective_end\": null,\n   \"observed_rate\": 0.000304,\n   \"notes\": \"Read requests.\"\n  },\n  {\n   \"provider\": \"AWS\",\n   \"service\": \"KMS\",\n   \"sku\": \"KMS-Requests\",\n   \"unit\": \"REQUEST_10000\",\n   \"region\": \"US_EAST_1\",\n   \"rate\": 0.03,\n   \"status\": \"VALIDATED_OBSERVED\",\n   \"source\": \"Cloudability validated sample\",\n   \"effective_start\": \"2026-08-01\",\n   \"effective_end\": null,\n   \"observed_rate\": 0.0228,\n   \"notes\": \"\"\n  },\n  {\n   \"provider\": \"AWS\",\n   \"service\": \"KMS\",\n   \"sku\": \"KMS-Keys\",\n   \"unit\": \"KEY_MONTH\",\n   \"region\": \"US_EAST_1\",\n   \"rate\": 1.0,\n   \"status\": \"VALIDATED_OBSERVED\",\n   \"source\": \"Cloudability validated sample\",\n   \"effective_start\": \"2026-08-01\",\n   \"effective_end\": null,\n   \"observed_rate\": 0.76,\n   \"notes\": \"\"\n  }\n ]\n}")
table = "system.billing.account_prices" if SRC == "ACCOUNT_PRICES" else "system.billing.list_prices"
price = "pricing.default" if SRC == "ACCOUNT_PRICES" else "coalesce(pricing.effective_list.default, pricing.default)"
status = "GOVERNED" if SRC == "ACCOUNT_PRICES" else "REFERENCE_ONLY"
live = {}
for sku, bsku in SEED["billing_sku"].items():
    name = f"{bsku}_{SKU_REGION}"
    vals = [r[0] for r in spark.sql(f"""
        SELECT DISTINCT CAST({price} AS DOUBLE) FROM {table}
        WHERE upper(sku_name) = '{name}' AND upper(cloud) = 'AWS' AND usage_unit = 'DBU' AND currency_code = 'USD'
          AND price_start_time <= current_timestamp() AND (price_end_time IS NULL OR price_end_time > current_timestamp())
          AND {price} IS NOT NULL""").collect()]
    live[sku] = (vals[0] if len(vals) == 1 else None, name, len(vals))
rows = []
for r in SEED["rows"]:
    r = dict(r); bsku = None
    if r["provider"] == "DATABRICKS" and r["unit"] == "DBU" and r["region"] == "US_EAST_1" and r["sku"] in live:
        rate, bsku, n = live[r["sku"]]
        r.update(observed_rate=r["rate"], rate=rate, status=status if rate is not None else "PENDING",
                 source=table if rate is not None else f"{table}: {n} current prices for {bsku} (exactly one required)",
                 effective_start="2026-01-01", effective_end=None,
                 notes="" if SRC == "ACCOUNT_PRICES" else "List price used for testing. Mastercard estimates use system.billing.account_prices.")
    rows.append((r["provider"], r["service"], r["sku"], r["unit"], r["region"], r["rate"], r["status"], r["source"],
                 r["effective_start"], r["effective_end"], r["observed_rate"], r["notes"], bsku))
df = spark.createDataFrame(rows, "provider string, service string, sku string, unit string, region string, rate double, status string, "
                                 "source string, effective_start string, effective_end string, observed_rate double, notes string, billing_sku string")
df.selectExpr("*", "current_timestamp() AS refreshed_at").write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{FQ}.dcp_rate_card")
display(spark.sql(f"SELECT provider, sku, unit, region, rate, status, observed_rate AS calculator_registry_rate, source FROM {FQ}.dcp_rate_card ORDER BY 1, 2"))
# Lakemeter price card (estimate_* and sizing functions)
regions = [r.strip().lower() for r in dbutils.widgets.get("price_regions").split(",") if r.strip()]
region_filter = "true" if [r.upper() for r in regions] == ["ALL"] else "lower(region) IN (" + ", ".join(repr(r) for r in regions) + ")"
PRICE_CARD_SQL = "CREATE OR REPLACE TABLE __FQ__.ref_dbu_price_card\nCOMMENT 'Price per DBU used by every Lakemeter estimate_* and sizing function: one row per cloud, region, tier and product. status GOVERNED (system.billing.account_prices), REFERENCE_ONLY (system.billing.list_prices, test workspaces) or PENDING (no single current price - estimates show PENDING, never a fallback). list_price_reference = Lakemeter CSV list price, for comparison only.'\nAS\nWITH k AS (\n  SELECT upper(cloud) AS cloud, lower(region) AS region, upper(tier) AS tier, upper(sku_name) AS product_type, max(price_per_dbu) AS list_ref\n  FROM __FQ__.ref_dbu_rates\n  WHERE sku_name IS NOT NULL AND region IS NOT NULL AND tier IS NOT NULL AND {{REGION_FILTER}}\n  GROUP BY ALL),\nm AS (\n  SELECT k.*, upper(s.sku_region) AS sku_region\n  FROM k LEFT JOIN (SELECT DISTINCT upper(cloud) AS cloud, lower(region_code) AS region_code, sku_region FROM __FQ__.ref_sku_region_map) s\n    ON s.cloud = k.cloud AND s.region_code = k.region),\np AS (\n  SELECT upper(sku_name) AS sku_name, upper(cloud) AS cloud, CAST({{PRICE_EXPR}} AS DOUBLE) AS price\n  FROM {{RATE_TABLE}}\n  WHERE currency_code = 'USD' AND {{PRICE_EXPR}} IS NOT NULL   -- any usage unit: storage SKUs are priced per GB-month\n    AND price_start_time <= current_timestamp() AND (price_end_time IS NULL OR price_end_time > current_timestamp())),\nc AS (\n  SELECT m.cloud, m.region, m.tier, m.product_type, m.sku_region, m.list_ref,\n         concat(m.tier, '_', m.product_type, '_', coalesce(m.sku_region, '?')) AS reg_sku, concat(m.tier, '_', m.product_type) AS glob_sku,\n         array_distinct(collect_list(pr.price)) AS reg_prices, array_distinct(collect_list(pg.price)) AS glob_prices\n  FROM m\n  LEFT JOIN p pr ON pr.cloud = m.cloud AND pr.sku_name = concat(m.tier, '_', m.product_type, '_', m.sku_region)\n  LEFT JOIN p pg ON pg.cloud = m.cloud AND pg.sku_name = concat(m.tier, '_', m.product_type)\n  GROUP BY ALL),\nd AS (\n  SELECT c.*, CASE WHEN size(reg_prices) > 0 THEN reg_sku ELSE glob_sku END AS billing_sku,\n         CASE WHEN size(reg_prices) > 0 THEN reg_prices ELSE glob_prices END AS prices\n  FROM c)\nSELECT cloud, region, tier, product_type, billing_sku,\n       CASE WHEN size(prices) = 1 THEN prices[0] END AS price_per_dbu,\n       CASE WHEN size(prices) = 1 THEN '{{STATUS}}' ELSE 'PENDING' END AS status,\n       CASE WHEN size(prices) = 1 THEN '{{RATE_TABLE}}'\n            ELSE concat('{{RATE_TABLE}}: ', size(prices), ' current prices for ', billing_sku,\n                        CASE WHEN size(prices) = 0 AND sku_region IS NOT NULL THEN concat(' or ', reg_sku) ELSE '' END, ' (exactly one required)') END AS source,\n       list_ref AS list_price_reference, current_timestamp() AS refreshed_at\nFROM d"
spark.sql(PRICE_CARD_SQL.replace("__FQ__", FQ).replace("{{RATE_TABLE}}", table).replace("{{PRICE_EXPR}}", price)
          .replace("{{STATUS}}", status).replace("{{REGION_FILTER}}", region_filter))
display(spark.sql(f"SELECT status, count(*) AS skus FROM {FQ}.ref_dbu_price_card GROUP BY 1"))
display(spark.sql(f"SELECT region, tier, product_type, billing_sku, price_per_dbu, status, list_price_reference, source FROM {FQ}.ref_dbu_price_card ORDER BY 1, 2, 3"))
pending = [k for k, v in live.items() if v[0] is None]
if pending:
    print(f"WARNING: no single current {table} price for {pending} - those lines will be PENDING.")
