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


def _mode(config) -> str | None:
    if config.getoption("testscope_off") or os.environ.get("TESTSCOPE") == "off":
        return None
    if config.getoption("testscope_record"):
        return "record"
    if config.getoption("testscope") or os.environ.get("TESTSCOPE") == "on":
        return "select"
    if any(os.environ.get(k) for k in AGENT_ENV):
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
    if m is None:
        if config.getoption("testscope") or os.environ.get("TESTSCOPE") == "on":
            raise pytest.UsageError("testscope: no map. Run `pytest --testscope-record` first (or fetch it from CI).")
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

    def _ids(self, funcs, files, dirs):
        out = set()
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
                    ids |= self._ids([tuple(x) for x in d["funcs"]], d["files"], d["dirs"])
                except (OSError, ValueError, KeyError):
                    pass
            shutil.rmtree(out, ignore_errors=True)
            path = item.nodeid.split("::", 1)[0]
            self.tests[item.nodeid] = ids | self.module_deps.get(path, set())

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)

    def _shard(self):
        names = sorted(self.deps, key=self.deps.get)
        return {"deps": names, "tests": {k: sorted(v) for k, v in self.tests.items()}}

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
        for sh in shards:
            for nid, ids in sh["tests"].items():
                tests[nid] = sorted({deps.setdefault(sh["deps"][i], len(deps)) for i in ids})
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, capture_output=True,
                                    text=True).stdout.strip()
        except OSError:
            commit = ""
        m = {"version": 1, "created": int(time.time()), "commit": commit,
             "python": sys.version.split()[0], "snap": snapshot(self.root),
             "deps": sorted(deps, key=deps.get), "tests": tests}
        with open(os.path.join(d, "map.json"), "w") as fh:
            json.dump(m, fh, separators=(",", ":"))
        shutil.rmtree(os.path.join(d, "tmp"), ignore_errors=True)
        with open(os.path.join(d, "lastfailed.json"), "w") as fh:
            json.dump(sorted(self.failed), fh)
        session.config.pluginmanager.get_plugin("terminalreporter").write_line(
            f"testscope: recorded {len(tests)} tests, {len(deps)} deps -> {_sel.MAP_DIR}/map.json")
