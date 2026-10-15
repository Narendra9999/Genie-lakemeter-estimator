-- DCP rate card for test workspaces WITHOUT negotiated account prices (list prices).
-- In production run notebooks/09_load_rate_card.py with rate_source = ACCOUNT_PRICES instead.
-- 1. seed with the calculator v4.0.2 reference registry (AWS rows are used as-is)
DELETE FROM __FQ__.dcp_rate_card
-- @@
INSERT INTO __FQ__.dcp_rate_card VALUES
 ('DATABRICKS', 'JOBS', 'ENTERPRISE_JOBS_SERVERLESS', 'DBU', 'US_EAST_1', 0.3465, 'GOVERNED', 'system.billing.account_prices', '2026-01-01', NULL, NULL, '', 'ENTERPRISE_JOBS_SERVERLESS_COMPUTE', current_timestamp()),
 ('DATABRICKS', 'SQL', 'SQL_PRO', 'DBU', 'US_EAST_1', 0.4235, 'GOVERNED', 'system.billing.account_prices', '2026-01-01', NULL, NULL, '', 'ENTERPRISE_SQL_PRO_COMPUTE', current_timestamp()),
 ('DATABRICKS', 'ALL_PURPOSE', 'ENTERPRISE_ALL_PURPOSE_COMPUTE', 'DBU', 'US_EAST_1', 0.5005, 'GOVERNED', 'system.billing.account_prices', '2026-01-01', NULL, NULL, '', 'ENTERPRISE_ALL_PURPOSE_COMPUTE', current_timestamp()),
 ('DATABRICKS', 'ALL_PURPOSE', 'ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE', 'DBU', 'US_EAST_1', 0.7315, 'GOVERNED', 'system.billing.account_prices', '2026-01-01', NULL, NULL, '', 'ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE', current_timestamp()),
 ('DATABRICKS', 'FOUNDATION_MODEL_API', 'ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE', 'DBU', 'US_EAST_1', 0.07, 'REFERENCE_ONLY', 'Databricks public pricing cross-check', '2026-09-21', NULL, NULL, 'Model account rate $0.07/DBU is a planning fallback until the exact account rate is resolved.', 'ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE', current_timestamp()),
 ('AWS', 'DATASYNC', 'DataSync-Bytes', 'GB', 'US_EAST_1', 0.0125, 'VALIDATED_OBSERVED', 'Cloudability validated sample', '2026-08-01', NULL, 0.0095, '', NULL, current_timestamp()),
 ('AWS', 'S3', 'TimedStorage-ByteHrs', 'GB_MONTH', 'US_EAST_1', 0.023, 'REFERENCE_ONLY', 'AWS S3 Standard list planning rate', '2026-08-01', NULL, 0.02105655, 'S3 storage uses the $0.023 list rate (matches the presented SEND figure); Cloudability observed $0.0211. Standard to be decided.', NULL, current_timestamp()),
 ('AWS', 'S3', 'Requests-Tier1', 'REQUEST_1000', 'US_EAST_1', 0.005, 'VALIDATED_OBSERVED', 'Cloudability August 2026 export', '2026-08-01', NULL, 0.0038, 'Write, copy, list requests.', NULL, current_timestamp()),
 ('AWS', 'S3', 'Requests-Tier2', 'REQUEST_1000', 'US_EAST_1', 0.0004, 'VALIDATED_OBSERVED', 'Cloudability August 2026 export', '2026-08-01', NULL, 0.000304, 'Read requests.', NULL, current_timestamp()),
 ('AWS', 'KMS', 'KMS-Requests', 'REQUEST_10000', 'US_EAST_1', 0.03, 'VALIDATED_OBSERVED', 'Cloudability validated sample', '2026-08-01', NULL, 0.0228, '', NULL, current_timestamp()),
 ('AWS', 'KMS', 'KMS-Keys', 'KEY_MONTH', 'US_EAST_1', 1.0, 'VALIDATED_OBSERVED', 'Cloudability validated sample', '2026-08-01', NULL, 0.76, '', NULL, current_timestamp())
-- @@
-- 2. Databricks DBU rows: list price from the dcp_list_price_rates snapshot; no list price -> PENDING (never the seed value)
MERGE INTO __FQ__.dcp_rate_card t
USING (SELECT c.sku, c.region, l.rate
       FROM __FQ__.dcp_rate_card c
       LEFT JOIN (SELECT sku_name, max_by(rate, price_start_time) AS rate FROM __FQ__.dcp_list_price_rates GROUP BY sku_name) l
         ON l.sku_name = c.billing_sku || '_US_EAST_N_VIRGINIA'
       WHERE c.provider = 'DATABRICKS' AND c.unit = 'DBU' AND c.region = 'US_EAST_1') s
ON t.sku = s.sku AND t.region = s.region AND t.provider = 'DATABRICKS' AND t.unit = 'DBU'
WHEN MATCHED THEN UPDATE SET
  rate = s.rate,
  status = CASE WHEN s.rate IS NULL THEN 'PENDING' ELSE 'REFERENCE_ONLY' END,
  source = 'system.billing.list_prices (list price; account rates not available in this workspace)',
  observed_rate = t.rate,
  notes = 'List price used for testing; production estimates use the governed rate from system.billing.account_prices.',
  effective_start = '2026-01-01', effective_end = NULL, refreshed_at = current_timestamp()
