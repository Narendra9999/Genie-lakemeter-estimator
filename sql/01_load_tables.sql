CREATE OR REPLACE TABLE ref_dbu_rates AS
SELECT * FROM read_files('/Volumes/fevm_catalog_naren/lakemeter/raw/dbu-rates.csv', format => 'csv', header => true, inferSchema => true)
-- @@
CREATE OR REPLACE TABLE ref_instance_dbu_rates AS
SELECT * FROM read_files('/Volumes/fevm_catalog_naren/lakemeter/raw/instance-dbu-rates.csv', format => 'csv', header => true, inferSchema => true)
-- @@
CREATE OR REPLACE TABLE ref_dbu_multipliers AS
SELECT * FROM read_files('/Volumes/fevm_catalog_naren/lakemeter/raw/dbu-multipliers.csv', format => 'csv', header => true, inferSchema => true)
-- @@
CREATE OR REPLACE TABLE ref_dbsql_rates AS
SELECT * FROM read_files('/Volumes/fevm_catalog_naren/lakemeter/raw/dbsql-rates.csv', format => 'csv', header => true, inferSchema => true)
-- @@
CREATE OR REPLACE TABLE ref_dbsql_warehouse_config AS
SELECT * FROM read_files('/Volumes/fevm_catalog_naren/lakemeter/raw/dbsql-warehouse-config.csv', format => 'csv', header => true, inferSchema => true)
-- @@
CREATE OR REPLACE TABLE ref_serverless_rates AS
SELECT * FROM read_files('/Volumes/fevm_catalog_naren/lakemeter/raw/serverless-rates.csv', format => 'csv', header => true, inferSchema => true)
-- @@
CREATE OR REPLACE TABLE ref_sku_region_map AS
SELECT * FROM read_files('/Volumes/fevm_catalog_naren/lakemeter/raw/sku-region-map.csv', format => 'csv', header => true, inferSchema => true)
-- @@
CREATE OR REPLACE TABLE ref_fmapi_databricks_rates AS
SELECT * FROM read_files('/Volumes/fevm_catalog_naren/lakemeter/raw/fmapi-databricks-rates.csv', format => 'csv', header => true, inferSchema => true)
-- @@
CREATE OR REPLACE TABLE ref_fmapi_proprietary_rates AS
SELECT * FROM read_files('/Volumes/fevm_catalog_naren/lakemeter/raw/fmapi-proprietary-rates.csv', format => 'csv', header => true, inferSchema => true)
-- @@
CREATE OR REPLACE TABLE ref_vm_costs AS
SELECT * FROM read_files('/Volumes/fevm_catalog_naren/lakemeter/raw/vm-costs_part*.csv', format => 'csv', header => true, inferSchema => true)
-- @@
SELECT 'ref_dbu_rates' t, count(*) n FROM ref_dbu_rates
UNION ALL SELECT 'ref_instance_dbu_rates', count(*) FROM ref_instance_dbu_rates
UNION ALL SELECT 'ref_dbu_multipliers', count(*) FROM ref_dbu_multipliers
UNION ALL SELECT 'ref_dbsql_rates', count(*) FROM ref_dbsql_rates
UNION ALL SELECT 'ref_dbsql_warehouse_config', count(*) FROM ref_dbsql_warehouse_config
UNION ALL SELECT 'ref_serverless_rates', count(*) FROM ref_serverless_rates
UNION ALL SELECT 'ref_sku_region_map', count(*) FROM ref_sku_region_map
UNION ALL SELECT 'ref_fmapi_databricks_rates', count(*) FROM ref_fmapi_databricks_rates
UNION ALL SELECT 'ref_fmapi_proprietary_rates', count(*) FROM ref_fmapi_proprietary_rates
UNION ALL SELECT 'ref_vm_costs', count(*) FROM ref_vm_costs
