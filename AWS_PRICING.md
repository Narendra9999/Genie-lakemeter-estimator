# AWS pricing for DCP estimates

Setup reads an existing table configured with `aws_pricing_catalog`, `aws_pricing_schema`, and `aws_pricing_table_name`. The twelve original columns are required; setup does not create or import pricing data. Missing source row IDs are derived as hashes without changing the table.

AWS rate selection ignores `period_start` and `period_end`. These columns remain in source records and audit notes. No estimate is blocked because rates are outside those dates. Legacy request `_aws_pricing.as_of` and SQL helper `p_as_of` are retained for compatibility but ignored for AWS selection. Databricks date filtering remains unchanged and uses the original runtime date.

BILLED selects `billed_rate` by default. Request `"_aws_pricing": {"basis": "LIST"}` for `list_price`. Exact service, region, usage type, storage class, charge type and price units must match. Multiple matching source rows remain pending, even when they have different periods. No latest-period selection or legacy AWS registry fallback occurs. Source status remains REFERENCE_ONLY for the uploaded Cloudability data.

S3 Standard storage and requests and KMS keys/requests are mapped for us-east-1. KMS rates per 1,000 requests are multiplied by ten for calculator REQUEST_10000 units. The user confirmed that DataSync charge type `Data copied (per GB)` is priced per GB despite the source label `per unit (unit not reported)`. This exact mapped charge is normalized to GB without changing the source row. The confirmation is recorded in rate notes. Billed: $0.00395004/GB; list: $0.00695098/GB. Other unknown units remain pending. Other AWS products/regions are not automatically supported by the DCP calculator.

For 90 GB-month, billed cost is 90 × 0.00999606 = $0.8996454; list cost is 90 × 0.01693344 = $1.5240096, regardless of source period dates.

`dcp_rates_json()` reads Databricks registry rows and AWS source rows. `calculator/aws_rate_lookup.py` selects AWS records and normalizes units before the existing calculator formulas execute. `dcp_aws_rates(p_as_of, p_basis)` exposes the equivalent SQL lookup. Dates are audit-only for AWS; missing or ambiguous prices remain pending, never zero.
