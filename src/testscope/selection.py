"""Map a change (current tree vs the recorded baseline) to the tests it can affect."""
from __future__ import annotations

import ast
import fnmatch
import json
import os
from collections import defaultdict

from .fingerprint import file_imports, module_index, module_statements, referenced_names, snapshot

# A change to any of these can change every test's behavior: run everything.
FULL_RUN = ["pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini", "noxfile.py",
            "requirements*.txt", "*.lock", "Pipfile", ".python-version", "constraints*.txt"]
# Changes here cannot affect a test run.
IGNORE = ["*.md", "*.rst", "docs/*", "LICENSE*", "CHANGELOG*", ".github/*", "*.png", "*.jpg", "*.svg", "*.gif"]

MAP_DIR = ".testscope"
MAP_VERSION = 2
CANARIES = 5
SCRIPTS = (".sh", ".bash", ".zsh")


def load_map(root: str) -> dict | None:
    try:
        with open(os.path.join(root, MAP_DIR, "map.json")) as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return None
    return m if m.get("version") == MAP_VERSION else None


def installed(tops) -> dict[str, str]:
    """'dist==version' for each top-level import name: what an upgrade changes."""
    try:
        from importlib.metadata import packages_distributions, version
        pd = packages_distributions()
    except Exception:  # Python 3.9 has no packages_distributions
        return {}
    out = {}
    for t in tops:
        vs = []
        for d in sorted(set(pd.get(t, ()))):
            try:
                vs.append(f"{d}=={version(d)}")
            except Exception:
                pass
        out[t] = ",".join(vs)
    return out


def versions_now(was: dict[str, str]) -> dict[str, str]:
    """Same format as installed(), but only looks up the distributions recorded in the map
    (a direct lookup each; scanning every installed package takes most of a second)."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:
        return {}
    cache: dict[str, str] = {}
    out = {}
    for t, s in was.items():
        vs = []
        for d in filter(None, s.split(",")):
            name = d.split("==", 1)[0]
            if name not in cache:
                try:
                    cache[name] = version(name)
                except PackageNotFoundError:
                    cache[name] = ""
            if cache[name]:
                vs.append(f"{name}=={cache[name]}")
        out[t] = ",".join(vs)
    return out


RANK = (("failed last run", 0), ("new test", 1), (" changed", 1), (" read by test", 1),
        ("import canary", 3))


def order_files(why: dict[str, str], dur: dict[str, float]) -> list[str]:
    """Test files in fail-first order: last failures, then tests that ran the changed code,
    then indirect reasons, then import canaries; shorter files first within a rank. Whole
    files move, so module- and class-scoped fixtures still run once per file."""
    best: dict[str, tuple[int, float]] = {}
    for nid, w in why.items():
        f = nid.split("::", 1)[0]
        r = next((v for k, v in RANK if k in w), 2)
        best[f] = (min(best.get(f, (9, 0))[0], r), best.get(f, (9, 0))[1] + dur.get(nid, 0))
    return sorted(best, key=lambda f: best[f])


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


class _Src:
    """Lazily parsed current sources, plus who imports whom."""

    def __init__(self, root, snap):
        self.root, self.snap = root, snap
        self.mods = module_index(snap)
        self._trees: dict[str, tuple] = {}
        self._importers: dict[str, dict[str, set[str]]] | None = None

    def tree(self, rel):
        if rel not in self._trees:
            try:
                src = open(os.path.join(self.root, rel), encoding="utf-8", errors="replace").read()
                self._trees[rel] = (ast.parse(src), src.splitlines(keepends=True))
            except (OSError, SyntaxError, ValueError):
                self._trees[rel] = (None, [])
        return self._trees[rel]

    def importers(self, rel) -> dict[str, set[str]]:
        """files that import rel directly -> names they take from it."""
        if self._importers is None:
            self._importers = defaultdict(dict)
            for g in self.snap:
                if g.endswith(".py"):
                    t, _ = self.tree(g)
                    if t is not None:
                        for f, names in file_imports(g, t, self.mods).items():
                            self._importers[f][g] = names
        return self._importers.get(rel, {})


def _split_key(key):
    """'n:C.a,b' -> ('C.', {'a','b'})"""
    rest = key[2:]
    scope, _, names = rest.rpartition(".")
    return (scope + "." if scope else ""), set(names.split(","))


def compute(root: str, m: dict, snap: dict | None = None) -> Selection:
    sel = Selection()
    base = m["snap"]
    snap = snapshot(root, base) if snap is None else snap
    changed = sorted(f for f in set(base) | set(snap)
                     if base.get(f, {}).get("sha") != snap.get(f, {}).get("sha"))
    sel.changed = changed
    deps = m["deps"]
    pk = {d[2:]: i for i, d in enumerate(deps) if d.startswith("p:")}
    was = m.get("pkgs") or {}
    now = versions_now(was) if pk and was else {}
    upgraded = [t for t in sorted(pk) if now and now.get(t) != was.get(t)]
    if not sel.changed and not upgraded:
        return sel
    for f in sel.changed:
        if _match(f, FULL_RUN):
            sel.full = f"{f} changed (environment/config)"
            return sel

    tests = m["tests"]
    users: dict[int, list[str]] = defaultdict(list)  # dep id -> tests holding it
    for nid, ids in tests.items():
        for i in ids:
            users[i].append(nid)
    for t in upgraded:  # only tests whose code ran inside the package
        why = f"installed package {t} changed ({was.get(t) or 'none'} -> {now.get(t) or 'removed'})"
        for n in users[pk[t]]:
            sel.tests.setdefault(n, why)
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
    src = _Src(root, snap)

    def add(nids, why):
        for n in nids:
            sel.tests.setdefault(n, why)

    def code_users(f, pred, why):
        for q, i in by_file.get(f, ()):
            if pred(q):
                add(users[i], why)

    def func_changed(f, qual, why):  # recorded qualname q ran inside qual (nested, lambdas)
        code_users(f, lambda q: q == qual or q.startswith(qual + "."), why)

    names_todo: list[tuple[str, set[str], str]] = []  # (file, changed names, why)
    canaries: list[str] = []

    for f in sel.changed:
        old, new = base.get(f) or {}, snap.get(f) or {}
        hit_before = len(sel.tests) + len(sel.paths)
        if f.endswith(".py"):
            ofn, nfn = old.get("funcs", {}), new.get("funcs", {})
            for q in set(ofn) | set(nfn):
                if ofn.get(q) != nfn.get(q):
                    func_changed(f, q, f"{f}::{q} changed")
            ost, nst = old.get("stmts", {}), new.get("stmts", {})
            if ost != nst:
                # Any module-level edit can make the import itself raise, which breaks every
                # importer whether or not it reads the changed name. A few tests that ran this
                # module's import code surface that. ponytail: 5 canaries; raise if needed.
                canaries.append(f)
            for key in set(ost) | set(nst):
                if ost.get(key) == nst.get(key):
                    continue
                if key.startswith("x:"):  # binds nothing (bare call, docstring): anything in f
                    why = f"{f}: module-level code changed"
                    code_users(f, lambda q: True, why)
                    if f in test_files:
                        sel.paths[f] = why
                    if os.path.basename(f) == "conftest.py":
                        sel.paths[os.path.dirname(f) or "."] = why
                    continue
                scope, names = _split_key(key)
                why = f"{f}: {scope}{','.join(sorted(names))} changed"
                if scope:  # class attribute: tests that used the class
                    code_users(f, lambda q, c=scope: q.startswith(c), why)
                names_todo.append((f, names, why))
        if f in reads:
            add(users[reads[f]], f"{f} read by test")
        if (not old or not new) and os.path.dirname(f) in lists:
            add(users[lists[os.path.dirname(f)]], f"{f} added/removed in a listed dir")
        if len(sel.tests) + len(sel.paths) == hit_before and not f.endswith(".py") and not _match(f, IGNORE):
            sel.unobserved.append(f)

    # A test can depend on a package without running its code: a cache filled by an earlier
    # test, a value computed once. Repo functions that name the package count too.
    for t in upgraded:
        why = f"installed package {t} changed ({was.get(t) or 'none'} -> {now.get(t) or 'removed'})"
        for f, qual in _package_users(src, t):
            func_changed(f, qual, why)

    _propagate(names_todo, src, test_files, func_changed, code_users)
    _referenced_data(root, [f for f in sel.changed if not f.endswith(".py")], reads, users, add)
    for f in canaries:  # after real reasons, so those win in the report
        ran_import = sorted({n for q, i in by_file.get(f, ()) if q == "<module>" for n in users[i]})
        add(ran_import[:CANARIES], f"{f}: import canary (module-level change)")
    sel.changed += [f"{t} (installed package)" for t in upgraded]
    return sel


def _referenced_data(root, changed, reads, users, add):
    """Files no test opens directly (a script `source`d by another script, a config included
    by another config) are reached through a file tests did read that names them."""
    texts: dict[str, str] = {}

    def text(rel):
        if rel not in texts:
            try:
                with open(os.path.join(root, rel), "rb") as fh:
                    b = fh.read(1 << 20)
                texts[rel] = "" if b"\0" in b else b.decode("utf-8", "replace")
            except OSError:
                texts[rel] = ""
        return texts[rel]

    seen, todo = set(changed), list(changed)
    while todo:
        f = todo.pop()
        name = os.path.basename(f)
        for x, i in reads.items():
            if x in seen or _match(x, IGNORE) or name not in text(x):
                continue  # docs name files without loading them
            add(users[i], f"{x} references {f}")
            seen.add(x)
            if x.endswith(SCRIPTS):  # a script sourcing a script sourcing ...; data stops at one hop
                todo.append(x)


def _propagate(todo, src, test_files, func_changed, code_users):
    """A changed name selects code that reads it: in its own file, and in files that
    import it. Module-level statements that read it make their own names changed too."""
    seen: set[tuple[str, str]] = set()
    while todo:
        f, names, why = todo.pop()
        names = {n for n in names if (f, n) not in seen}
        if not names:
            continue
        seen |= {(f, n) for n in names}
        cands = {f: None, **src.importers(f)}  # None: same file, every reference counts
        if os.path.basename(f) == "conftest.py":  # fixtures reach tests by parameter name
            d = os.path.dirname(f)
            cands.update({t: None for t in test_files if not d or t.startswith(d + "/")})
        for g, taken in cands.items():
            tree, lines = src.tree(g)
            if tree is None:
                continue
            via_from = names if taken is None or "*" in taken else names & taken

            def reads(node):
                bare, attr = referenced_names(node)
                return (bare & via_from) | (attr & names)

            for qual, node in _functions(tree):
                if reads(node):
                    func_changed(g, qual, why)
            for key, node, _text in module_statements(tree, lines):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                target = ast.Module(body=node.bases + node.keywords + node.decorator_list, type_ignores=[]) \
                    if isinstance(node, ast.ClassDef) else node
                if not reads(target):
                    continue
                if isinstance(node, ast.ClassDef):  # base class / decorator changed
                    code_users(g, lambda q, c=node.name + ".": q.startswith(c), why)
                    continue
                if key.startswith("x:"):
                    code_users(g, lambda q: True, why)
                    continue
                todo.append((g, _split_key(key)[1], why))


def _package_users(src, top):
    """(file, function qualname) for repo functions whose code uses the package `top`."""
    out = []
    for f in src.snap:
        if not f.endswith(".py"):
            continue
        tree, lines = src.tree(f)
        if tree is None or top not in "".join(lines):
            continue
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.asname or a.name.split(".")[0] for a in n.names if a.name.split(".")[0] == top}
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and (n.module or "").split(".")[0] == top:
                names |= {a.asname or a.name for a in n.names}
        if not names:
            continue
        for qual, node in _functions(tree):
            if any(isinstance(x, ast.Name) and x.id in names for x in ast.walk(node)) or any(
                    isinstance(x, (ast.Import, ast.ImportFrom)) and top in ast.dump(x) for x in ast.walk(node)):
                out.append((f, qual))
    return out


def _functions(tree, prefix=""):
    for node in tree.body if isinstance(tree, ast.Module) else tree:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield prefix + node.name, node
        elif isinstance(node, ast.ClassDef):
            yield from _functions(node.body, prefix + node.name + ".")
