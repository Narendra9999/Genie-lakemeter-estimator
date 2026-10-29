"""Load and embed the ordered Python sources used by the DCP UC function."""
import hashlib
import json
import os
import re
import sys
import textwrap
import types

_NAME = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


def load_bundle(manifest_path, root):
    manifest_path = os.path.abspath(manifest_path)
    root = os.path.abspath(root)
    with open(manifest_path, "rb") as f:
        raw = f.read()
    manifest = json.loads(raw.decode("utf-8"))
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not modules:
        raise ValueError("bundle manifest must contain a non-empty modules list")

    loaded = []
    names = set()
    for entry in modules:
        name, relpath = entry.get("name"), entry.get("path")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"invalid flat module name: {name!r}")
        if name in names:
            raise ValueError(f"duplicate module name: {name}")
        names.add(name)
        if not isinstance(relpath, str) or not relpath:
            raise ValueError(f"{name}: a relative source path is required")
        path = os.path.abspath(os.path.join(root, relpath))
        if os.path.commonpath((root, path)) != root:
            raise ValueError(f"{name}: source path must stay inside the repository")
        if not os.path.isfile(path):
            raise ValueError(f"{name}: source file does not exist: {relpath}")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        if "'''" in source or "$$" in source:
            raise ValueError(f"{name}: source cannot contain ''' or $$ in the UC function body")
        if source.endswith("\\"):
            raise ValueError(f"{name}: source cannot end with a backslash")
        compile(source, relpath, "exec")
        loaded.append({"name": name, "path": relpath, "source": source})

    ordered_names = [x["name"] for x in loaded]
    if "dcp_estimator" not in ordered_names or ordered_names[-1] != "dcp_adapter":
        raise ValueError("module order must include dcp_estimator and end with dcp_adapter")
    return {"manifest_bytes": raw, "manifest": manifest, "modules": loaded}


def cache_key(bundle):
    digest = hashlib.sha256()
    digest.update(bundle["manifest_bytes"])
    digest.update(b"\0")
    for module in bundle["modules"]:
        digest.update(module["name"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(module["source"].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:10]


def load_local_modules(bundle, key):
    loaded = {}
    for entry in bundle["modules"]:
        name, source = entry["name"], entry["source"]
        module = types.ModuleType(name)
        module.__file__ = entry["path"]
        sys.modules[name] = module
        exec(compile(source, entry["path"], "exec"), module.__dict__)
        sys.modules[name + "_" + key] = module
        loaded[name] = module
    return loaded


def build_loader(bundle, key):
    lines = ["import sys, types, textwrap", f'KEY = "{key}"']
    source_names = []
    for index, module in enumerate(bundle["modules"]):
        variable = f"DCP_SOURCE_{index}"
        lines.append(f"{variable} = r'''\n{module['source']}\n'''")
        source_names.append(f"({module['name']!r}, {variable})")
    lines.append("if \"dcp_adapter_\" + KEY not in sys.modules:")
    lines.append("    for name, code in (" + ", ".join(source_names) + (",)" if len(source_names) == 1 else ")") + ":")
    lines.append("        mod = types.ModuleType(name)")
    lines.append("        sys.modules[name] = mod")
    lines.append("        exec(compile(textwrap.dedent(code), name + '.py', 'exec'), mod.__dict__)")
    lines.append("        sys.modules[name + '_' + KEY] = mod")
    return "\n".join(lines)
