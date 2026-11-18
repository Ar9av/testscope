"""pytest plugin.

  pytest --testscope-record    full run; writes .testscope/map.json (baseline)
  pytest --testscope           run only tests the change since the baseline can affect
  TESTSCOPE=on|off|auto        auto (default) selects when an AI agent runs pytest
                               (CLAUDECODE / CODEX_* in env) and a map exists
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from . import selection as _sel
from .fingerprint import snapshot

BOOT = os.path.join(os.path.dirname(__file__), "_boot")
AGENT_ENV = ("CLAUDECODE", "CODEX_SANDBOX", "CODEX_THREAD_ID", "CURSOR_AGENT", "GEMINI_CLI")


def pytest_addoption(parser):
    g = parser.getgroup("testscope")
    g.addoption("--testscope", action="store_true", help="run only tests affected by changes since the map")
    g.addoption("--testscope-record", action="store_true", help="run tests and record the dependency map")
    g.addoption("--testscope-off", action="store_true", help="disable testscope selection")
    g.addoption("--testscope-ai", nargs="?", const="anthropic", default=None, metavar="BACKEND",
                help="no map yet: let an LLM pick test files from the diff (anthropic | claude | codex | opencode | cursor | gemini | qwen | copilot); "
                     "or set TESTSCOPE_AI. Ignored once a map exists.")


def _mode(config) -> str | None:
    if config.getoption("testscope_off") or os.environ.get("TESTSCOPE") == "off":
        return None
    if config.getoption("testscope_record"):
        return "record"
    ai = config.getoption("testscope_ai") or os.environ.get("TESTSCOPE_AI")
    if config.getoption("testscope") or os.environ.get("TESTSCOPE") == "on":
        return "select"
    if (ai and ai != "off") or any(os.environ.get(k) for k in AGENT_ENV):
        # Agent asked for specific files/ids: respect that, do not narrow further.
        for a in config.args:
            if "::" in a or os.path.isfile(a):
                return None
        return "select"
    return None


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    mode = _mode(config)
    if mode is None:
        return
    root = str(config.rootpath)
    if mode == "record":
        if sys.version_info < (3, 12):
            raise pytest.UsageError("testscope --testscope-record needs Python 3.12+ (sys.monitoring)")
        config.pluginmanager.register(_Record(config, root), "testscope-record")
        return
    m = _sel.load_map(root)
    backend = config.getoption("testscope_ai") or os.environ.get("TESTSCOPE_AI")
    if m is None and backend and backend != "off":
        config.pluginmanager.register(_AISelect(root, backend), "testscope-ai-select")
        return
    if m is None:
        if config.getoption("testscope") or os.environ.get("TESTSCOPE") == "on":
            raise pytest.UsageError("testscope: no map for this version. Run `pytest --testscope-record` "
                                   "first (or fetch it from CI).")
        return
    config.pluginmanager.register(_Select(config, root, m), "testscope-select")


def _lastfailed(root):
    try:
        return set(json.load(open(os.path.join(root, _sel.MAP_DIR, "lastfailed.json"))))
    except (OSError, ValueError):
        return set()


class _Select:
    def __init__(self, config, root, m):
        self.config, self.root, self.map = config, root, m
        self.sel = _sel.compute(root, m)
        self.failed_before = _lastfailed(root)
        self.ran, self.failed, self.passed = 0, set(), set()
        self.why: dict[str, str] = {}
        # Test files with nothing selected are never imported (collection is most of the
        # cost on a big suite). New files, unknown to the map, are always collected.
        self.known_files = {n.split("::", 1)[0] for n in m["tests"]}
        self.keep_files = {n.split("::", 1)[0] for n in list(self.sel.tests) + list(self.failed_before)}

    def pytest_ignore_collect(self, collection_path, config):
        if self.sel.full or not collection_path.is_file():
            return None
        rel = os.path.relpath(str(collection_path), self.root)
        if rel not in self.known_files or rel in self.keep_files:
            return None
        if any(rel == p or rel.startswith(p.rstrip("/") + "/") or p == "." for p in self.sel.paths):
            return None
        return True

    def pytest_report_header(self, config):
        s = self.sel
        if s.full:
            return f"testscope: full run — {s.full}"
        return (f"testscope: {len(s.changed)} changed file(s) since map {self.map.get('commit', '')[:10]}; "
                f"running affected tests only (TESTSCOPE=off for all)")

    def pytest_collection_modifyitems(self, config, items):
        keep, drop = [], []
        known = self.map["tests"]
        for it in items:
            why = self.sel.wants(it.nodeid, it.nodeid in known)
            if why is None and it.nodeid in self.failed_before:
                why = "failed last run"
            if why is None:
                drop.append(it)
            else:
                keep.append(it)
                self.why[it.nodeid] = why
        if drop:
            config.hook.pytest_deselected(items=drop)
        if not self.sel.full and os.environ.get("TESTSCOPE_ORDER") != "off":
            order = {f: i for i, f in enumerate(_sel.order_files(self.why, self.map.get("dur", {})))}
            keep.sort(key=lambda it: order.get(it.nodeid.split("::", 1)[0], len(order)))  # stable
        items[:] = keep
        self._write_why()

    def _write_why(self):
        d = os.path.join(self.root, _sel.MAP_DIR)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "last-selection.json"), "w") as fh:
            json.dump({"changed": self.sel.changed, "unobserved": self.sel.unobserved,
                       "full": self.sel.full, "selected": self.why}, fh, indent=1)

    def pytest_runtest_logreport(self, report):
        if report.when == "call" or report.outcome == "failed":
            if report.failed:
                self.failed.add(report.nodeid)
            elif report.when == "call":
                self.passed.add(report.nodeid)

    def pytest_sessionfinish(self, session):
        if hasattr(self.config, "workerinput"):
            return
        lf = (self.failed_before - self.passed) | self.failed
        with open(os.path.join(self.root, _sel.MAP_DIR, "lastfailed.json"), "w") as fh:
            json.dump(sorted(lf), fh)
        if session.exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and not self.sel.full:
            session.exitstatus = pytest.ExitCode.OK  # nothing affected is a pass, not an error

    def pytest_terminal_summary(self, terminalreporter):
        s = self.sel
        total = len(self.map["tests"])
        n = len(self.passed | self.failed)
        tr = terminalreporter
        tr.write_sep("-", "testscope")
        if s.full:
            tr.write_line(f"full run: {s.full}")
        elif not s.changed:
            tr.write_line(f"no changes since the map; ran {n} test(s) that failed last time")
        else:
            tr.write_line(f"ran {n} of ~{total} tests affected by: {', '.join(s.changed[:8])}"
                          + (" ..." if len(s.changed) > 8 else ""))
        if s.unobserved:
            tr.write_line(f"no recorded test touches: {', '.join(s.unobserved[:8])} "
                          f"(run the full suite if these matter)")
        tr.write_line("why each test ran: .testscope/last-selection.json · full suite: TESTSCOPE=off pytest")


class _AISelect:
    """No map yet: the LLM picks test files from the diff alone. Changed and new test files
    always run. A config or lockfile change, an empty diff, or an AI error runs everything."""

    def __init__(self, root, backend):
        self.root = root
        git = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True).stdout.split()
        try:
            self.changed = sorted(f for f in set(git("diff", "--name-only", "HEAD")) | set(git("ls-files", "-o", "--exclude-standard"))
                                  if not f.startswith(_sel.MAP_DIR + "/") and "__pycache__/" not in f)
        except OSError:
            self.changed = []
        self.full = None
        if not self.changed:
            self.full = "no changes against HEAD"
        elif any(_sel._match(f, _sel.FULL_RUN) for f in self.changed):
            self.full = "config or lockfile changed"
        self.ai, self.keep = None, set()
        if not self.full:
            sel = _sel.Selection()
            sel.changed = self.changed
            self.ai = _ai_suggest(root, sel, backend)
            if "error" in self.ai:
                self.full = f"ai step failed ({self.ai['error']})"
            self.keep = set(self.ai["add"]) | {f for f in self.changed if os.path.basename(f).startswith("test_")}

    def pytest_ignore_collect(self, collection_path, config):
        if self.full or not collection_path.is_file():
            return None
        rel = os.path.relpath(str(collection_path), self.root)
        name = os.path.basename(rel)
        if name.startswith("test_") and name.endswith(".py") and rel not in self.keep:
            return True
        return None

    def pytest_sessionfinish(self, session):
        if session.exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and not self.full:
            session.exitstatus = pytest.ExitCode.OK

    def pytest_report_header(self, config):
        if self.full:
            return f"testscope: no map; full run — {self.full}"
        return f"testscope: no map; running {len(self.keep)} test file(s) picked by AI (record a map for exact selection)"

    def pytest_terminal_summary(self, terminalreporter):
        tr = terminalreporter
        tr.write_sep("-", "testscope")
        if self.full:
            tr.write_line(f"no map; full run: {self.full}")
            return
        i = self.ai["info"]
        cost = f", ${i['usd']:.4f}" if i.get("usd") is not None else ""
        tr.write_line(f"no map; ai ({i.get('backend')}/{i.get('model')}) picked: {', '.join(sorted(self.keep)[:8])}"
                      f"{' ...' if len(self.keep) > 8 else ''} ({i.get('seconds')}s{cost})")
        tr.write_line("this is a guess from the diff; `pytest --testscope-record` gives exact selection")


def _ai_suggest(root, sel, backend):
    """Run the AI step once per distinct change: xdist workers and repeat runs read the cache."""
    import hashlib
    from . import ai

    path = os.path.join(root, _sel.MAP_DIR, "ai-cache.json")
    try:
        key = hashlib.sha256(json.dumps([backend, os.environ.get("TESTSCOPE_AI_MODEL"),
                                         sel.changed, ai.diff(root)]).encode()).hexdigest()
        try:
            with open(path) as fh:
                c = json.load(fh)
            if c.get("key") == key:
                return dict(c["result"], cached=True)
        except (OSError, ValueError, KeyError):
            pass
        add, info = ai.suggest(root, sel.changed, backend=backend)
        res = {"add": sorted(add), "info": info}
    except Exception as e:  # never fail the run because the AI step failed
        return {"add": [], "info": {"backend": backend}, "error": f"{type(e).__name__}: {e}"[:200]}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"key": key, "result": res}, fh)
    return res


class _Record:
    def __init__(self, config, root):
        from .recorder import Recorder

        self.config, self.root = config, root
        self.rec = Recorder(root)
        self.rec.install()
        self.deps: dict[str, int] = {}
        self.tests: dict[str, set[int]] = {}
        self.module_deps: dict[str, set[int]] = {}
        self.tmp = os.path.join(root, _sel.MAP_DIR, "tmp", os.environ.get("PYTEST_XDIST_WORKER", "main"))
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        self.n = 0
        self.failed: set[str] = set()
        self.dur: dict[str, float] = {}

    def _ids(self, funcs, files, dirs, pkgs=()):
        out = set()
        for p in pkgs:
            out.add(self.deps.setdefault(f"p:{p}", len(self.deps)))
        for f, q in funcs:
            out.add(self.deps.setdefault(f"f:{f}::{q}", len(self.deps)))
        for f in files:
            out.add(self.deps.setdefault(f"r:{f}", len(self.deps)))
        for d in dirs:
            out.add(self.deps.setdefault(f"d:{d}", len(self.deps)))
        return out

    # Import-time code of a test module runs during collection: charge it to the file.
    @pytest.hookimpl(hookwrapper=True)
    def pytest_make_collect_report(self, collector):
        is_mod = isinstance(collector, pytest.Module)
        if is_mod:
            self.rec.start()
        yield
        if is_mod:
            path = os.path.relpath(str(collector.path), self.root)
            self.module_deps[path] = self._ids(*self.rec.stop())

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.n += 1
        out = os.path.join(self.tmp, str(self.n))
        os.makedirs(out, exist_ok=True)
        saved = {k: os.environ.get(k) for k in ("TESTSCOPE_OUT", "TESTSCOPE_ROOT", "PYTHONPATH")}
        os.environ["TESTSCOPE_OUT"], os.environ["TESTSCOPE_ROOT"] = out, self.root
        os.environ["PYTHONPATH"] = os.pathsep.join([BOOT] + ([saved["PYTHONPATH"]] if saved["PYTHONPATH"] else []))
        self.rec.start()
        try:
            yield
        finally:
            ids = self._ids(*self.rec.stop())
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            for name in os.listdir(out):
                try:
                    d = json.load(open(os.path.join(out, name)))
                    ids |= self._ids([tuple(x) for x in d["funcs"]], d["files"], d["dirs"], d.get("pkgs", ()))
                except (OSError, ValueError, KeyError):
                    pass
            shutil.rmtree(out, ignore_errors=True)
            path = item.nodeid.split("::", 1)[0]
            self.tests[item.nodeid] = ids | self.module_deps.get(path, set())

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)
        self.dur[report.nodeid] = self.dur.get(report.nodeid, 0) + report.duration  # setup+call+teardown

    def _shard(self):
        names = sorted(self.deps, key=self.deps.get)
        return {"deps": names, "tests": {k: sorted(v) for k, v in self.tests.items()}, "dur": self.dur}

    def pytest_sessionfinish(self, session):
        d = os.path.join(self.root, _sel.MAP_DIR)
        wid = getattr(self.config, "workerinput", {}).get("workerid")
        if wid:
            with open(os.path.join(d, f"shard-{wid}.json"), "w") as fh:
                json.dump(self._shard(), fh)
            return
        shards = [self._shard()] if self.tests else []
        for name in sorted(os.listdir(d)):
            if name.startswith("shard-"):
                shards.append(json.load(open(os.path.join(d, name))))
                os.remove(os.path.join(d, name))
        deps: dict[str, int] = {}
        tests: dict[str, list[int]] = {}
        dur: dict[str, float] = {}
        for sh in shards:
            dur.update(sh.get("dur", {}))
            for nid, ids in sh["tests"].items():
                tests[nid] = sorted({deps.setdefault(sh["deps"][i], len(deps)) for i in ids})
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, capture_output=True,
                                    text=True).stdout.strip()
        except OSError:
            commit = ""
        m = {"version": _sel.MAP_VERSION, "created": int(time.time()), "commit": commit,
             "python": sys.version.split()[0], "snap": snapshot(self.root),
             "deps": sorted(deps, key=deps.get), "tests": tests,
             "pkgs": _sel.installed(d[2:] for d in deps if d.startswith("p:")),
             "dur": {k: round(v, 4) for k, v in dur.items() if k in tests}}
        with open(os.path.join(d, "map.json"), "w") as fh:
            json.dump(m, fh, separators=(",", ":"))
        shutil.rmtree(os.path.join(d, "tmp"), ignore_errors=True)
        with open(os.path.join(d, "lastfailed.json"), "w") as fh:
            json.dump(sorted(self.failed), fh)
        session.config.pluginmanager.get_plugin("terminalreporter").write_line(
            f"testscope: recorded {len(tests)} tests, {len(deps)} deps -> {_sel.MAP_DIR}/map.json")
