"""Generate the estimator product catalog as a queryable Unity Catalog function."""
import json
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MANIFEST_PATH = os.path.join(ROOT, "genie", "catalog", "estimator_catalog.json")
OUTPUT_PATH = os.path.join(ROOT, "sql", "10_product_catalog.sql")

ENGINES = {"DCP_CALCULATOR", "LAKEMETER_FUNCTION", "SIZING", "NONE"}
IMPLEMENTATION_STATES = {"AVAILABLE", "PENDING", "NOT_PORTED"}
VALIDATION_STATES = {"BENCHMARKED", "PARTIAL", "UNTESTED", "NONE"}
FUNCTION_RE = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+__FQ__\.([A-Za-z_][A-Za-z_0-9]*)", re.I)


def load_catalog(path=MANIFEST_PATH):
    with open(path) as f:
        return json.load(f)


def quote(value):
    return "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"


def function_names(sql_path):
    with open(sql_path) as f:
        return {name.lower() for name in FUNCTION_RE.findall(f.read())}


def validate_catalog(catalog, sql02, sql06, sql08):
    categories = catalog.get("categories") or []
    products = catalog.get("products") or []
    category_ids = [x.get("id") for x in categories]
    product_ids = [x.get("id") for x in products]
    errors = []
    if not catalog.get("as_of"):
        errors.append("catalog as_of is required")
    if len(category_ids) != len(set(category_ids)):
        errors.append("duplicate category id")
    if len(product_ids) != len(set(product_ids)):
        errors.append("duplicate product id")
    if any(not x for x in category_ids + product_ids):
        errors.append("category and product ids must be non-empty")

    available = {
        "DCP_CALCULATOR": function_names(sql06),
        "LAKEMETER_FUNCTION": function_names(sql02),
        "SIZING": function_names(sql08),
    }
    for product in products:
        pid = product.get("id", "<unknown>")
        if product.get("category_id") not in category_ids:
            errors.append(f"{pid}: unknown category_id {product.get('category_id')!r}")
        if product.get("engine") not in ENGINES:
            errors.append(f"{pid}: invalid engine {product.get('engine')!r}")
        if product.get("implementation_state") not in IMPLEMENTATION_STATES:
            errors.append(f"{pid}: invalid implementation_state {product.get('implementation_state')!r}")
        if product.get("validation_state") not in VALIDATION_STATES:
            errors.append(f"{pid}: invalid validation_state {product.get('validation_state')!r}")
        if product.get("implementation_state") in {"PENDING", "NOT_PORTED"} and not product.get("status_note"):
            errors.append(f"{pid}: PENDING/NOT_PORTED requires a status_note")
        if product.get("implementation_state") == "NOT_PORTED" and product.get("engine") != "NONE":
            errors.append(f"{pid}: NOT_PORTED must not name a calculation engine")
        entrypoints = product.get("entrypoints") or []
        if product.get("engine") == "NONE" and entrypoints:
            errors.append(f"{pid}: NONE engine cannot have entrypoints")
        for name in entrypoints:
            if name.lower() not in available.get(product.get("engine"), set()):
                errors.append(f"{pid}: entrypoint {name!r} is not defined for {product.get('engine')}")
        for field in ("label", "use_when", "required_facts", "pricing_basis", "region_scope", "evidence_ref"):
            if not product.get(field):
                errors.append(f"{pid}: {field} is required")
    if errors:
        raise ValueError("Invalid estimator catalog:\n- " + "\n- ".join(errors))


def generate_sql(catalog):
    categories = {row["id"]: row for row in catalog["categories"]}
    rows = []
    for product in catalog["products"]:
        category = categories[product["category_id"]]
        entrypoints = ", ".join(product.get("entrypoints", []))
        values = [
            category["sort_order"], category["id"], category["label"],
            product["id"], product["label"], product["engine"], entrypoints,
            product["implementation_state"], product["validation_state"],
            product["use_when"], product["required_facts"], product["pricing_basis"],
            product["region_scope"], product["status_note"], product["evidence_ref"], catalog["as_of"],
        ]
        rows.append("  (" + ", ".join(str(v) if isinstance(v, int) else quote(v) for v in values) + ")")
    values_sql = ",\n".join(rows)
    return f"""CREATE OR REPLACE FUNCTION __FQ__.estimator_catalog(p_category STRING DEFAULT NULL)
RETURNS TABLE(category_id STRING, category STRING, product_id STRING, product STRING, engine STRING,
              entrypoints STRING, implementation_state STRING, validation_state STRING, use_when STRING,
              required_facts STRING, pricing_basis STRING, region_scope STRING, status_note STRING,
              evidence_ref STRING, catalog_as_of STRING)
COMMENT 'Estimator coverage catalog for discovery and routing. Omit p_category to list all categories and products; optionally pass a category id such as COMPUTE or SQL. The catalog distinguishes implementation from validation, documents pricing basis, and is not a price source or a calculator.'
RETURN
  SELECT category_id, category, product_id, product, engine, entrypoints, implementation_state, validation_state,
         use_when, required_facts, pricing_basis, region_scope, status_note, evidence_ref, catalog_as_of
  FROM VALUES
{values_sql}
  AS v(category_order, category_id, category, product_id, product, engine, entrypoints, implementation_state,
       validation_state, use_when, required_facts, pricing_basis, region_scope, status_note, evidence_ref, catalog_as_of)
  WHERE p_category IS NULL OR upper(trim(p_category)) IN (upper(category_id), upper(category))
  ORDER BY category_order, product_id
"""


def build():
    catalog = load_catalog()
    validate_catalog(
        catalog,
        os.path.join(ROOT, "sql", "02_functions.sql"),
        os.path.join(ROOT, "sql", "06_dcp_calculator.sql"),
        os.path.join(ROOT, "sql", "08_workload_sizing.sql"),
    )
    sql = generate_sql(catalog)
    with open(OUTPUT_PATH, "w") as f:
        f.write(sql)
    print(f"wrote sql/10_product_catalog.sql ({len(catalog['products'])} products)")


if __name__ == "__main__":
    build()
