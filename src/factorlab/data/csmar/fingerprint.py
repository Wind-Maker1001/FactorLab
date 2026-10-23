"""Versioned, path-bound implementation closure, computed from actual bytes.

Follow repository imports, including function-local imports and executable
package initializers. Include every CSMAR entry point. Third party/stdlib code
is version-bound by the runtime contract, not represented as repository code.
This is version identity, not authorization to modify a trusted interpreter.
"""

import ast
from functools import lru_cache
import importlib.metadata
from pathlib import Path
import platform

import factorlab
from factorlab.data.evidence import json_bytes, sha256

CONTRACT = "factorlab-csmar-implementation-path-closure-v1"
CONFIGS = ("src/factorlab/resources/csi500_5y.yaml",)


def implementation_sha256(kind):
    return sha256(json_bytes(implementation_manifest(kind)))


@lru_cache(maxsize=128)
def _syntax(body, path):
    # Cache parsing only. Every identity call still rereads and hashes all
    # actual bytes; a change cannot reuse a cached identity or dependency list.
    return ast.parse(body, filename=path)


def implementation_manifest(kind):
    if kind not in {"parser", "qualification"}:
        raise ValueError("unknown CSMAR implementation role")
    root = Path(factorlab.__file__).resolve().parent
    package = Path(__file__).resolve().parent

    def physical(path):
        prefix = "src/factorlab/"
        if not path.startswith(prefix):
            raise ValueError("implementation source outside factorlab")
        return root / path[len(prefix):]

    files = {}
    pending = {"src/factorlab/data/csmar/" + p.name for p in package.glob("*.py")}
    if kind == "qualification":
        pending.add("src/factorlab/data/market_qualification.py")

    def add_module(module):
        parts = module.split(".")
        if parts[:1] != ["factorlab"]:
            return
        for count in range(1, len(parts) + 1):
            name = "src/" + "/".join(parts[:count])
            for path in (name + ".py", name + "/__init__.py"):
                if path == "src/factorlab.py":
                    continue
                if physical(path).is_file() and path not in files:
                    pending.add(path)

    while pending:
        path = min(pending)
        pending.remove(path)
        if path in files:
            continue
        body = physical(path).read_bytes()
        files[path] = sha256(body)
        parts = path[len("src/"):-3].split("/")
        package_parts = parts[:-1]
        # Include the initializers executed before this module.
        add_module(".".join(package_parts))
        for node in ast.walk(_syntax(body, path)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add_module(alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = package_parts[:len(package_parts) - node.level + 1] if node.level else []
                module = ".".join(base + ([node.module] if node.module else []))
                add_module(module)
                for alias in node.names:
                    add_module(module + "." + alias.name)
    return {"contract": CONTRACT, "role": kind,
            "files": [{"path": p, "sha256": files[p]} for p in sorted(files)],
            "configs": [{"path": p, "sha256": sha256(physical(p).read_bytes())} for p in CONFIGS],
            "runtime": {"python": platform.python_version(),
                        **{p: importlib.metadata.version(p) for p in ("numpy", "pandas", "pyarrow")}}}
