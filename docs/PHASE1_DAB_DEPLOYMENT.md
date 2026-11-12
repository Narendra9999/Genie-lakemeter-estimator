# Phase 1 deployment with DABs

This deployment packages the existing embedded-Python Phase 1 estimator. Phase 2 is a separate project and is not deployed here.

## Targets

- `fevm_validation`: isolated schema `fevm_catalog_naren.dcp_phase1_dab_validation`, separate jobs and a new test Genie agent. Reads the existing Phase 1 AWS table without modifying it. Uses LIST_PRICES for Databricks validation and leaves rate refresh paused.
- `mastercard`: production target with a configurable restricted bundle root and explicit service-principal run identity. Target values come from variables, never a personal fevm profile.
- Existing `dev` and `prod` targets remain available.

The warehouse ID is for Genie SQL queries. Setup and refresh are notebook job tasks that use serverless jobs compute; the target workspace must support that compute. A classic-only workspace needs an explicit compute configuration before deployment.

## Mastercard configuration

All customer-specific values remain `<configurable>` until supplied in the Mastercard workspace. No Mastercard connection or deployment has been attempted.

Authenticate to the explicitly selected customer profile using OAuth. Do not copy credentials into this repository. Provide the values below as `DATABRICKS_BUNDLE_VAR_*` environment variables in the same shell as the bundle commands, or use `--var` flags:

| Variable | Required value |
| --- | --- |
| `catalog`, `schema` | Dedicated target catalog/schema for calculator objects |
| `aws_pricing_catalog`, `aws_pricing_schema`, `aws_pricing_table_name` | Existing customer AWS price table |
| `warehouse_id` | Customer Genie-compatible SQL warehouse |
| `run_as_service_principal` | Customer service-principal application ID |
| `deployment_root` | Restricted customer workspace folder for bundle deployment state/files; grant only the deployment and run identities access |
| `genie_parent_path` | Existing workspace folder writable by the service principal, e.g. `/Workspace/Shared/DCP` |
| `rate_source` | `ACCOUNT_PRICES` for governed deployment; `LIST_PRICES` only if explicitly accepted as reference pricing |
| `sku_region` | Correct billing SKU region suffix; defaults to `US_EAST_N_VIRGINIA` |
| `space_id` | Blank for first installation; thereafter persist and supply the returned customer agent ID |
| `space_title` | Customer agent display name |
| `schedule_pause_status` | Keep `PAUSED` until setup and rate checks pass |

The caller and run identity require appropriate workspace/warehouse access. The run identity needs UC schema/table/function creation rights, read access to the existing AWS table and chosen billing source, and permission to create/update the Genie agent in the supplied folder. Customer group permissions must be granted explicitly according to customer policy; this bundle does not invent group names or grant all users access.

```bash
databricks bundle validate --strict --target mastercard --profile "<configurable>"
databricks bundle deploy --target mastercard --profile "<configurable>"
databricks bundle run setup_agent --target mastercard --profile "<configurable>"
```

For example, in one shell (replace each placeholder):

```bash
# Workspace URL: <configurable>; authenticate the customer profile separately.
export DATABRICKS_BUNDLE_VAR_catalog="<configurable>"
export DATABRICKS_BUNDLE_VAR_schema="<configurable>"
export DATABRICKS_BUNDLE_VAR_aws_pricing_catalog="<configurable>"
export DATABRICKS_BUNDLE_VAR_aws_pricing_schema="<configurable>"
export DATABRICKS_BUNDLE_VAR_aws_pricing_table_name="<configurable>"
export DATABRICKS_BUNDLE_VAR_warehouse_id="<configurable>"
export DATABRICKS_BUNDLE_VAR_run_as_service_principal="<configurable>"
export DATABRICKS_BUNDLE_VAR_deployment_root="<configurable>"
export DATABRICKS_BUNDLE_VAR_genie_parent_path="<configurable>"
export DATABRICKS_BUNDLE_VAR_space_title="DCP Cost Estimator"
```

Replace every `<configurable>` placeholder before running these commands. Placeholders are documentation values, not deployable resource names. Set all required variables before these commands. No customer profile, warehouse, catalog or principal is assumed.

## fevm validation

```bash
python3 -I -m unittest discover -s tests
databricks bundle validate --strict --target fevm_validation --profile fevm-naren
databricks bundle deploy --target fevm_validation --profile fevm-naren
databricks bundle run setup_agent --target fevm_validation --profile fevm-naren
```

Save the returned agent ID. Supply `--var space_id=RETURNED_ID` on subsequent deployments/runs to update it; leaving it blank creates another agent. Run Agent Mode regression tests against that test ID. Test-job schedules remain paused by default.

Setup creates/replaces calculator-managed objects and refreshes their rate card; do not point it at a shared schema inadvertently. It validates the AWS table's twelve required columns and never imports/replaces that source table. AWS period dates are audit metadata, not rate-validity filters. Existing duplicate and unit-normalization behavior stays in the calculator.

## Acceptance

Require a successful setup job, read-back of the new agent/configuration, callable calculator functions with no DATE/TIMESTAMP certified arguments, direct calculator checks and Agent Mode regression evidence. A successful bundle validation or deployment alone does not prove calculation correctness or production permissions. Customer runtime/permissions must still be checked in the customer workspace. Track known intermittent Agent Mode clarification failures rather than declaring them fixed when a later sample passes.
