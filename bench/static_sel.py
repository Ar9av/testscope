"""Baselines: 'imports' = test files that transitively import a changed .py file
(pytest-impacted style); 'names' = tests/test_<module>*.py naming heuristic.
Usage: python static_sel.py <imports|names>   (cwd = repo; changed files from git diff)"""
import ast, os, subprocess, sys
from collections import defaultdict

from testscope.fingerprint import file_imports, module_index, repo_files

mode = sys.argv[1]
changed = subprocess.run(["git", "diff", "--name-only"], capture_output=True, text=True).stdout.split()
tests = sorted(f for f in os.listdir("tests") if f.startswith("test_") and f.endswith(".py"))
out = set()
if mode == "names":
    for c in changed:
        stem = os.path.basename(c).rsplit(".", 1)[0].lstrip("_")
        out |= {f"tests/{t}" for t in tests if stem and stem in t}
else:
    files = [f for f in repo_files(".") if f.endswith(".py")]
    mods = module_index(dict.fromkeys(files))
    rev = defaultdict(set)
    for g in files:
        try:
            tree = ast.parse(open(g, encoding="utf-8", errors="replace").read())
        except SyntaxError:
            continue
        for f in file_imports(g, tree, mods):
            rev[f].add(g)
    seen, todo = set(), list(changed)
    while todo:
        for g in rev.get(todo.pop(), ()):
            if g not in seen:
                seen.add(g)
                todo.append(g)
    out = {f for f in seen if f.startswith("tests/test_")}
print("\n".join(sorted(out)))
