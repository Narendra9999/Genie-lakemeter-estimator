-- Governed DBU prices for the Lakemeter estimate_* and sizing functions.
-- Every Lakemeter price goes through get_dbu_price(cloud, region, tier, product_type). It now reads ref_dbu_price_card,
-- built from system.billing.account_prices (GOVERNED) or system.billing.list_prices (REFERENCE_ONLY, test workspaces).
-- Billing SKU = <TIER>_<PRODUCT>_<SKU REGION> for regional SKUs (e.g. ENTERPRISE_JOBS_SERVERLESS_COMPUTE_US_EAST_N_VIRGINIA),
-- else <TIER>_<PRODUCT> (e.g. ENTERPRISE_JOBS_COMPUTE). Exactly one current price is required; otherwise the row is
-- PENDING with rate NULL, and every estimate using it returns NULL cost (PENDING) - never a list-price fallback.
-- The Lakemeter CSV price is kept as list_price_reference for comparison only.
-- The price-card build statement has placeholders filled by the notebooks:
--   {{RATE_TABLE}} {{PRICE_EXPR}} {{STATUS}} {{REGION_FILTER}}
-- __FQ__ = catalog.schema.

CREATE TABLE IF NOT EXISTS __FQ__.ref_dbu_price_card (
  cloud STRING, region STRING, tier STRING, product_type STRING, billing_sku STRING, price_per_dbu DOUBLE,
  status STRING, source STRING, list_price_reference DOUBLE, refreshed_at TIMESTAMP)
COMMENT 'Price per DBU used by every Lakemeter estimate_* and sizing function: one row per cloud, region, tier and product. status GOVERNED (system.billing.account_prices), REFERENCE_ONLY (system.billing.list_prices, test workspaces) or PENDING (no single current price - estimates show PENDING, never a fallback). list_price_reference = Lakemeter CSV list price, for comparison only.'
-- @@
CREATE OR REPLACE TABLE __FQ__.ref_dbu_price_card
COMMENT 'Price per DBU used by every Lakemeter estimate_* and sizing function: one row per cloud, region, tier and product. status GOVERNED (system.billing.account_prices), REFERENCE_ONLY (system.billing.list_prices, test workspaces) or PENDING (no single current price - estimates show PENDING, never a fallback). list_price_reference = Lakemeter CSV list price, for comparison only.'
AS
WITH k AS (
  SELECT upper(cloud) AS cloud, lower(region) AS region, upper(tier) AS tier, upper(sku_name) AS product_type, max(price_per_dbu) AS list_ref
  FROM __FQ__.ref_dbu_rates
  WHERE sku_name IS NOT NULL AND region IS NOT NULL AND tier IS NOT NULL AND {{REGION_FILTER}}
  GROUP BY ALL),
m AS (
  SELECT k.*, upper(s.sku_region) AS sku_region
  FROM k LEFT JOIN (SELECT DISTINCT upper(cloud) AS cloud, lower(region_code) AS region_code, sku_region FROM __FQ__.ref_sku_region_map) s
    ON s.cloud = k.cloud AND s.region_code = k.region),
p AS (
  SELECT upper(sku_name) AS sku_name, upper(cloud) AS cloud, CAST({{PRICE_EXPR}} AS DOUBLE) AS price
  FROM {{RATE_TABLE}}
  WHERE currency_code = 'USD' AND {{PRICE_EXPR}} IS NOT NULL   -- any usage unit: storage SKUs are priced per GB-month
    AND price_start_time <= current_timestamp() AND (price_end_time IS NULL OR price_end_time > current_timestamp())),
c AS (
  SELECT m.cloud, m.region, m.tier, m.product_type, m.sku_region, m.list_ref,
         concat(m.tier, '_', m.product_type, '_', coalesce(m.sku_region, '?')) AS reg_sku, concat(m.tier, '_', m.product_type) AS glob_sku,
         array_distinct(collect_list(pr.price)) AS reg_prices, array_distinct(collect_list(pg.price)) AS glob_prices
  FROM m
  LEFT JOIN p pr ON pr.cloud = m.cloud AND pr.sku_name = concat(m.tier, '_', m.product_type, '_', m.sku_region)
  LEFT JOIN p pg ON pg.cloud = m.cloud AND pg.sku_name = concat(m.tier, '_', m.product_type)
  GROUP BY ALL),
d AS (
  SELECT c.*, CASE WHEN size(reg_prices) > 0 THEN reg_sku ELSE glob_sku END AS billing_sku,
         CASE WHEN size(reg_prices) > 0 THEN reg_prices ELSE glob_prices END AS prices
  FROM c)
SELECT cloud, region, tier, product_type, billing_sku,
       CASE WHEN size(prices) = 1 THEN prices[0] END AS price_per_dbu,
       CASE WHEN size(prices) = 1 THEN '{{STATUS}}' ELSE 'PENDING' END AS status,
       CASE WHEN size(prices) = 1 THEN '{{RATE_TABLE}}'
            ELSE concat('{{RATE_TABLE}}: ', size(prices), ' current prices for ', billing_sku,
                        CASE WHEN size(prices) = 0 AND sku_region IS NOT NULL THEN concat(' or ', reg_sku) ELSE '' END, ' (exactly one required)') END AS source,
       list_ref AS list_price_reference, current_timestamp() AS refreshed_at
FROM d
-- @@
CREATE OR REPLACE FUNCTION __FQ__.get_dbu_price(p_cloud STRING, p_region STRING, p_tier STRING, p_product_type STRING)
RETURNS DOUBLE
COMMENT 'Price per DBU (USD) from ref_dbu_price_card (governed account prices, or list prices in test workspaces). NULL = PENDING: no single current price for that SKU and region - the estimate cost is NULL, never a fallback.'
RETURN (SELECT price_per_dbu FROM __FQ__.ref_dbu_price_card
  WHERE cloud = upper(p_cloud) AND region = lower(p_region) AND tier = upper(p_tier) AND product_type = upper(p_product_type)
  LIMIT 1)
-- @@
CREATE OR REPLACE FUNCTION __FQ__.get_dbu_price_status(p_cloud STRING, p_region STRING, p_tier STRING, p_product_type STRING)
RETURNS STRING
COMMENT 'Where the DBU price for a SKU comes from: "<status> <rate>/DBU <billing sku> (<source>)", or PENDING with the reason.'
RETURN coalesce(
  (SELECT CASE WHEN status = 'PENDING' THEN concat('PENDING - ', source)
               ELSE concat(status, ' $', price_per_dbu, '/DBU ', billing_sku, ' (', source, ')') END
   FROM __FQ__.ref_dbu_price_card
   WHERE cloud = upper(p_cloud) AND region = lower(p_region) AND tier = upper(p_tier) AND product_type = upper(p_product_type) LIMIT 1),
  concat('PENDING - no price-card row for ', upper(p_tier), ' ', upper(p_product_type), ' in ', upper(p_cloud), ' ', lower(p_region),
         ' (region not loaded into ref_dbu_price_card)'))
