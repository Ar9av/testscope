"""Pick mutants: Python functions (insert a raise) and data files (corrupt them).

Usage: python mutants.py <repo> <n_py> <seed>  -> JSON list on stdout
"""
import ast, json, random, sys
from pathlib import Path

repo, n_py, seed = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
rng = random.Random(seed)
pkg = repo / "prismor"

funcs = []
for f in sorted(pkg.rglob("*.py")):
    try:
        tree = ast.parse(f.read_text())
    except Exception:
        continue
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(node.body) >= 2:
            body = node.body
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) and isinstance(first.value.value, str):
                first = body[1]
            funcs.append({"kind": "py", "file": str(f.relative_to(repo)), "func": node.name,
                          "line": first.lineno, "col": first.col_offset})

data = [str(f.relative_to(repo)) for f in sorted(pkg.rglob("*"))
        if f.is_file() and f.suffix in {".yaml", ".yml", ".json", ".toml", ".sh", ".js", ".jsonl"}]

out = rng.sample(funcs, min(n_py, len(funcs))) + [{"kind": "data", "file": d} for d in data]
json.dump(out, sys.stdout, indent=1)
