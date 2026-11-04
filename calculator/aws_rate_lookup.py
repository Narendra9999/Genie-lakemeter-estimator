"""Exact AWS billing-reference lookup; user-confirmed inclusive validity windows."""
from datetime import date
import json

MAPPINGS = [
 ('DATASYNC','DataSync-Bytes','GB','AWS DataSync','USE1-Transferred-Bytes',None,'Data copied (per GB)','per GB',1),
 ('S3','TimedStorage-ByteHrs','GB_MONTH','Amazon Simple Storage Service','TimedStorage-ByteHrs','Standard','Storage','per GB-month',1),
 ('S3','Requests-Tier1','REQUEST_1000','Amazon Simple Storage Service','Requests-Tier1','Standard','Write requests (PUT, COPY, POST, LIST)','per 1,000 requests',1),
 ('S3','Requests-Tier2','REQUEST_1000','Amazon Simple Storage Service','Requests-Tier2','Standard','Read requests (GET, SELECT, all other)','per 1,000 requests',1),
 ('KMS','KMS-Requests','REQUEST_10000','AWS Key Management Service','us-east-1-KMS-Requests',None,'KMS API requests (encrypt, decrypt, generate data key)','per 1,000 requests',10),
 ('KMS','KMS-Keys','KEY_MONTH','AWS Key Management Service','us-east-1-KMS-Keys',None,'KMS key storage','per key-month',1),
]

def select_rate(rows, mapping, as_of, basis='BILLED'):
    """Pure reference implementation used to test SQL mapping and unit conversion."""
    date.fromisoformat(as_of)
    if basis not in ('BILLED','LIST'): raise ValueError('basis must be BILLED or LIST')
    service,sku,unit,name,usage,storage,charge,price_unit,factor=mapping
    matches=[r for r in rows if r['service_name']==name and r['region']=='us-east-1' and r['usage_type']==usage and r.get('s3_storage_class')==storage and r['charge_type']==charge and r['period_start']<=as_of<=r['period_end']]
    if len(matches)!=1:return None
    row=matches[0];rate=row.get('billed_rate' if basis=='BILLED' else 'list_price')
    if row['price_unit']!=price_unit or rate is None or rate<0 or not (row.get('rate_status') or '').startswith('REFERENCE_ONLY'):return None
    return rate*factor

def apply_aws_rates(request, rates):
    """Select exact, dated AWS records before constructing the pricing provider."""
    if 'aws_rows' not in rates:
        return rates
    meta=request.get('_aws_pricing') or {}
    if not isinstance(meta,dict): raise ValueError('_aws_pricing must be an object')
    as_of=meta.get('as_of',rates['as_of'])
    date.fromisoformat(as_of)
    basis=str(meta.get('basis','BILLED')).upper()
    if basis not in ('BILLED','LIST'): raise ValueError('AWS basis must be BILLED or LIST')
    result=dict(rates,as_of=as_of,rows=[x for x in rates['rows'] if x['provider']!='AWS'])
    for mapping in MAPPINGS:
        service,sku,unit,name,usage,storage,charge,price_unit,factor=mapping
        value=select_rate(rates['aws_rows'],mapping,as_of,basis)
        candidates=[x for x in rates['aws_rows'] if x['service_name']==name and x['region']=='us-east-1' and x['usage_type']==usage and x.get('s3_storage_class')==storage and x['charge_type']==charge and x['period_start']<=as_of<=x['period_end']]
        source=[{k:x.get(k) for k in ('source_row_id','period_start','period_end','price_unit','list_price','billed_rate','rate_status')} for x in candidates]
        result['rows'].append(dict(provider='AWS',service=service,sku=sku,unit=unit,region='US_EAST_1',rate=value,status='REFERENCE_ONLY' if value is not None else 'PENDING',source='aws_pricing_list; basis='+basis+'; rows='+','.join(x.get('source_row_id','') for x in candidates),effective_start=as_of,effective_end=None,observed_rate=None,notes=json.dumps(dict(basis=basis,as_of=as_of,validity_end_inclusive=True,unit_factor=factor,source_rows=source))))
    result['source']=rates.get('source','')+'; aws_pricing_list; basis='+basis+'; as_of='+as_of
    return result
