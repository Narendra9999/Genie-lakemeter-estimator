from pathlib import Path
from aws_pricing import generate_sql
ROOT=Path(__file__).resolve().parents[1]
(ROOT/"sql/14_aws_pricing.sql").write_text(generate_sql())
