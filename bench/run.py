"""For each mutant: ground truth = tests that fail in a full run; compare selectors.

Usage: python run.py <repo> <mutants.json> <out.jsonl> [selector ...]
Selectors are shell commands run in <repo>; they print selected test files, one per line.
A selector is SAFE for a mutant when every failing test file is in its output.
"""
import json, os, subprocess, sys, time, xml.etree.ElementTree as ET
from pathlib import Path

repo, muts, out = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text()), Path(sys.argv[3])
selectors = dict(s.split("=", 1) for s in sys.argv[4:])
junit = Path(__file__).parent / "junit.xml"
MUTANT = 'raise RuntimeError("TIA_MUTANT")'
TIMEOUT = 900  # a normal full run is ~4 min; a mutant that hangs the suite is skipped
START = int(os.environ.get("START", "0"))


def failing_files():
    if junit.exists():
        junit.unlink()
    proc = subprocess.Popen([sys.executable, "-m", "pytest", "-q", "-n", "12", "-p", "no:cacheprovider",
                             "-p", "no:testmon", f"--junitxml={junit}", "tests"], cwd=repo,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                            env={**os.environ, "TESTSCOPE": "off"})
    try:
        proc.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 9)  # xdist workers too
        proc.wait()
        raise
    bad = set()
    for tc in ET.parse(junit).getroot().iter("testcase"):
        if tc.find("failure") is not None or tc.find("error") is not None:
            cls = tc.get("classname", "")  # tests.test_x.TestY  or  tests.test_x
            parts = cls.split(".")
            for i in range(len(parts), 0, -1):
                p = "/".join(parts[:i]) + ".py"
                if (repo / p).exists():
                    bad.add(p)
                    break
            else:
                bad.add(cls or "<collection>")
    return bad


def apply(m):
    p = repo / m["file"]
    src = p.read_text()
    if m["kind"] == "py":
        lines = src.splitlines(keepends=True)
        lines.insert(m["line"] - 1, " " * m["col"] + m.get("text", MUTANT) + "\n")
        p.write_text("".join(lines))
    else:
        p.write_text("%%TIA_MUTANT%%{[\n")


baseline = set(json.loads(os.environ["BASELINE"])) if "BASELINE" in os.environ else failing_files()
print("baseline failures:", sorted(baseline), flush=True)
with out.open("a") as fh:
    for m in muts[START:]:
        apply(m)
        try:
            t = time.time()
            try:
                truth = failing_files() - baseline
            except subprocess.TimeoutExpired:
                m["skipped"] = "full suite hung"
                fh.write(json.dumps(m) + "\n")
                print(m["file"], m.get("func", ""), "SKIPPED (hang)", flush=True)
                continue
            m["truth"], m["full_s"] = sorted(truth), round(time.time() - t, 1)
            m["sel"] = {}
            for name, cmd in selectors.items():
                t = time.time()
                r = subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True, timeout=1800)
                picked = {l.strip() for l in r.stdout.splitlines() if l.strip()}
                m["sel"][name] = {"n": len(picked), "s": round(time.time() - t, 1),
                                  "missed": sorted(truth - picked)}
        finally:
            subprocess.run(["git", "checkout", "--", m["file"]], cwd=repo)
        fh.write(json.dumps(m) + "\n")
        fh.flush()
        print(m["file"], m.get("func", ""), "fail:", len(truth),
              {k: (v["n"], len(v["missed"])) for k, v in m["sel"].items()}, flush=True)
