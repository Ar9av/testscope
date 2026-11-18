# AGENTS.md — set up testscope in a repository

These steps are for a coding agent (Claude Code, Codex, Cursor, Gemini CLI, Copilot, …)
that must add testscope to a user's Python project. Do the steps in order. Each step has
a check. Do not continue if a check fails: tell the user what failed.

testscope is a pytest plugin. After one full recorded run, `pytest` runs only the tests
that the current change can affect.

## 0. Check that the project fits

- The project uses **pytest**. Look for `pytest` in `pyproject.toml`, `requirements*.txt`,
  `setup.cfg`, `tox.ini`, or `noxfile.py`, or a `tests/` directory with `test_*.py` files.
  If the project uses only `unittest` runners, stop: testscope needs pytest.
- **Python 3.12 or later** is needed to record the map. Run `python --version` in the
  project's environment (its venv, `uv run`, `poetry run`, …). Python 3.9–3.11 can select
  from a map that was recorded elsewhere (for example in CI), but cannot record one.

## 1. Install

Add `testscope` as a **dev / test dependency**, with the tool the project already uses:

| Project uses | Command |
|---|---|
| uv | `uv add --dev testscope` |
| Poetry | `poetry add --group dev testscope` |
| PDM | `pdm add -dG dev testscope` |
| Hatch / optional-deps in `pyproject.toml` | add `"testscope"` to the `dev` or `test` extra |
| `requirements-dev.txt` (or similar) | add a line `testscope` |
| none of these | `pip install testscope` in the project's venv |

Do not add it to the runtime dependencies.

Check: `pytest --help | grep testscope` shows `--testscope` and `--testscope-record`.

## 2. Ignore the map directory

Add this line to the project's `.gitignore`:

```
.testscope/
```

The map is local state. Never commit it.

## 3. Record the first map

```bash
pytest --testscope-record
```

Use the same arguments the project normally uses (for example `pytest -n auto tests`).
This is one full run of the suite, so it takes as long as a normal full run.
If the suite takes more than about 2 minutes, run it in the background and wait for it to
finish. Do not stop it part way: the map is written at the end.

Check: `.testscope/map.json` exists.

Tests that fail during recording are fine. They are rerun first on the next run.

## 4. Check that selection works

Make no code change, then run:

```bash
pytest --testscope
```

Check: the header contains `testscope: ... running affected tests only`, and pytest runs
few or no tests. If the header says `full run — <file> changed`, a config or lockfile is
different from when the map was recorded. That run records a new map, so the next run is
narrow.

## 5. Turn it on for agents

testscope turns on by itself when one of these variables is set in the environment and a
map exists: `CLAUDECODE` (Claude Code), `CODEX_SANDBOX` or `CODEX_THREAD_ID` (Codex),
`CURSOR_AGENT` (Cursor), `GEMINI_CLI` (Gemini CLI). For these agents, plain `pytest` is
enough.

For any other agent, set `TESTSCOPE=on` in that agent's environment. For example, put it in
the agent's settings file, or in the shell profile that the agent uses.

Then add this text to the project's agent instructions file (`AGENTS.md`, `CLAUDE.md`,
`.cursor/rules`, `GEMINI.md`, or the file the project already has). Create `AGENTS.md`
if the project has none:

```markdown
## Tests

This project uses testscope. Run `pytest` as usual: it runs only the tests your change can
affect. The header line that starts with `testscope:` tells you what was selected.

- To run one test or file, name it (`pytest tests/test_x.py::test_y`). Named targets are
  never narrowed.
- To run the full suite, use `TESTSCOPE=off pytest`. Do this before you say a large
  change is done.
- To see why each test ran, read `.testscope/last-selection.json`.
- If the header says there is no map, run `pytest --testscope-record` once (a full run).
- Do not commit `.testscope/`.
```

## 6. CI (only if the user asks for it)

Copy [`examples/github-actions.yml`](examples/github-actions.yml) to
`.github/workflows/`. Change the install step and the pytest arguments to match the
project's own CI. Keep the three parts:

- **push to main:** `pytest --testscope-record`, and save `.testscope/map.json` to the
  Actions cache.
- **pull requests:** restore the newest map, then `pytest --testscope`. If no map is
  found, run the full suite.
- **merge queue and nightly:** plain `pytest`, a full run. Do not remove this part. It
  catches what selection misses.

If the project already has a test workflow, add these steps to it. Do not add a second
workflow that runs the same tests.

## Settings

| Setting | Effect |
|---|---|
| `pytest --testscope-record` | full run, writes `.testscope/map.json` |
| `pytest --testscope` or `TESTSCOPE=on` | run only affected tests; error if no map |
| `TESTSCOPE=off` or `--testscope-off` | run everything |
| `TESTSCOPE_AUTORECORD=off` | do not record a new map when a change forces a full run |
| `TESTSCOPE_ORDER=off` | keep pytest's order (default: last failures first) |
| `TESTSCOPE_AI=<backend>` | no map yet: a model guesses the tests from the diff (see README) |

## Things testscope does not see

Tell the user about these if they apply to the project:

- Tests that start a child process with `env={}`, `python -I`, or `python -E`.
- Network services, databases, and time.
- Packages that are only compiled extensions with no Python code.
- Code built from `eval` / `exec` strings that no recorded test ran.

The full CI run on merge and nightly is the safety net for these.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `--testscope-record needs Python 3.12+` | Record in a 3.12+ environment, or in CI. |
| `no map for this version` | Run `pytest --testscope-record`. The map format changed or no map exists. |
| Every run is a full run | A config file changed: `pyproject.toml`, `setup.py`, `setup.cfg`, `tox.ini`, `pytest.ini`, `noxfile.py`, `requirements*.txt`, `constraints*.txt`, `*.lock`, `Pipfile`, or `.python-version`. The full run records a new map. If `TESTSCOPE_AUTORECORD=off` is set, record again by hand. |
| Plain `pytest` runs everything under an agent | The agent sets none of the variables in step 5. Set `TESTSCOPE=on`. |
| A git worktree has no map | It uses the main checkout's map. No action needed. |
