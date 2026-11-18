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

## Overhead

| | time |
|---|---|
| Plain full run | 3m36s |
| `pytest --testscope-record` | 3m24s |
| testmon DB build | 3m59s |

Recording adds no measurable cost (run-to-run noise is larger).

## Limits of this evidence

One project. Faults are synthetic. Real commits are measured for size, not for misses.
Run the harness on your own suite before you trust it there.
