"""Content fingerprints and the static import graph.

A .py file is split into functions (by qualname, hashed by token stream, so moving
code or editing comments does not count as a change) and module/class-level statements
keyed by the names they bind, so a changed constant maps to the code that reads it.
Other files get a sha1.
"""
from __future__ import annotations

import ast
import hashlib
import io
import os
import subprocess
import textwrap
import tokenize

def _h(s: str | bytes) -> str:
    return hashlib.sha1(s if isinstance(s, bytes) else s.encode()).hexdigest()[:16]


_SKIP_TOK = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
             tokenize.ENCODING, tokenize.ENDMARKER}


def _tok_hash(text: str) -> str:
    """Hash of the token stream: ignores comments, blank lines and indentation, and
    unlike ast.dump() it is stable across Python versions (map from CI 3.12, run on 3.14)."""
    try:
        toks = tokenize.generate_tokens(io.StringIO(textwrap.dedent(text)).readline)
        return _h(" ".join(t.string for t in toks if t.type not in _SKIP_TOK))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return _h(text)


def bound_names(node) -> list[str]:
    """Names a module- or class-level statement binds."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Import):
        return [a.asname or a.name.split(".")[0] for a in node.names]
    if isinstance(node, ast.ImportFrom):
        return [a.asname or a.name for a in node.names]
    if isinstance(node, ast.Expr):
        return []  # bare call / docstring: binds nothing
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.append(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.append(n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out += bound_names(n)
    return out


def referenced_names(node) -> tuple[set[str], set[str]]:
    """(bare names, attribute names) a node reads. Function parameters count as bare
    names so a pytest fixture maps to the tests that request it."""
    bare, attr = set(), set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            bare.add(n.id)
        elif isinstance(n, ast.Attribute):
            attr.add(n.attr)
        elif isinstance(n, ast.arg):
            bare.add(n.arg)
        elif isinstance(n, ast.alias):
            bare.add(n.name.split(".")[-1])
    return bare, attr


def module_statements(tree, lines):
    """Yield (key, node, text) for each module- and class-level statement that is not a def.

    key is "n:<scope><names>" for statements that bind names, "x:<scope>#<i>" otherwise.
    A def contributes "n:<scope><name>" with no text, so adding or removing one is a change."""
    def seg(a, b):
        return "".join(lines[a - 1:b])

    def walk(body, scope):
        anon = 0
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield f"n:{scope}{node.name}", node, "def"
            elif isinstance(node, ast.ClassDef):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                yield f"n:{scope}{node.name}", node, seg(first, node.body[0].lineno - 1) or node.name
                yield from walk(node.body, f"{scope}{node.name}.")
            else:
                names = bound_names(node)
                if names:
                    key = f"n:{scope}" + ",".join(sorted(set(names)))
                else:
                    anon += 1
                    key = f"x:{scope}#{anon}"
                yield key, node, seg(node.lineno, node.end_lineno)

    yield from walk(tree.body, "")


def py_fingerprint(src: str) -> dict:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {"funcs": {}, "stmts": {"x:#syntax": _h(src)}}
    lines = src.splitlines(keepends=True)
    funcs: dict[str, str] = {}
    stmts: dict[str, str] = {}

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                funcs[prefix + node.name] = _tok_hash("".join(lines[first - 1:node.end_lineno]))
            elif isinstance(node, ast.ClassDef):
                walk(node.body, prefix + node.name + ".")

    walk(tree.body, "")
    for key, _node, text in module_statements(tree, lines):
        h = _tok_hash(text)
        # Same names bound twice (try/except import, redefinition): fold into one entry.
        stmts[key] = _h(stmts[key] + h) if key in stmts else h
    return {"funcs": funcs, "stmts": stmts}


def repo_files(root: str) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root,
                             capture_output=True, check=True).stdout
        return sorted(p for p in os.fsdecode(out).split("\0")
                      if p and not p.startswith(".testscope/") and os.path.isfile(os.path.join(root, p)))
    except (OSError, subprocess.CalledProcessError):
        files = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("venv", "node_modules", "__pycache__")]
            files += [os.path.relpath(os.path.join(d, n), root) for n in names]
        return sorted(files)


def snapshot(root: str, base: dict | None = None) -> dict:
    """Fingerprint every repo file. Files whose bytes match `base` reuse its entry, so
    only changed files are parsed (selection on a big repo stays well under a second)."""
    snap = {}
    base = base or {}
    for rel in repo_files(root):
        try:
            with open(os.path.join(root, rel), "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        sha = _h(data)
        if base.get(rel, {}).get("sha") == sha:
            snap[rel] = base[rel]
        elif rel.endswith(".py"):
            fp = py_fingerprint(data.decode("utf-8", "replace"))
            fp["sha"] = sha
            snap[rel] = fp
        else:
            snap[rel] = {"sha": sha}
    return snap


def module_index(snap: dict) -> dict[str, str]:
    """dotted module name -> repo file. Every dotted suffix is a name ("src/pkg/mod.py" ->
    src.pkg.mod, pkg.mod, mod), longest first, so a full path beats a shorter suffix."""
    mods: dict[str, str] = {}
    paths = []
    for rel in snap:
        if rel.endswith(".py"):
            parts = rel[:-3].split("/")
            paths.append((parts[:-1] if parts[-1] == "__init__" else parts, rel))
    for depth in range(max((len(p) for p, _ in paths), default=0), 0, -1):
        for parts, rel in paths:
            if len(parts) >= depth:
                mods.setdefault(".".join(parts[len(parts) - depth:]), rel)
    return mods


def file_imports(rel: str, tree, mods: dict[str, str]) -> dict[str, set[str]]:
    """repo files this file imports -> names taken with `from x import ...` ("*" for a
    star import, empty for `import x` where only attribute access can reach names)."""
    pkg = rel.split("/")[:-1]
    out: dict[str, set[str]] = {}

    def hit(mod, names=()):
        bits = mod.split(".")
        for i in range(1, len(bits) + 1):  # importing a.b.c also runs a/__init__, a/b/__init__
            f = mods.get(".".join(bits[:i]))
            if f and f != rel:
                out.setdefault(f, set())
        f = mods.get(mod)
        if f and f != rel:
            out.setdefault(f, set()).update(names)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                hit(a.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                up = pkg[: len(pkg) - node.level + 1] if node.level > 1 else pkg
                base = ".".join(up + ([base] if base else []))
            hit(base, [a.name for a in node.names])
            for a in node.names:  # `from pkg import mod` imports a module
                if base and mods.get(f"{base}.{a.name}"):
                    hit(f"{base}.{a.name}")
    return out
