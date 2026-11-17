"""Selection size on real changes: for each recent commit touching source .py files,
reverse-apply it onto HEAD (same functions as the real change) and count what each
selector picks. Needs .testscope/map.json and .testmondata recorded at HEAD.
Usage: python commits.py <repo> <n> <out.jsonl>"""
import json, os, subprocess, sys, time
from pathlib import Path

repo, n, out = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
here = Path(__file__).parent
SEL = {
    "testscope": "TESTSCOPE=on python -m pytest -p no:testmon -p no:cacheprovider --collect-only -q tests",
    "testmon": "TESTSCOPE=off python -m pytest -p no:testscope --testmon-nocollect -p no:cacheprovider --collect-only -q tests",
}


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True, **kw)


def count(cmd):
    t = time.time()
    lines = [l for l in sh(cmd + " 2>/dev/null").stdout.splitlines() if "::" in l]
    return len(lines), len({l.split("::")[0] for l in lines}), round(time.time() - t, 1)


total = count("TESTSCOPE=off python -m pytest -p no:testmon -p no:testscope -p no:cacheprovider --collect-only -q tests")
shas = sh(f"git log --format=%h -n {n * 3} HEAD -- 'prismor/*.py' 'prismor/**/*.py'").stdout.split()[1:]
done = 0
with out.open("w") as fh:
    fh.write(json.dumps({"total_tests": total[0], "total_files": total[1]}) + "\n")
    for c in shas:
        if done >= n:
            break
        if sh(f"git show --format= {c} -- prismor | git apply -R --check").returncode:
            continue
        sh(f"git show --format= {c} -- prismor | git apply -R")
        try:
            files = sh("git diff --name-only").stdout.split()
            row = {"commit": c, "files": len(files)}
            for k, cmd in SEL.items():
                row[k] = count(cmd)
            imp = sh(f"python {here}/static_sel.py imports").stdout.split()
            row["imports"] = (None, len(imp), None)
        finally:
            sh("git checkout -- .")
        fh.write(json.dumps(row) + "\n")
        fh.flush()
        print(row, flush=True)
        done += 1
