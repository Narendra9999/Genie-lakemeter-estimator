# Databricks notebook source
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
SEED = json.loads('{"calculator_version": "4.0.2", "billing_sku": {"ENTERPRISE_JOBS_SERVERLESS": "ENTERPRISE_JOBS_SERVERLESS_COMPUTE", "SQL_PRO": "ENTERPRISE_SQL_PRO_COMPUTE", "ENTERPRISE_ALL_PURPOSE_COMPUTE": "ENTERPRISE_ALL_PURPOSE_COMPUTE", "ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE": "ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE", "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE": "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE"}, "rows": [{"provider": "DATABRICKS", "service": "JOBS", "sku": "ENTERPRISE_JOBS_SERVERLESS", "unit": "DBU", "region": "US_EAST_1", "rate": 0.3465, "status": "GOVERNED", "source": "system.billing.account_prices", "effective_start": "2026-01-01", "effective_end": null, "observed_rate": null, "notes": ""}, {"provider": "DATABRICKS", "service": "SQL", "sku": "SQL_PRO", "unit": "DBU", "region": "US_EAST_1", "rate": 0.4235, "status": "GOVERNED", "source": "system.billing.account_prices", "effective_start": "2026-01-01", "effective_end": null, "observed_rate": null, "notes": ""}, {"provider": "DATABRICKS", "service": "ALL_PURPOSE", "sku": "ENTERPRISE_ALL_PURPOSE_COMPUTE", "unit": "DBU", "region": "US_EAST_1", "rate": 0.5005, "status": "GOVERNED", "source": "system.billing.account_prices", "effective_start": "2026-01-01", "effective_end": null, "observed_rate": null, "notes": ""}, {"provider": "DATABRICKS", "service": "ALL_PURPOSE", "sku": "ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE", "unit": "DBU", "region": "US_EAST_1", "rate": 0.7315, "status": "GOVERNED", "source": "system.billing.account_prices", "effective_start": "2026-01-01", "effective_end": null, "observed_rate": null, "notes": ""}, {"provider": "DATABRICKS", "service": "FOUNDATION_MODEL_API", "sku": "ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE", "unit": "DBU", "region": "US_EAST_1", "rate": 0.07, "status": "REFERENCE_ONLY", "source": "Databricks public pricing cross-check", "effective_start": "2026-09-21", "effective_end": null, "observed_rate": null, "notes": "Model account rate $0.07/DBU is a planning fallback until the exact account rate is resolved."}, {"provider": "AWS", "service": "DATASYNC", "sku": "DataSync-Bytes", "unit": "GB", "region": "US_EAST_1", "rate": 0.0125, "status": "VALIDATED_OBSERVED", "source": "Cloudability validated sample", "effective_start": "2026-08-01", "effective_end": null, "observed_rate": 0.0095, "notes": ""}, {"provider": "AWS", "service": "S3", "sku": "TimedStorage-ByteHrs", "unit": "GB_MONTH", "region": "US_EAST_1", "rate": 0.023, "status": "REFERENCE_ONLY", "source": "AWS S3 Standard list planning rate", "effective_start": "2026-08-01", "effective_end": null, "observed_rate": 0.02105655, "notes": "S3 storage uses the $0.023 list rate (matches the presented SEND figure); Cloudability observed $0.0211. Standard to be decided."}, {"provider": "AWS", "service": "S3", "sku": "Requests-Tier1", "unit": "REQUEST_1000", "region": "US_EAST_1", "rate": 0.005, "status": "VALIDATED_OBSERVED", "source": "Cloudability August 2026 export", "effective_start": "2026-08-01", "effective_end": null, "observed_rate": 0.0038, "notes": "Write, copy, list requests."}, {"provider": "AWS", "service": "S3", "sku": "Requests-Tier2", "unit": "REQUEST_1000", "region": "US_EAST_1", "rate": 0.0004, "status": "VALIDATED_OBSERVED", "source": "Cloudability August 2026 export", "effective_start": "2026-08-01", "effective_end": null, "observed_rate": 0.000304, "notes": "Read requests."}, {"provider": "AWS", "service": "KMS", "sku": "KMS-Requests", "unit": "REQUEST_10000", "region": "US_EAST_1", "rate": 0.03, "status": "VALIDATED_OBSERVED", "source": "Cloudability validated sample", "effective_start": "2026-08-01", "effective_end": null, "observed_rate": 0.0228, "notes": ""}, {"provider": "AWS", "service": "KMS", "sku": "KMS-Keys", "unit": "KEY_MONTH", "region": "US_EAST_1", "rate": 1.0, "status": "VALIDATED_OBSERVED", "source": "Cloudability validated sample", "effective_start": "2026-08-01", "effective_end": null, "observed_rate": 0.76, "notes": ""}]}')
"""Refresh the DCP rate card; missing or ambiguous DBU prices remain pending."""
import re


def refresh_rates(spark, fq, seed, rate_source, sku_region):
    if rate_source not in ('ACCOUNT_PRICES', 'LIST_PRICES'):
        raise ValueError('rate_source must be ACCOUNT_PRICES or LIST_PRICES')
    if not re.fullmatch(r'[A-Z0-9_]+', sku_region):
        raise ValueError('sku_region must be a billing SKU suffix')
    table = 'system.billing.account_prices' if rate_source == 'ACCOUNT_PRICES' else 'system.billing.list_prices'
    price = 'pricing.default' if rate_source == 'ACCOUNT_PRICES' else 'coalesce(pricing.effective_list.default, pricing.default)'
    status = 'GOVERNED' if rate_source == 'ACCOUNT_PRICES' else 'REFERENCE_ONLY'
    live = {}
    for sku, billing_sku in seed['billing_sku'].items():
        name = billing_sku + '_' + sku_region
        values = [row[0] for row in spark.sql(f"""
            SELECT DISTINCT CAST({price} AS DOUBLE) FROM {table}
            WHERE upper(sku_name) = '{name}' AND upper(cloud) = 'AWS'
              AND usage_unit = 'DBU' AND currency_code = 'USD'
              AND price_start_time <= current_timestamp()
              AND (price_end_time IS NULL OR price_end_time > current_timestamp())
              AND {price} IS NOT NULL""").collect()]
        live[sku] = (values[0] if len(values) == 1 else None, name, len(values))
    rows = []
    for original in seed['rows']:
        row = dict(original)
        billing_sku = None
        if row['provider'] == 'DATABRICKS' and row['unit'] == 'DBU':
            rate, billing_sku, count = live.get(row['sku'], (None, None, 0)) if row['region'] == 'US_EAST_1' else (None, None, 0)
            row.update(observed_rate=row['rate'], rate=rate,
                       status=status if rate is not None else 'PENDING',
                       source=table if rate is not None else f'{table}: {count} current prices for {billing_sku} (exactly one required)',
                       effective_start='2026-01-01', effective_end=None,
                       notes='' if rate_source == 'ACCOUNT_PRICES' else 'List price used for testing; production estimates use system.billing.account_prices.')
        rows.append(tuple(row.get(field) for field in ('provider', 'service', 'sku', 'unit', 'region', 'rate', 'status', 'source', 'effective_start', 'effective_end', 'observed_rate', 'notes')) + (billing_sku,))
    frame = spark.createDataFrame(rows, 'provider string, service string, sku string, unit string, region string, rate double, status string, source string, effective_start string, effective_end string, observed_rate double, notes string, billing_sku string')
    frame.selectExpr('*', 'current_timestamp() AS refreshed_at').write.mode('overwrite').option('overwriteSchema', 'true').saveAsTable(f'{fq}.dcp_rate_card')

refresh_rates(spark, FQ, SEED, dbutils.widgets.get("rate_source"), dbutils.widgets.get("sku_region").strip().upper())
display(spark.sql(f"SELECT provider, sku, rate, status, source FROM {FQ}.dcp_rate_card ORDER BY provider, sku"))
