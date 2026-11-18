"""Re-run one selector on already-measured mutants (ground truth is unchanged), e.g. after a
selector change. Usage: python reselect.py <repo> <in.jsonl> <out.jsonl> <name>=<cmd>"""
import json, subprocess, sys, time
from pathlib import Path

repo, src, dst = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
name, cmd = sys.argv[4].split("=", 1)
MUTANT = 'raise RuntimeError("TIA_MUTANT")'


def apply(m):
    p = repo / m["file"]
    if m["kind"] == "py":
        lines = p.read_text().splitlines(keepends=True)
        lines.insert(m["line"] - 1, " " * m["col"] + m.get("text", MUTANT) + "\n")
        p.write_text("".join(lines))
    else:
        p.write_text("%%TIA_MUTANT%%{[\n")


with dst.open("w") as fh:
    for line in src.read_text().splitlines():
        m = json.loads(line)
        if "sel" in m:
            apply(m)
            try:
                t = time.time()
                r = subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True, timeout=900)
                picked = {l.strip() for l in r.stdout.splitlines() if l.strip()}
                m["sel"][name] = {"n": len(picked), "s": round(time.time() - t, 1),
                                  "missed": sorted(set(m["truth"]) - picked)}
            finally:
                subprocess.run(["git", "checkout", "--", m["file"]], cwd=repo)
            print(m["file"], m.get("func", ""), name, m["sel"][name]["n"], m["sel"][name]["missed"], flush=True)
        fh.write(json.dumps(m) + "\n")
