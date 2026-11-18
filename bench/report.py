"""Turn bench results into bench/RESULTS.md numbers and assets/selection.svg.
Usage: python report.py <verified mutants.jsonl ...> --commits <commits.jsonl> --out <dir>"""
import json, statistics as st, sys
from pathlib import Path

args = sys.argv[1:]
commits = Path(args[args.index("--commits") + 1])
out = Path(args[args.index("--out") + 1])
mut_files = [Path(a) for a in args[:args.index("--commits")]]

rows = [json.loads(l) for f in mut_files for l in f.read_text().splitlines()]
rows = [r for r in rows if "sel" in r]
flaky = {f for r in rows for f, v in r.get("verdict", {}).items() if v == "flaky"}
sels = ["testscope", "testmon", "imports", "names"]


def kind_of(r):
    if r["kind"] == "data":
        return "data"
    return "module" if r.get("func", "").startswith("<module>") else "py"


res = {}
for kind in ("py", "module", "data", "all"):
    rs = [r for r in rows if kind in ("all", kind_of(r))]
    rs = [dict(r, real=set(r["truth"]) - flaky) for r in rs]
    hit = [r for r in rs if r["real"]]
    res[kind] = {"mutants": len(rs), "breaking": len(hit)}
    for s in sels:
        have = [r for r in hit if s in r["sel"]]
        if not have:
            continue
        picked = lambda r: set(r["real"]) - set(r["sel"][s]["missed"])
        res[kind][s] = {
            "caught": sum(1 for r in have if picked(r)),
            "all_files": sum(1 for r in have if picked(r) == r["real"]),
            "of": len(have),
            "median_files": st.median(r["sel"][s]["n"] for r in rs if s in r["sel"]),
        }

cm = [json.loads(l) for l in commits.read_text().splitlines()]
total, cm = cm[0], cm[1:]
com = {"commits": len(cm), "total_tests": total["total_tests"], "total_files": total["total_files"]}
for s in ("testscope", "testmon"):
    com[s] = {"median_tests": st.median(r[s][0] for r in cm), "median_files": st.median(r[s][1] for r in cm),
              "mean_tests": round(st.mean(r[s][0] for r in cm)), "median_s": st.median(r[s][2] for r in cm)}
com["imports"] = {"median_files": st.median(r["imports"][1] for r in cm)}
(out / "results.json").write_text(json.dumps({"mutants": res, "commits": com, "flaky": sorted(flaky)}, indent=1))

# Chart: median tests run per real commit. One measure, one axis; testscope in the accent.
bars = [("Full suite", com["total_tests"]), ("testmon", com["testmon"]["median_tests"]),
        ("testscope", com["testscope"]["median_tests"])]
W, H, left, top, bh, gap = 720, 210, 120, 52, 30, 16
scale = (W - left - 90) / max(v for _, v in bars)
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
       'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">',
       "<style>.bg{fill:#fcfcfb}.t1{fill:#0b0b0b}.t2{fill:#52514e}.muted{fill:#c9c7c0}.acc{fill:#2a78d6}"
       ".grid{stroke:#e6e4de}@media (prefers-color-scheme:dark){.bg{fill:#1a1a19}.t1{fill:#f0efec}"
       ".t2{fill:#c3c2b7}.muted{fill:#55544f}.acc{fill:#3987e5}.grid{stroke:#33332f}}</style>",
       f'<rect class="bg" width="{W}" height="{H}" rx="8"/>',
       f'<text class="t1" x="20" y="28" font-size="15" font-weight="600">Tests run per change, median of '
       f'{com["commits"]} real commits</text>']
for i, (name, v) in enumerate(bars):
    y = top + i * (bh + gap)
    w = max(2, v * scale)
    cls = "acc" if name == "testscope" else "muted"
    svg += [f'<text class="t2" x="{left - 12}" y="{y + bh / 2 + 5}" font-size="13" text-anchor="end">{name}</text>',
            f'<rect class="{cls}" x="{left}" y="{y}" width="{w:.1f}" height="{bh}" rx="4"/>',
            f'<text class="t1" x="{left + w + 8:.1f}" y="{y + bh / 2 + 5}" font-size="13" '
            f'font-weight="600">{v:,.0f}</text>']
svg.append("</svg>")
(out / "selection.svg").write_text("\n".join(svg))
print(json.dumps(res, indent=1)); print(json.dumps(com, indent=1))
