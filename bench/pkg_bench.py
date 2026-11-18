"""Faults in installed packages, as a bad upgrade would bring: put `raise` at the top of one
function in a dependency and raise the version in its METADATA. A full run gives the ground
truth; testscope's selection (tests whose code ran inside that package) is compared with it.
Usage: python pkg_bench.py <repo> <out.jsonl> <dist>:<relpath in site-packages>:<function> ...
Run inside the repo's venv; the package files are restored after each fault."""
import ast, glob, json, os, re, shutil, subprocess, sys, sysconfig, xml.etree.ElementTree as ET
from pathlib import Path

from testscope import selection

repo, out, faults = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
site = Path(sysconfig.get_paths()["purelib"])
env = {**os.environ, "TESTSCOPE": "off"}
env.pop("CLAUDECODE", None)


def failing():
    j = repo / ".pkg_junit.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-n", "12", "-p", "no:testmon", "-p", "no:cacheprovider",
                    "-o", "junit_family=xunit1", f"--junitxml={j}", "tests"], cwd=repo, env=env, capture_output=True, timeout=1800)
    bad = set()
    for tc in ET.parse(j).getroot().iter("testcase"):
        if tc.find("failure") is not None or tc.find("error") is not None:
            bad.add(tc.get("file") or "<collection>")  # xunit1 carries the file path
    j.unlink()
    return bad


base = failing()
m = selection.load_map(str(repo))
with out.open("w") as fh:
    for spec in faults:
        dist, rel, func = spec.split(":")
        src = site / rel
        meta = Path(glob.glob(str(site / f"{dist}-*.dist-info" / "METADATA"))[0])
        keep = {src: src.read_text(), meta: meta.read_text()}
        try:
            tree = ast.parse(keep[src])
            fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func)
            first = fn.body[0]
            lines = keep[src].splitlines(keepends=True)
            lines.insert(first.lineno - 1, " " * first.col_offset + 'raise RuntimeError("TIA_MUTANT")\n')
            src.write_text("".join(lines))
            meta.write_text(re.sub(r"^Version: (.*)$", r"Version: \1.post99", keep[meta], count=1, flags=re.M))
            truth = sorted(failing() - base)
            s = selection.compute(str(repo), m)
            picked = {n.split("::")[0] for n in s.tests}
            row = {"fault": spec, "truth": truth, "full": s.full, "selected_tests": len(s.tests),
                   "selected_files": len(picked), "missed": sorted(set(truth) - picked), "changed": s.changed}
        finally:
            for p, t in keep.items():
                p.write_text(t)
            shutil.rmtree(src.parent / "__pycache__", ignore_errors=True)
        fh.write(json.dumps(row) + "\n")
        fh.flush()
        print(row["fault"], "truth", len(row["truth"]), "sel", row["selected_tests"], row["selected_files"],
              "missed", row["missed"], flush=True)
