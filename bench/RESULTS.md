# Results

Measured on one real project: a Python codebase with 6,195 tests in 378 test files
(pytest, 12 xdist workers). The full suite takes 3m36s. Raw rows are in [`data/`](data/),
and the summary is in [`results.json`](results.json).

## Real commits

The last 30 non-merge commits that touch code, each reverse-applied to the working tree
against a map recorded at HEAD ([`commits.py`](commits.py)).

| | median tests | mean tests | median test files | selection time (median) |
|---|---|---|---|---|
| Full suite | 6,195 | 6,195 | 378 | - |
| testscope | 353 | 652 | 54.5 | 4.9 s |
| pytest-testmon | 584 | 836 | 76 | 3.3 s |
| Import graph (transitive importers) | - | - | 352 | - |

The testscope numbers include 34 tests that failed at record time in that checkout
(environment failures). testscope always reruns the tests that failed last time. Without
them, testscope selects fewer tests than testmon on 21 of 30 commits (1 tie).

The testmon numbers come from an earlier run on the same 30 commits. In the final run,
one extra package was installed in the venv. testmon fingerprints the installed packages,
so it then selected the full suite on every commit. That is documented testmon
behavior, and the final run is not a fair measure of it.

## Fault injection

Each fault is put into the working tree. A full-suite run gives the ground truth (the
test files that fail). Then each selector is asked which files to run.

- **py**: `raise RuntimeError` as the first line of a function ([`mutants.py`](mutants.py)).
- **module**: a module-level constant set to `None` ([`module_mutants.py`](module_mutants.py)).
- **data**: a YAML, JSON, shell or fixture file replaced with garbage.

"Caught" means the selection includes at least one failing file, so the run goes red.
"All files" means it includes every failing file.

| kind | breaking faults | testscope caught / all files | testmon caught / all files | import graph caught / all files |
|---|---|---|---|---|
| py | 20 | 20 / 19 | 18 / 15 | 18 / 17 |
| module | 4 | 3 / 3 | 4 / 4 | 4 / 4 |
| data | 10 | 10 / 10 | 0 / 0 | 0 / 0 |
| **all** | **34** | **33 / 32** | **22 / 19** | **22 / 21** |

Median files selected per fault: testscope 7, testmon 5, import graph 352.

Notes:

- The module miss (`A2A_SCREENED_METHODS = None`) crashes an import. The testscope
  selection does go red, through a collection error in a test file that imports the
  module. The harness counts collection errors as a baseline bucket, so it cannot credit
  this case. We report it as a miss.
- 54 faults were run. 20 broke no test. 3 more module faults made the suite hang and were
  skipped.
- Flaky tests: each missed file was rerun alone, 2 times with the fault and 2 times
  without ([`verify.py`](verify.py)). A miss counts only if the file fails 2/2 with the
  fault and passes 2/2 clean. Files that are flaky are removed from the ground truth
  for all selectors.

## Package upgrades

A bad upgrade of a dependency, simulated: `raise` at the top of one function in an installed
package, and its version raised in `METADATA` ([`pkg_bench.py`](pkg_bench.py), rows in
[`data/pkg_faults.jsonl`](data/pkg_faults.jsonl)). A full run gives the failing files.

| fault | failing files | testscope tests run | testscope files missed | testmon |
|---|---|---|---|---|
| PyYAML `safe_load` | 58 | 2,278 | 4 | full suite (6,195) |
| PyYAML `dump` | 9 | 2,278 | 0 | full suite |
| cryptography `Ed25519PublicKey.from_public_bytes` | 7 | 763 | 0 | full suite |
| PyJWT `decode` | 4 | 92 | 0 | full suite |
| requests `request` | 0 | 33 | - | full suite |

testscope records which installed packages each test's code ran in, and also selects the
tests that ran a repo function whose code names the package (a cache filled by an earlier
test hides the call otherwise). The 4 missed `safe_load` files use YAML only at import
time, to build a module constant; covering that needs the import graph. Before this change
testscope did not notice upgrades at all, and testmon reruns everything on any change to
the environment.

## Fail-first order

Selected test files run in this order: failed last time, then tests that ran the changed
code, then indirect reasons, then import canaries; shorter files first in each group
([`order_bench.py`](order_bench.py)). Time is the sum of recorded file durations before the
first failing file, one process, on the 33 caught faults, after a clean previous run.

| order | median | mean | p90 |
|---|---|---|---|
| pytest default (by path) | 0 s | 21.3 s | 72.4 s |
| testscope fail-first | 0 s | 4.0 s | 7.2 s |

In most faults the first file already fails in both orders. Fail-first cuts the tail: an
agent that runs `pytest -x` sees the failure in seconds, not a minute.

## Overhead

| | time |
|---|---|
| Plain full run | 3m36s |
| `pytest --testscope-record` | 3m24s |
| testmon DB build | 3m59s |

Recording adds no measurable cost (run-to-run noise is larger). Measured again with package
and duration tracking, on an idle machine: plain run 2m13s, record 1m59s.

## Limits of this evidence

One project. Faults are synthetic. Real commits are measured for size, not for misses.
Run the harness on your own suite before you trust it there.

## AI step

`--testscope-ai` (or `TESTSCOPE_AI=anthropic|claude`) sends the diff and a test index
(each test file with the repo modules it imports, about 30k tokens) to a model. The test
index is the same on every call, so the prompt cache serves it. Two modes:

- **With a map ("union"):** the model sees the testscope selection and may only add files.
  Measured, then removed from testscope, because it caught nothing extra.
- **Without a map ("solo"):** the model picks test files from the diff alone. This is the
  mode testscope ships (`TESTSCOPE_AI`, used only when there is no map).

Same 34 breaking faults and same 30 commits as above ([`ai_bench.py`](ai_bench.py),
[`ai_report.py`](ai_report.py), raw rows in [`data/ai/`](data/ai/)). Prices are list
prices from the API usage fields.

| mode | model, effort | caught | all files | files added per fault (median) | tests added per real commit (median) | $ per call | s per call (median) |
|---|---|---|---|---|---|---|---|
| testscope alone | - | 33 | 32 | - | - | 0 | - |
| union | Opus 5.5, low | 33 | 32 | 2 | 49.5 | 0.0094 | 2.4 |
| union | Opus 5.5, medium | 33 | 32 | 2 | 35.5 | 0.0127 | 3.3 |
| union | Sonnet 5.5, low | 33 | 32 | 3 | 88 | 0.0052 | 2.1 |
| union | Haiku 5.5, low | 33 | 32 | 2.5 | 7.5 | 0.0005 | 1.7 |
| solo (no map) | Opus 5.5, low | 33 | 21 | 3 | - | 0.0094 | 2.4 |
| solo (no map) | Opus 5.5, medium | 33 | 20 | 3 | - | 0.0127 | 3.3 |
| solo (no map) | Sonnet 5.5, low | 33 | 21 | 3.5 | - | 0.0052 | 2.1 |
| solo (no map) | Haiku 5.5, low | 31 | 19 | 3 | - | 0.0005 | 1.7 |

For comparison, without a map: testmon catches 22 (it also needs a recorded DB), and the
import graph catches 22 at 352 files.

What this shows:

- **With a map, the AI step found nothing new.** On no fault did it add a failing file that
  testscope missed. The one fault testscope misses (an import crash in a module constant)
  was missed by every model too. It adds 2 to 3 files per fault that do not fail, and 7 to
  88 tests per real commit. Leave it off unless your suite has deps testscope cannot see.
- **Without a map, the AI step is a good first guess.** It caught 33 of 34 faults while it
  ran about 3 test files. It found every failing file only 21 times in 34, so treat it as a
  guess until you record a map.
- **Cost:** the first call writes the test index to the cache (about $0.15 on Opus 5.5).
  After that, a call is about 1 cent on Opus 5.5 and 0.05 cents on Haiku 5.5. The cache
  lives 5 minutes, so an agent that runs tests every few minutes stays on cache reads.
- **Claude Code CLI backend (`TESTSCOPE_AI=claude`):** uses the Claude Code login, no API
  key. It works (70 calls, no errors, 8.3 s median). Its prompt cache hit for only a few
  calls and then stopped, so most calls wrote the whole prompt again, $0.25 at list price.
  On a claude.ai plan that counts against plan limits, not money. A plain `claude -p`
  loads the full agent context (about 450k tokens per call); testscope runs it with no
  tools, MCP, settings or project files.
- The faults are synthetic. A `raise` in a named function is easy to match by name, so
  the solo numbers are likely better than on real bugs.
