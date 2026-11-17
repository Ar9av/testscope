"""Module-level mutants: right after `NAME = ...` (top level or class body) insert `NAME = None`.
Only names read somewhere else in the package are used, so most mutants break something.
Usage: python module_mutants.py <repo> <n> <seed>  -> JSON list on stdout"""
import ast, json, random, sys
from pathlib import Path

repo, n, seed = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
files = sorted((repo / "prismor").rglob("*.py"))
src = {f: f.read_text() for f in files}
reads = {}
for f, s in src.items():
    try:
        for node in ast.walk(ast.parse(s)):
            name = node.id if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) else \
                node.attr if isinstance(node, ast.Attribute) else None
            if name:
                reads[name] = reads.get(name, 0) + 1
    except SyntaxError:
        pass

cands = []
for f, s in src.items():
    try:
        tree = ast.parse(s)
    except SyntaxError:
        continue
    scopes = [(tree.body, 0)] + [(c.body, c.col_offset + 4) for c in tree.body if isinstance(c, ast.ClassDef)]
    for body, col in scopes:
        for node in body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name.startswith("__") or reads.get(name, 0) < 2:
                    continue
                cands.append({"kind": "py", "file": str(f.relative_to(repo)), "func": f"<module>:{name}",
                              "line": node.end_lineno + 1, "col": col, "text": f"{name} = None"})
print(json.dumps(random.Random(seed).sample(cands, min(n, len(cands))), indent=1))
