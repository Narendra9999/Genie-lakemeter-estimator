"""Guard exact AWS source selection, validity and calculator-unit conversion."""
import csv
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('aws_lookup_test', ROOT/'calculator/aws_rate_lookup.py')
lookup=importlib.util.module_from_spec(spec);spec.loader.exec_module(lookup)

class AwsPricingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT/'data/aws_pricing_list.csv').open() as f: cls.rows=list(csv.DictReader(f))
        for row in cls.rows:
            for key in ('list_price','billed_rate'): row[key]=float(row[key]) if row[key] else None
            row['s3_storage_class']=row['s3_storage_class'] or None
        cls.storage=next(m for m in lookup.MAPPINGS if m[1]=='TimedStorage-ByteHrs')
    def test_uploaded_source_and_example(self):
        self.assertEqual(len(self.rows),8151)
        self.assertEqual(len({x['source_row_id'] for x in self.rows}),8151)
        self.assertAlmostEqual(90*lookup.select_rate(self.rows,self.storage,'2026-09-01'),0.8996454)
        self.assertAlmostEqual(90*lookup.select_rate(self.rows,self.storage,'2026-09-01','LIST'),1.5240096)
    def test_inclusive_dates_and_expiry(self):
        for day in ('2026-06-01','2026-09-30'): self.assertIsNotNone(lookup.select_rate(self.rows,self.storage,day))
        for day in ('2026-05-31','2026-10-01','2026-11-04'): self.assertIsNone(lookup.select_rate(self.rows,self.storage,day))
    def test_unit_conversion_and_unknown_datasync(self):
        kms=next(m for m in lookup.MAPPINGS if m[1]=='KMS-Requests')
        self.assertAlmostEqual(lookup.select_rate(self.rows,kms,'2026-09-01'),0.0111425)
        self.assertIsNone(lookup.select_rate(self.rows,lookup.MAPPINGS[0],'2026-09-01'))
    def test_exact_matches_duplicates_and_zero(self):
        row=next(dict(x) for x in self.rows if x['usage_type']=='TimedStorage-ByteHrs' and x['region']=='us-east-1' and x['s3_storage_class']=='Standard')
        for field,value in [('region','us-west-2'),('s3_storage_class','Glacier'),('usage_type','USE2-TimedStorage-ByteHrs'),('price_unit','unknown')]:
            changed={**row,field:value};self.assertIsNone(lookup.select_rate([changed],self.storage,'2026-09-01'))
        self.assertIsNone(lookup.select_rate([row,dict(row)],self.storage,'2026-09-01'))
        self.assertIsNone(lookup.select_rate([{**row,'billed_rate':None}],self.storage,'2026-09-01'))
        self.assertEqual(lookup.select_rate([{**row,'billed_rate':0}],self.storage,'2026-09-01'),0)
    def test_runtime_replaces_legacy_rates_and_rejects_invalid_metadata(self):
        rates={'as_of':'2026-11-04','source':'test','aws_rows':self.rows,'rows':[{'provider':'AWS','rate':999}]}
        result=lookup.apply_aws_rates({'_aws_pricing':{'as_of':'2026-09-01','basis':'LIST'}},rates)
        storage=next(x for x in result['rows'] if x['sku']=='TimedStorage-ByteHrs')
        self.assertEqual(storage['rate'],0.01693344)
        self.assertIn('source_row_id',storage['notes'])
        self.assertTrue(all(x['status']=='PENDING' and x['rate'] is None for x in lookup.apply_aws_rates({},rates)['rows']))
        for meta in ({'as_of':'bad'},{'basis':'unsupported'}):
            with self.assertRaises(ValueError):lookup.apply_aws_rates({'_aws_pricing':meta},rates)
