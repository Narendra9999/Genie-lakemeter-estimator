"""Regenerate every derived SQL/notebook artifact in dependency order."""
import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SCRIPTS = (
    "build_sql.py",
    "build_catalog.py",
    "build_notebook.py",
    "build_setup.py",
)

for script in SCRIPTS:
    print(f"==> calculator/{script}", flush=True)
    subprocess.run([sys.executable, os.path.join(ROOT, "calculator", script)], cwd=ROOT, check=True)
