"""Create a separate DCP-only Genie space using existing UC calculator objects."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', required=True)
    p.add_argument('--schema', required=True, help='catalog.schema')
    p.add_argument('--warehouse', required=True)
    p.add_argument('--parent-path', required=True)
    p.add_argument('--space-id', help='Explicitly update this DCP-only agent instead of creating')
    a = p.parse_args()
    space = json.loads((ROOT / 'serialized_space.json').read_text().replace('__FQ__', a.schema))
    payload = dict(title='DCP Calculator', description='Validate workload inputs and scope, ask for missing facts, then calculate DCP costs and engine breakdowns. DCP only.', warehouse_id=a.warehouse, serialized_space=json.dumps(space))
    if not a.space_id:
        payload['parent_path'] = a.parent_path
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'payload.json'
        path.write_text(json.dumps(payload))
        action = ['update-space', a.space_id] if a.space_id else ['create-space']
        result = subprocess.run(['databricks', 'genie', *action, '--profile', a.profile, '--json', '@' + str(path), '--output', 'json'], capture_output=True, text=True)
        if result.returncode:
            raise SystemExit(result.stderr or result.stdout)
    metadata = json.loads(result.stdout)
    (ROOT / 'deployment.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(result.stdout)

if __name__ == '__main__':
    main()
