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
