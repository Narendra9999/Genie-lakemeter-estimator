"""Generate the SQL AWS lookup from the shared mapping contract."""
from aws_rate_lookup import MAPPINGS, select_rate, apply_aws_rates

def generate_sql():
    q=lambda x:'NULL' if x is None else "'"+str(x).replace("'","''")+"'"
    values=',\n'.join('('+','.join([q(v) for v in m[:-1]]+[str(m[-1])])+')' for m in MAPPINGS)
    return """CREATE TABLE IF NOT EXISTS __FQ__.aws_pricing_list (
 service_name STRING, region STRING, usage_type STRING, usage_quantity_unit STRING,
 s3_storage_class STRING, charge_type STRING, price_unit STRING,
 list_price DOUBLE, billed_rate DOUBLE, rate_status STRING, period_start DATE, period_end DATE,
 source_file STRING, source_file_sha256 STRING, source_row_number INT, source_row_id STRING, loaded_at TIMESTAMP)
USING DELTA
COMMENT 'Cloudability-derived AWS reference rates. Source period_start/end retained for audit only; date validation is disabled. Preserve reference status.'
-- @@
CREATE OR REPLACE FUNCTION __FQ__.dcp_aws_rates(p_as_of DATE, p_basis STRING)
RETURNS TABLE(provider STRING, service STRING, sku STRING, unit STRING, region STRING,
 rate DOUBLE, status STRING, source STRING, effective_start STRING, effective_end STRING,
 observed_rate DOUBLE, notes STRING)
COMMENT 'Exact AWS reference-rate lookup. BILLED selects billed_rate; LIST selects list_price. Both remain REFERENCE_ONLY. Source period dates are audit-only; p_as_of is retained for compatibility and ignored for AWS rate selection. DataSync Data copied (per GB) is user-confirmed per GB when source unit is unreported. No ambiguous, other unknown-unit or registry fallback. S3 currently Standard in us-east-1 only. KMS per-1000 request prices normalize to calculator per-10000 units.'
RETURN
WITH mappings AS (SELECT * FROM VALUES
"""+values+"""
 AS m(service,sku,unit,service_name,usage_type,storage_class,charge_type,price_unit,unit_factor)),
candidates AS (
 SELECT a.*, b.basis, CASE WHEN b.basis='BILLED' THEN a.billed_rate ELSE a.list_price END AS selected_rate
 FROM __FQ__.aws_pricing_list a CROSS JOIN (SELECT * FROM VALUES ('BILLED'),('LIST') AS b(basis)) b
 WHERE b.basis=p_basis),
matched AS (
 SELECT m.*, count(a.source_row_id) OVER (PARTITION BY m.sku) AS match_count,
 a.source_row_id, a.price_unit AS actual_unit, a.rate_status, a.period_start, a.period_end,
 a.list_price, a.billed_rate, a.basis, a.selected_rate
 FROM mappings m LEFT JOIN candidates a
 ON a.service_name=m.service_name AND a.region='us-east-1' AND a.usage_type=m.usage_type
 AND a.s3_storage_class <=> m.storage_class AND a.charge_type=m.charge_type
),
resolved AS (
 SELECT *, match_count=1 AND (actual_unit=price_unit OR (service='DATASYNC' AND sku='DataSync-Bytes' AND charge_type='Data copied (per GB)' AND actual_unit='per unit (unit not reported)')) AND selected_rate>=0
 AND rate_status LIKE 'REFERENCE_ONLY%' AS usable FROM matched)
SELECT DISTINCT 'AWS', service, sku, unit, 'US_EAST_1',
 CASE WHEN usable THEN selected_rate*unit_factor END,
 CASE WHEN usable THEN 'REFERENCE_ONLY' ELSE 'PENDING' END,
 concat('__FQ__.aws_pricing_list; basis=',coalesce(basis,'UNRESOLVED'),'; source_row_id=',coalesce(source_row_id,'NONE')),
 '0001-01-01', cast(NULL AS STRING),
 CASE WHEN usable THEN list_price*unit_factor END,
 to_json(named_struct('basis',basis,'source_period_start',period_start,'source_period_end',period_end,'period_validation_applied',false,
 'unit_confirmation',CASE WHEN service='DATASYNC' AND actual_unit='per unit (unit not reported)' THEN 'User confirmed Data copied (per GB) is priced per GB' END,
 'source_status',rate_status,'price_unit',actual_unit,'calculator_unit',unit,'unit_factor',unit_factor,
 'matches',match_count,'list_price',list_price,'billed_rate',billed_rate,
 'warning',CASE WHEN usable THEN 'Cloudability-derived reference; source period date validation disabled.' ELSE 'PENDING: missing/ambiguous matching row, unsupported price unit, invalid basis or unusable rate. No fallback.' END))
FROM resolved
"""


