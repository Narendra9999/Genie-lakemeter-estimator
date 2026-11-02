"""Exercise DCP rate refresh without a Spark cluster."""
import json
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'calculator'))
from rate_refresh import refresh_rates

class SparkStub:
    def __init__(self, prices): self.prices=prices; self.rows=None; self.table=None; self.queries=[]
    def sql(self, query): self.queries.append(query); return self
    def collect(self): return [(value,) for value in self.prices]
    def createDataFrame(self, rows, schema): self.rows=rows; return self
    def selectExpr(self,*args): return self
    @property
    def write(self): return self
    def mode(self,*args): return self
    def option(self,*args): return self
    def saveAsTable(self,table): self.table=table

class RateRefreshTests(unittest.TestCase):
    def setUp(self): self.seed=json.loads((ROOT/'calculator/rate_registry_seed.json').read_text())
    def test_single_account_price_replaces_dbu_registry_and_keeps_aws_basis(self):
        spark=SparkStub([0.45]);refresh_rates(spark,'catalog.dcp',self.seed,'ACCOUNT_PRICES','US_EAST_N_VIRGINIA')
        self.assertEqual(spark.table,'catalog.dcp.dcp_rate_card')
        for original,row in zip(self.seed['rows'],spark.rows):
            if original['provider']=='DATABRICKS' and original['unit']=='DBU':
                self.assertEqual(row[5:8],(0.45,'GOVERNED','system.billing.account_prices'))
                self.assertEqual(row[10],original['rate'])
            else:
                self.assertEqual(row[5:8],(original['rate'],original['status'],original['source']))
    def test_missing_or_ambiguous_rates_never_fall_back_to_registry(self):
        for prices in [[],[0.4,0.5]]:
            with self.subTest(prices=prices):
                spark=SparkStub(prices);refresh_rates(spark,'catalog.dcp',self.seed,'ACCOUNT_PRICES','US_EAST_N_VIRGINIA')
                for row in spark.rows:
                    if row[0]=='DATABRICKS' and row[3]=='DBU':
                        self.assertIsNone(row[5]);self.assertEqual(row[6],'PENDING')
    def test_list_prices_are_explicitly_reference_only(self):
        spark=SparkStub([0.6]);refresh_rates(spark,'catalog.dcp',self.seed,'LIST_PRICES','US_EAST_N_VIRGINIA')
        for row in spark.rows:
            if row[0]=='DATABRICKS' and row[3]=='DBU':
                self.assertEqual(row[6:8],('REFERENCE_ONLY','system.billing.list_prices'))
    def test_invalid_configuration_is_rejected_before_queries(self):
        spark=SparkStub([])
        for source,region in [('OTHER','US_EAST_N_VIRGINIA'),('ACCOUNT_PRICES',"invalid' suffix")]:
            with self.assertRaises(ValueError):refresh_rates(spark,'catalog.dcp',self.seed,source,region)
        self.assertEqual(spark.queries,[])
