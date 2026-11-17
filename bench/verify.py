"""Re-check every (mutant, missed file): real only if it fails 2/2 with the mutant and passes 2/2 without.
Usage: python verify.py <repo> <res.jsonl> <out.jsonl>"""
import json, os, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
repo, res, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
MUTANT = 'raise RuntimeError("TIA_MUTANT")'
env = {**os.environ, "TESTSCOPE": "off"}


def run(f):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-p", "no:testmon", f],
                          cwd=repo, capture_output=True, env=env, timeout=900).returncode


def apply(m):
    p = repo / m["file"]
    if m["kind"] == "py":
        lines = p.read_text().splitlines(keepends=True)
        lines.insert(m["line"] - 1, " " * m["col"] + m.get("text", MUTANT) + "\n")
        p.write_text("".join(lines))
    else:
        p.write_text("%%TIA_MUTANT%%{[\n")


cache = {}
with out.open("w") as fh:
    for line in res.read_text().splitlines():
        m = json.loads(line)
        files = sorted({f for s in m["sel"].values() for f in s["missed"]})
        verdict = {}
        for f in files:
            if f not in cache:
                cache[f] = all(run(f) == 0 for _ in range(2))  # clean passes?
            if not cache[f]:
                verdict[f] = "flaky"
                continue
            apply(m)
            try:
                verdict[f] = "real" if all(run(f) != 0 for _ in range(2)) else "flaky"
            finally:
                subprocess.run(["git", "checkout", "--", m["file"]], cwd=repo)
        m["verdict"] = verdict
        fh.write(json.dumps(m) + "\n")
        print(m["file"], m.get("func", ""), verdict, flush=True)
