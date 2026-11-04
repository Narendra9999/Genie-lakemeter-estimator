# AWS pricing for DCP estimates

`fevm_catalog_naren.dcp_estimator.aws_pricing_list` contains 8,151 rows imported from `New_query.xlsx`. The installer assumes an AWS pricing table already exists. Supply `aws_pricing_catalog`, `aws_pricing_schema` and `aws_pricing_table_name` (default `aws_pricing_list`). It validates the original twelve columns and reads the table without creating, importing or modifying it. If `source_row_id` is absent, a deterministic row hash supplies the audit identifier at query time. Original columns are preserved, alongside source filename, file SHA-256, Excel row number, row ID and load timestamp.

The user confirmed `period_start` and `period_end` are rate-validity dates; both endpoints are inclusive. This upload covers **2026-06-01 through 2026-09-30**. Prices retain their original **REFERENCE_ONLY derived from Cloudability, not AWS Price List** status. Confirmation of validity dates does not change source provenance.

DCP defaults to the SQL warehouse's current date and the **BILLED** basis. For an explicit dated estimate, include this request metadata alongside the normal workload and confirmed `_areas`:

```json
"_aws_pricing": {"as_of": "2026-09-01", "basis": "BILLED"}
```

Use `LIST` for a list-price comparison. There is no fallback from a missing billed rate to a list rate, from an expired period to a previous period, or to the old AWS rows in `dcp_rate_card`. Uncovered or ambiguous matches remain pending; invalid request metadata is rejected. The calculator does not treat pending cost as zero. Historical AWS selection does not establish historical Databricks pricing coverage.

| DCP component | Source and calculator units | Coverage |
|---|---|---|
| S3 retained storage | GB-month → GB_MONTH | Standard, us-east-1 |
| S3 writes / reads | per 1,000 requests → REQUEST_1000 | Exact Tier1 / Tier2 rows, Standard, us-east-1 |
| KMS requests | per 1,000 requests → REQUEST_10000; rate × 10 | us-east-1 |
| KMS keys | key-month → KEY_MONTH | us-east-1 |
| DataSync copy | Calculator needs GB | Pending: uploaded price unit is unknown |

The raw table includes other AWS products and regions, but these are not automatically supported DCP calculator capabilities. Exact service, usage type, region, storage class, charge type, unit and validity match are required. Overlapping matching rows are pending until resolved.

For 90 GB-month of S3 Standard on 2026-09-01, billed cost is `90 × 0.00999606 = $0.8996454`, and list cost is `90 × 0.01693344 = $1.5240096`.

`dcp_rates_json()` supplies non-AWS registry records and relevant raw AWS records to the existing calculator adapter. `calculator/aws_rate_lookup.py` selects and normalizes the AWS records before the unchanged calculator formulas run. `dcp_aws_rates(date, basis)` provides an auditable SQL lookup with the same mapping contract. Original source windows and row IDs remain in rate notes and the Delta table. The runtime represents the selected AWS snapshot as eligible from its requested date; this does not overwrite source validity dates.

Databricks DBU prices continue to use `dcp_rate_card`. Legacy AWS seed rows may remain in that registry for compatibility, but are excluded from runtime assembly.

Run `python3 calculator/build_all.py` after source changes, and `python3 -m unittest discover -s tests -q` for regression validation. The generated `setup_genie_dcp_agent.py` includes existing-table validation, lookup functions, runtime and Genie configuration. Table references in functions and Genie metadata resolve to the supplied AWS table, independently of the calculator catalog/schema.

## Validation

Verified live table count and distinct source IDs (8,151), historical billed/list mappings, KMS conversion, current-date pending rates, and full DCP storage estimates ($0.90 billed / $1.52 list for 90 GB-month). Expired-date storage line costs are NULL/PENDING. All 26 local tests pass; generated artifacts reproduce exactly. Live Genie conversations also selected the requested pricing date/basis and explained missing coverage. Presentation remains imperfect: one response exposed wide tool-result tables, and the pending response repeated a zero placeholder while explaining that it was not a priced estimate. These are response-formatting limitations; the underlying pending costs are not priced as zero.
