"""Guard exact AWS source selection, audit-only dates and calculator-unit conversion."""
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
    def test_source_dates_do_not_restrict_selection(self):
        for day in ('2026-06-01','2026-09-30'): self.assertIsNotNone(lookup.select_rate(self.rows,self.storage,day))
        for day in ('2026-05-31','2026-10-01','2026-11-04'): self.assertIsNotNone(lookup.select_rate(self.rows,self.storage,day))
    def test_unit_conversion_and_confirmed_datasync(self):
        kms=next(m for m in lookup.MAPPINGS if m[1]=='KMS-Requests')
        self.assertAlmostEqual(lookup.select_rate(self.rows,kms,'2026-09-01'),0.0111425)
        self.assertAlmostEqual(lookup.select_rate(self.rows,lookup.MAPPINGS[0],'2026-09-01'),0.00395004)
        self.assertAlmostEqual(lookup.select_rate(self.rows,lookup.MAPPINGS[0],'2026-09-01','LIST'),0.00695098)
    def test_exact_matches_duplicates_and_zero(self):
        row=next(dict(x) for x in self.rows if x['usage_type']=='TimedStorage-ByteHrs' and x['region']=='us-east-1' and x['s3_storage_class']=='Standard')
        for field,value in [('region','us-west-2'),('s3_storage_class','Glacier'),('usage_type','USE2-TimedStorage-ByteHrs'),('price_unit','unknown')]:
            changed={**row,field:value};self.assertIsNone(lookup.select_rate([changed],self.storage,'2026-09-01'))
        self.assertEqual(lookup.select_rate([row,dict(row)],self.storage,'2026-09-01'),row['billed_rate'])
        self.assertIsNone(lookup.select_rate([{**row,'billed_rate':None}],self.storage,'2026-09-01'))
        self.assertEqual(lookup.select_rate([{**row,'billed_rate':0}],self.storage,'2026-09-01'),0)
    def test_runtime_replaces_legacy_rates_and_rejects_invalid_metadata(self):
        rates={'as_of':'2026-11-04','source':'test','aws_rows':self.rows,'rows':[{'provider':'AWS','rate':999}]}
        result=lookup.apply_aws_rates({'_aws_pricing':{'as_of':'2026-09-01','basis':'LIST'}},rates)
        storage=next(x for x in result['rows'] if x['sku']=='TimedStorage-ByteHrs')
        self.assertEqual(storage['rate'],0.01693344)
        self.assertIn('source_row_id',storage['notes'])
        self.assertEqual(next(x for x in lookup.apply_aws_rates({},rates)['rows'] if x['sku']=='TimedStorage-ByteHrs')['rate'],0.00999606)
        self.assertEqual(result['as_of'],rates['as_of'])
        self.assertEqual(lookup.apply_aws_rates({'_aws_pricing':{'as_of':'bad'}},rates)['as_of'],rates['as_of'])
        for meta in ({'basis':'unsupported'},):
            with self.assertRaises(ValueError):lookup.apply_aws_rates({'_aws_pricing':meta},rates)

    def test_missing_or_reversed_periods_are_audit_only(self):
        row=next(dict(x) for x in self.rows if x['usage_type']=='TimedStorage-ByteHrs' and x['region']=='us-east-1' and x['s3_storage_class']=='Standard')
        for dates in ({'period_start':None,'period_end':None},{'period_start':'2027-01-01','period_end':'2020-01-01'}):
            self.assertEqual(lookup.select_rate([{**row,**dates}],self.storage,'2026-11-04'),row['billed_rate'])
        self.assertEqual(lookup.select_rate([row,{**row,'period_start':'2027-01-01','period_end':'2027-12-31'}],self.storage,'2026-11-04'),row['billed_rate'])

    def test_datasync_confirmation_is_limited_to_exact_charge_and_unknown_unit(self):
        row=next(dict(x) for x in self.rows if x['service_name']=='AWS DataSync' and x['usage_type']=='USE1-Transferred-Bytes' and x['region']=='us-east-1' and x['charge_type']=='Data copied (per GB)')
        for field,value in [('charge_type','Other transfer'),('price_unit','per hour'),('region','us-west-2')]:
            self.assertIsNone(lookup.select_rate([{**row,field:value}],lookup.MAPPINGS[0],'2026-11-05'))
        result=lookup.apply_aws_rates({},dict(as_of='2026-11-05',source='test',rows=[],aws_rows=[row]))
        rate=next(x for x in result['rows'] if x['service']=='DATASYNC')
        self.assertIn('User confirmed',rate['notes'])
        self.assertIn('per unit (unit not reported)',rate['notes'])

    def test_one_usable_duplicate_is_selected_deterministically(self):
        row=next(dict(x) for x in self.rows if x['usage_type']=='TimedStorage-ByteHrs' and x['region']=='us-east-1' and x['s3_storage_class']=='Standard')
        first={**row,'source_row_id':'a','billed_rate':0.02,'list_price':None}
        second={**row,'source_row_id':'b','billed_rate':0.01,'list_price':0.03}
        invalid={**row,'source_row_id':'0','billed_rate':None,'list_price':None}
        for rows in ([second,invalid,first],[first,second,invalid]):
            self.assertEqual(lookup.select_rate(rows,self.storage,basis='BILLED'),0.02)
            self.assertEqual(lookup.select_rate(rows,self.storage,basis='LIST'),0.03)
            result=lookup.apply_aws_rates({},dict(as_of='2026-11-05',source='test',rows=[],aws_rows=rows))
            storage=next(x for x in result['rows'] if x['sku']=='TimedStorage-ByteHrs')
            notes=__import__('json').loads(storage['notes'])
            self.assertEqual(notes['match_count'],3)
            self.assertEqual(notes['usable_match_count'],2)
            self.assertEqual(notes['source_rows'][0]['source_row_id'],'a')
