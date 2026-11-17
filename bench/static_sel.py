"""Baselines: 'imports' = test files that transitively import a changed .py file
(pytest-impacted style); 'names' = tests/test_<module>*.py naming heuristic.
Usage: python static_sel.py <imports|names>   (cwd = repo; changed files from git diff)"""
import os, subprocess, sys
from testscope.fingerprint import import_graph, snapshot
from testscope.selection import _importers, _reverse

mode = sys.argv[1]
changed = subprocess.run(["git", "diff", "--name-only"], capture_output=True, text=True).stdout.split()
tests = sorted(f for f in os.listdir("tests") if f.startswith("test_") and f.endswith(".py"))
out = set()
if mode == "names":
    for c in changed:
        stem = os.path.basename(c).rsplit(".", 1)[0].lstrip("_")
        out |= {f"tests/{t}" for t in tests if stem and stem in t}
else:
    rev = _reverse(import_graph(".", snapshot(".")))
    for c in changed:
        out |= {f for f in _importers(rev, c) if f.startswith("tests/test_")}
print("\n".join(sorted(out)))
