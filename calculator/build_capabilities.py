"""Generate the Genie discovery reference table from mappings and authoritative product coverage."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
TABLE='cost_estimator_functional_capabilities'
def generate():
 catalog=json.loads((ROOT/'genie/catalog/estimator_catalog.json').read_text())
 products={p['id']:p for p in catalog['products']}
 mappings=json.loads((ROOT/'genie/catalog/functional_capabilities.json').read_text())
 assert len(mappings)==28 and len({m['capability_id'] for m in mappings})==28
 def q(v):return "'"+str(v).replace("'","''")+"'"
 rows=[]
 for m in mappings:
  ps=[products[k] for k in m['catalog_product_ids']]
  fields=[m['capability_id'],m['category'],m['workload_description'],m['engine'],m['estimate_type'],m['capability_kind'],', '.join(m['catalog_product_ids']),', '.join(dict.fromkeys(f for p in ps for f in p['entrypoints'])), '; '.join(dict.fromkeys(p['implementation_state'] for p in ps)), '; '.join(dict.fromkeys(p['validation_state'] for p in ps)), '; '.join(dict.fromkeys(p['required_facts'] for p in ps)), '; '.join(dict.fromkeys(p['region_scope'] for p in ps)), '; '.join(dict.fromkeys(p['pricing_basis'] for p in ps)), '; '.join(dict.fromkeys(p['status_note'] for p in ps)),catalog['as_of']]
  rows.append('('+str(m['sort_order'])+', '+', '.join(q(v) for v in fields)+')')
 names='sort_order, capability_id, category, workload_description, engine, estimate_type, capability_kind, catalog_product_ids, estimator_functions, implementation_state, validation_state, required_inputs, region_scope, pricing_basis, coverage_note, catalog_as_of'
 sql="CREATE OR REPLACE TABLE __FQ__."+TABLE+"\nUSING DELTA\nCOMMENT 'Cost Estimator Functional Capabilities: authoritative category discovery for DCP and Lakemeter, including workload sizing. Lists implemented categories, not a guarantee of validated pricing; always disclose implementation_state and validation_state. Use estimator_catalog for detailed coverage.'\nAS SELECT * FROM VALUES\n"+',\n'.join(rows)+'\nAS capabilities('+names+')\n'
 return sql
if __name__=='__main__':
 out=ROOT/'sql/11_functional_capabilities.sql';out.write_text(generate());print('Wrote',out.name,'with 28 capabilities')
