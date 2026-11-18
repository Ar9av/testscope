"""Measure the AI step on the same faults and commits as the other selectors.
For each fault: apply it, compute the testscope selection from the map, then ask the model
twice: 'union' (gets the testscope selection, may only add) and 'solo' (no map, picks alone).
For each commit: reverse-apply it and ask in 'union' mode only (no ground truth there).
Usage: python ai_bench.py <repo> <out.jsonl> <backend> <model or -> <effort> <mutants.jsonl ...> [--commits <commits.jsonl>]
Ground truth and verdicts come from the mutant rows (see run.py, verify.py)."""
import json, subprocess, sys
from pathlib import Path

from testscope import ai, selection

args = sys.argv[1:]
commits = Path(args.pop(args.index("--commits") + 1)) if "--commits" in args else None
args = [a for a in args if a != "--commits"]
repo, out, backend, model, effort, muts = Path(args[0]), Path(args[1]), args[2], args[3], args[4], args[5:]
model = None if model == "-" else model  # "-": the CLI's own default model
MODES = ("solo",)  # union rows in data/ai/ come from an older testscope that had that mode
MUTANT = 'raise RuntimeError("TIA_MUTANT")'
m = selection.load_map(str(repo))
index = ai.test_index(str(repo))
failed = set(json.loads((repo / ".testscope/lastfailed.json").read_text()))
by_file = {}
for nid in m["tests"]:
    by_file[nid.split("::")[0]] = by_file.get(nid.split("::")[0], 0) + 1


def apply(r):
    p = repo / r["file"]
    if r["kind"] == "py":
        lines = p.read_text().splitlines(keepends=True)
        lines.insert(r["line"] - 1, " " * r["col"] + r.get("text", MUTANT) + "\n")
        p.write_text("".join(lines))
    else:
        p.write_text("%%TIA_MUTANT%%{[\n")


def ts_files():
    s = selection.compute(str(repo), m)
    if s.full:
        return None, s
    return {n.split("::")[0] for n in list(s.tests) + list(failed)} | set(s.paths), s


def ask(sel, s, modes):
    res = {}
    for mode in modes:
        try:
            if mode == "union":
                raise RuntimeError("union mode was removed from testscope")
            add, info = ai.suggest(str(repo), s.changed, backend=backend, model=model, effort=effort, index=index)
            res[mode] = {"add": sorted(add), "info": info, "tests": sum(by_file.get(f, 0) for f in add)}
        except Exception as e:
            res[mode] = {"error": f"{type(e).__name__}: {e}"[:300]}
        print(mode, {k: v for k, v in res[mode].items() if k != "add"}, flush=True)
    return res


with out.open("w") as fh:
    for f in muts:
        for line in Path(f).read_text().splitlines():
            r = json.loads(line)
            if "sel" not in r:
                continue
            apply(r)
            try:
                sel, s = ts_files()
                row = {"src": "mutant", "file": r["file"], "func": r.get("func", ""), "kind": r["kind"],
                       "truth": r["truth"], "verdict": r.get("verdict", {}), "ts": sorted(sel or [])}
                row.update(ask(sel or set(), s, MODES))
            finally:
                subprocess.run(["git", "checkout", "--", r["file"]], cwd=repo)
            fh.write(json.dumps(row) + "\n")
            fh.flush()
    if commits:
        for line in commits.read_text().splitlines()[1:]:
            c = json.loads(line)["commit"]
            patch = subprocess.run(f"git show --format= {c} -- prismor", shell=True, cwd=repo,
                                   capture_output=True, text=True).stdout
            subprocess.run(["git", "apply", "-R"], input=patch, cwd=repo, text=True)
            try:
                sel, s = ts_files()
                row = {"src": "commit", "commit": c, "ts": sorted(sel or [])}
                row.update(ask(sel or set(), s, ("union",)))
            finally:
                subprocess.run("git checkout -- .", shell=True, cwd=repo)
            fh.write(json.dumps(row) + "\n")
            fh.flush()
