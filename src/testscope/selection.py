"""Map a change (current tree vs the recorded baseline) to the tests it can affect."""
from __future__ import annotations

import fnmatch
import json
import os
from collections import defaultdict

from .fingerprint import import_graph, snapshot

# A change to any of these can change every test's behavior: run everything.
FULL_RUN = ["pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini", "noxfile.py",
            "requirements*.txt", "*.lock", "Pipfile", ".python-version", "constraints*.txt"]
# Changes here cannot affect a test run.
IGNORE = ["*.md", "*.rst", "docs/*", "LICENSE*", "CHANGELOG*", ".github/*", "*.png", "*.jpg", "*.svg", "*.gif"]

MAP_DIR = ".testscope"


def load_map(root: str) -> dict | None:
    try:
        with open(os.path.join(root, MAP_DIR, "map.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _match(rel: str, pats) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(os.path.basename(rel), p) for p in pats)


class Selection:
    def __init__(self):
        self.full: str | None = None            # reason to run everything
        self.tests: dict[str, str] = {}         # nodeid -> reason
        self.paths: dict[str, str] = {}         # path prefix -> reason (whole file / dir)
        self.changed: list[str] = []
        self.unobserved: list[str] = []         # changed, but no recorded test touched it

    def wants(self, nodeid: str, known: bool) -> str | None:
        if self.full:
            return self.full
        if not known:
            return "new test (not in map)"
        if nodeid in self.tests:
            return self.tests[nodeid]
        path = nodeid.split("::", 1)[0]
        for pre, why in self.paths.items():
            if path == pre or path.startswith(pre.rstrip("/") + "/"):
                return why
        return None


def compute(root: str, m: dict, snap: dict | None = None) -> Selection:
    sel = Selection()
    base = m["snap"]
    snap = snapshot(root) if snap is None else snap
    changed = sorted(f for f in set(base) | set(snap)
                     if base.get(f, {}).get("sha") != snap.get(f, {}).get("sha"))
    sel.changed = changed
    if not sel.changed:
        return sel
    for f in sel.changed:
        if _match(f, FULL_RUN):
            sel.full = f"{f} changed (environment/config)"
            return sel

    deps, tests = m["deps"], m["tests"]
    # dep index -> tests that hold it
    users: dict[int, list[str]] = defaultdict(list)
    for nid, ids in tests.items():
        for i in ids:
            users[i].append(nid)
    by_file: dict[str, list[tuple[str, int]]] = defaultdict(list)  # py file -> [(qualname, dep id)]
    reads: dict[str, int] = {}
    lists: dict[str, int] = {}
    for i, d in enumerate(deps):
        kind, _, rest = d.partition(":")
        if kind == "f":
            f, _, q = rest.partition("::")
            by_file[f].append((q, i))
        elif kind == "r":
            reads[rest] = i
        elif kind == "d":
            lists[rest] = i
    test_files = {nid.split("::", 1)[0] for nid in tests}
    graph = None

    def add(nids, why):
        for n in nids:
            sel.tests.setdefault(n, why)

    for f in sel.changed:
        old, new = base.get(f), snap.get(f)
        hit_before = len(sel.tests) + len(sel.paths)
        if f.endswith(".py"):
            ofn, nfn = (old or {}).get("funcs", {}), (new or {}).get("funcs", {})
            changed_q = {q for q in set(ofn) | set(nfn) if ofn.get(q) != nfn.get(q)}
            module = old is None or new is None or old.get("module") != new.get("module")
            for q, i in by_file.get(f, ()):
                if module or any(q == c or q.startswith(c + ".") for c in changed_q):
                    add(users[i], f"{f}::{q} changed" if not module else f"{f} module-level change")
            if module:
                if os.path.basename(f) == "conftest.py":
                    sel.paths[os.path.dirname(f) or "."] = f"{f} changed"
                if f in test_files or (new is None and old is not None):
                    sel.paths[f] = f"{f} changed"
                if graph is None:
                    graph = _reverse(import_graph(root, {**base, **snap}))
                for imp in _importers(graph, f):
                    if imp in test_files:
                        sel.paths.setdefault(imp, f"imports {f} (module-level change)")
        if f in reads:
            add(users[reads[f]], f"{f} read by test")
        if (old is None or new is None) and os.path.dirname(f) in lists:
            add(users[lists[os.path.dirname(f)]], f"{f} added/removed in a listed dir")
        if len(sel.tests) + len(sel.paths) == hit_before and not f.endswith(".py") and not _match(f, IGNORE):
            sel.unobserved.append(f)
    return sel


def _reverse(graph):
    rev: dict[str, set[str]] = defaultdict(set)
    for f, ds in graph.items():
        for d in ds:
            rev[d].add(f)
    return rev


def _importers(rev, f):
    seen, todo = set(), [f]
    while todo:
        for g in rev.get(todo.pop(), ()):
            if g not in seen:
                seen.add(g)
                todo.append(g)
    return seen
