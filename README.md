# testscope

Run only the tests your change can affect. Built for AI coding agents and CI.

Coding agents run `pytest` after every edit. On a 6,000-test suite that is minutes per
run. It goes past Claude Code's ~2-minute foreground limit, so the run moves to the
background and the agent stops reading its output. testscope records which code and
files each test really touches. After that, plain `pytest` runs only the tests your
change can reach.

```bash
pip install testscope
pytest --testscope-record      # once (or in CI on main): full run + dependency map
# ... edit code ...
pytest --testscope             # only affected tests
```

When Claude Code runs pytest (it sets `CLAUDECODE=1`) and a map exists, selection turns
on automatically. The agent needs no new command and no hook. For other agents, export
`TESTSCOPE=on` in their environment.
`TESTSCOPE=off pytest` always runs everything. Explicit targets
(`pytest tests/test_x.py::test_y`) are never narrowed.

## What it tracks

| Dependency | How | testmon | import graph |
|---|---|---|---|
| Functions a test runs | `sys.monitoring` PY_START, disabled after the first hit, re-armed per test | yes (coverage) | no |
| Code run in child Python processes | `sitecustomize` on `PYTHONPATH`, tagged with the running test | no | no |
| Data files read (YAML, JSON, fixtures, shell scripts run) | audit hooks: `open`, `subprocess.Popen` | no | no |
| Files added to / removed from a listed directory | audit hooks: `os.listdir`, `os.scandir` | no | no |
| Module-level code (constants, decorators, class bodies) | conservative: tests that ran *any* code of the module, plus tests that import it transitively | yes | yes |
| Config / lockfile change (`pyproject.toml`, `*.lock`, `requirements*.txt`, …) | full run | full run | no |
| Tests that failed last run | always rerun | yes | no |

Function fingerprints are AST hashes without line numbers, so moving code or
changing a comment selects nothing. The map is a *baseline*. Selection compares
the working tree with it by content, so it works on uncommitted edits and on
branches built from an older main. As you edit, the selection can only grow.
Re-record to reset it.

## CI

[`examples/github-actions.yml`](examples/github-actions.yml):

- **push to main:** `pytest --testscope-record`; the map goes into the Actions cache.
- **pull requests:** restore the newest map; `pytest --testscope`.
- **merge queue + nightly:** plain full run. This is the safety net for what any selector can miss.

## Limits

- Recording needs Python 3.12+ (`sys.monitoring`). Selection works on any 3.9+.
- A test that starts a child process with a scrubbed environment (`env={}`), or with
  `python -I` / `-E`, hides that child's deps. Shell scripts that read files themselves
  are seen only as "script ran".
- Network services, databases and time are not dependencies. A flaky test stays flaky.
- `eval`/`exec` strings and other dynamic code that no recorded test reached cannot be mapped.

## Evidence

See [`bench/`](bench/): fault injection on a real 6,165-test suite. For each injected
fault, a full-suite run gives the ground truth, and the selectors are measured
against it.
