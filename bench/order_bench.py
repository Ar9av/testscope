"""Time to the first failure: default (alphabetical) file order vs testscope's fail-first
order, on the same faults. Sums recorded file durations before the first failing file,
one process (the -x case an agent uses). Usage: python order_bench.py <repo> <results.json> <mutants.jsonl ...>"""
import json, statistics as st, subprocess, sys
from pathlib import Path

from testscope import selection

repo, flaky = Path(sys.argv[1]), set(json.loads(Path(sys.argv[2]).read_text())["flaky"])
m = selection.load_map(str(repo))
m.pop("pkgs", None)  # code faults only; package changes are measured in pkg_bench.py
dur = m.get("dur", {})
# The checkout has 3 tests that fail for environment reasons; they would sit at the front of
# every fail-first order. Measure as after a clean previous run.
lastfailed = set()
fdur = {}
for nid, d in dur.items():
    fdur[nid.split("::")[0]] = fdur.get(nid.split("::")[0], 0) + d
MUTANT = 'raise RuntimeError("TIA_MUTANT")'
res = {"default": [], "fail_first": [], "files_default": [], "files_fail_first": []}
for f in sys.argv[3:]:
    for line in Path(f).read_text().splitlines():
        r = json.loads(line)
        if "sel" not in r:
            continue
        real = set(r["truth"]) - flaky - {k for k, v in r.get("verdict", {}).items() if v == "flaky"}
        if not real:
            continue
        p = repo / r["file"]
        old = p.read_text()
        if r["kind"] == "py":
            lines = old.splitlines(keepends=True)
            lines.insert(r["line"] - 1, " " * r["col"] + r.get("text", MUTANT) + "\n")
            p.write_text("".join(lines))
        else:
            p.write_text("%%TIA_MUTANT%%{[\n")
        try:
            s = selection.compute(str(repo), m)
        finally:
            p.write_text(old)
        why = {}
        for nid in m["tests"]:
            w = s.wants(nid, True) or ("failed last run" if nid in lastfailed else None)
            if w:
                why[nid] = w
        for name, order in (("default", sorted({n.split("::")[0] for n in why})),
                            ("fail_first", selection.order_files(why, dur))):
            t = k = 0
            for fl in order:
                if fl in real:
                    break
                t += fdur.get(fl, 0)
                k += 1
            else:
                t = k = None  # selection missed every failing file
            res[name].append(t)
            res["files_" + name].append(k)
        subprocess.run(["git", "checkout", "--", r["file"]], cwd=repo)
out = {}
for name in ("default", "fail_first"):
    v = [x for x in res[name] if x is not None]
    out[name] = {"faults": len(v), "median_s": round(st.median(v), 2), "mean_s": round(st.mean(v), 2),
                 "p90_s": round(sorted(v)[int(len(v) * 0.9)], 2),
                 "first_file_fails": sum(1 for x in res["files_" + name] if x == 0),
                 "median_files_before": st.median(x for x in res["files_" + name] if x is not None)}
print(json.dumps(out, indent=1))
