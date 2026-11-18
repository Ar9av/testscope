"""Summarize ai_bench.py runs. Usage: python ai_report.py <results.json> <run.jsonl ...>
results.json gives the flaky set used for the main report, so all selectors share one ground truth."""
import json, statistics as st, sys
from pathlib import Path

flaky = set(json.loads(Path(sys.argv[1]).read_text())["flaky"])
out = {}
for f in sys.argv[2:]:
    rows = [json.loads(l) for l in Path(f).read_text().splitlines()]
    mut = [r for r in rows if r["src"] == "mutant"]
    com = [r for r in rows if r["src"] == "commit"]
    calls = [r[m] for r in rows for m in ("union", "solo") if m in r]
    ok = [c for c in calls if "error" not in c]
    res = {"calls": len(calls), "errors": len(calls) - len(ok),
           "usd_total": round(sum(c["info"].get("usd") or 0 for c in ok), 3),
           "usd_per_call": round(st.mean(c["info"].get("usd") or 0 for c in ok), 4),
           "usd_per_call_after_first": round(st.median(c["info"].get("usd") or 0 for c in ok), 4),
           "seconds_median": st.median(c["info"]["seconds"] for c in ok),
           "seconds_p90": sorted(c["info"]["seconds"] for c in ok)[int(len(ok) * 0.9)],
           "input_tokens_median": st.median(c["info"]["input"] + c["info"]["cache_read"] + c["info"]["cache_write"] for c in ok),
           "output_tokens_median": st.median(c["info"]["output"] for c in ok)}
    for mode in ("union", "solo"):
        caught = allf = n = 0
        extra_files, extra_wrong = [], []
        for r in mut:
            real = set(r["truth"]) - flaky - {k for k, v in r["verdict"].items() if v == "flaky"}
            if mode not in r or "error" in r[mode]:
                continue
            add = set(r[mode]["add"])
            picked = (set(r["ts"]) if mode == "union" else set()) | add
            extra_files.append(len(add))
            extra_wrong.append(len(add - real))
            if real:
                n += 1
                caught += bool(picked & real)
                allf += real <= picked
        res[mode] = {"breaking": n, "caught": caught, "all_files": allf,
                     "files_added_median": st.median(extra_files) if extra_files else None,
                     "files_added_mean": round(st.mean(extra_files), 1) if extra_files else None,
                     "added_not_failing_mean": round(st.mean(extra_wrong), 1) if extra_wrong else None}
    # testscope alone, from the same recomputed selections (sanity check vs the main report)
    ts_c = ts_a = 0
    for r in mut:
        real = set(r["truth"]) - flaky - {k for k, v in r["verdict"].items() if v == "flaky"}
        if real:
            ts_c += bool(set(r["ts"]) & real)
            ts_a += real <= set(r["ts"])
    res["testscope_alone"] = {"caught": ts_c, "all_files": ts_a}
    good = [r for r in com if "error" not in r.get("union", {"error": 1})]
    res["commits"] = {"n": len(good),
                      "files_added_median": st.median(len(r["union"]["add"]) for r in good) if good else None,
                      "tests_added_median": st.median(r["union"]["tests"] for r in good) if good else None,
                      "tests_added_mean": round(st.mean(r["union"]["tests"] for r in good)) if good else None,
                      "ts_files_median": st.median(len(r["ts"]) for r in good) if good else None}
    out[Path(f).stem] = res
print(json.dumps(out, indent=1))
