"""Content fingerprints and the static import graph.

A .py file is split into functions (by qualname, hashed by token stream, so moving
code or editing comments does not count as a change) and one "<module>" bucket holding every
other top-level or class-level statement. Other files get a sha1.
"""
from __future__ import annotations

import ast
import hashlib
import io
import os
import subprocess
import textwrap
import tokenize

MODULE = "<module>"


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


def py_fingerprint(src: str) -> dict:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {"funcs": {}, "module": _h(src)}
    lines = src.splitlines(keepends=True)

    def seg(first, last):  # 1-based inclusive line range
        return "".join(lines[first - 1:last])

    def start(node):
        return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])

    funcs: dict[str, str] = {}
    rest: list[str] = []

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs[prefix + node.name] = _tok_hash(seg(start(node), node.end_lineno))
                rest.append("def " + prefix + node.name)  # adding/removing a def is a module change
            elif isinstance(node, ast.ClassDef):
                rest.append(_tok_hash(seg(start(node), node.body[0].lineno - 1) or node.name))
                walk(node.body, prefix + node.name + ".")
            else:
                rest.append(_tok_hash(seg(node.lineno, node.end_lineno)))

    walk(tree.body, "")
    return {"funcs": funcs, "module": _h("\n".join(rest))}


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


def snapshot(root: str) -> dict:
    snap = {}
    for rel in repo_files(root):
        try:
            data = open(os.path.join(root, rel), "rb").read()
        except OSError:
            continue
        if rel.endswith(".py"):
            fp = py_fingerprint(data.decode("utf-8", "replace"))
            fp["sha"] = _h(data)
            snap[rel] = fp
        else:
            snap[rel] = {"sha": _h(data)}
    return snap


def import_graph(root: str, snap: dict) -> dict[str, set[str]]:
    """file -> repo .py files it imports (resolved by module name, best effort)."""
    mods: dict[str, str] = {}
    paths = []
    for rel in snap:
        if rel.endswith(".py"):
            parts = rel[:-3].split("/")
            paths.append((parts[:-1] if parts[-1] == "__init__" else parts, rel))
    # Every dotted suffix is a name ("src/pkg/mod.py" -> src.pkg.mod, pkg.mod, mod),
    # longest first, so a full path always beats another file's shorter suffix.
    for depth in range(max((len(p) for p, _ in paths), default=0), 0, -1):
        for parts, rel in paths:
            if len(parts) >= depth:
                mods.setdefault(".".join(parts[len(parts) - depth:]), rel)
    graph: dict[str, set[str]] = {}
    for rel in snap:
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8", errors="replace").read())
        except (SyntaxError, OSError):
            continue
        pkg = rel[:-3].split("/")[:-1] if not rel.endswith("__init__.py") else rel.split("/")[:-1]
        deps = set()
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    up = pkg[: len(pkg) - node.level + 1] if node.level > 1 else pkg
                    base = ".".join(up + ([base] if base else []))
                names = [base] + [f"{base}.{a.name}" if base else a.name for a in node.names]
            for n in names:
                # importing a.b.c also runs a/__init__ and a/b/__init__
                bits = n.split(".")
                for i in range(1, len(bits) + 1):
                    hit = mods.get(".".join(bits[:i]))
                    if hit and hit != rel:
                        deps.add(hit)
        graph[rel] = deps
    return graph
