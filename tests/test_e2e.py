"""End to end: record a tiny project, edit one thing, check exactly what gets selected."""
import json
import sys
import textwrap

import pytest

pytest_plugins = "pytester"
pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="recording needs sys.monitoring")

FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": """
        import os
        CONST = 3
        def f():
            return 1
        def g():
            return 2
        def load():
            return open(os.path.join(os.path.dirname(__file__), "data.yaml")).read()
    """,
    "pkg/data.yaml": "k: v\n",
    "pkg/run.sh": "#!/bin/bash\nsource \"$(dirname \"$0\")/_lib.sh\"\necho \"$GREETING\"\n",
    "pkg/_lib.sh": "GREETING=hi\n",
    "pkg/b.py": """
        from pkg.a import CONST
        LIMIT = CONST * 2
        def h():
            return LIMIT
    """,
    "tests/conftest.py": """
        import pytest
        @pytest.fixture
        def fx():
            return 7
    """,
    "tests/test_y.py": """
        from pkg import b
        def test_h(): assert b.h() == 6
        def test_fx(fx): assert fx == 7
    """,
    "tests/test_x.py": """
        import subprocess, sys
        from pkg import a
        def test_f(): assert a.f() == 1
        def test_g(): assert a.g() == 2
        def test_data(): assert "k" in a.load()
        def test_const(): assert a.CONST == 3
        def test_sub():
            subprocess.run([sys.executable, "-c", "from pkg import a; a.g()"], check=True)
        def test_none(): assert True
        def test_sh():
            out = subprocess.run(["bash", "pkg/run.sh"], capture_output=True, text=True).stdout
            assert out.strip() == "hi"
    """,
    "pyproject.toml": "[tool.pytest.ini_options]\npythonpath = ['.']\n",
}


@pytest.fixture
def proj(pytester):
    for path, src in FILES.items():
        p = pytester.path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src))
    r = pytester.runpytest_subprocess("--testscope-record", "-p", "no:cacheprovider")
    r.assert_outcomes(passed=9)
    return pytester


def selected_raw(p):
    p.runpytest_subprocess("--testscope", "-p", "no:cacheprovider")
    return (p.path / ".testscope/last-selection.json").read_text()


def selected(p, canaries=False):
    sel = json.loads(selected_raw(p))["selected"]
    return {n for n, why in sel.items() if canaries or "import canary" not in why}


def edit(p, path, old, new):
    f = p.path / path
    s = f.read_text()
    assert old in s
    f.write_text(s.replace(old, new))


def ids(*names):
    return {n if "::" in n else f"tests/test_x.py::{n}" for n in names}


ALL = {"test_f", "test_g", "test_data", "test_const", "test_sub", "test_none", "test_sh",
       "tests/test_y.py::test_h", "tests/test_y.py::test_fx"}


def test_nothing_changed(proj):
    assert selected(proj) == set()


def test_function_body(proj):
    edit(proj, "pkg/a.py", "return 1", "return 1  # same\n    pass")
    assert selected(proj) == ids("test_f")


def test_function_reached_only_in_subprocess(proj):
    edit(proj, "pkg/a.py", "return 2", "return 2 or 0")
    assert selected(proj) == ids("test_g", "test_sub")


def test_script_sourced_by_another_script(proj):
    # bash reads _lib.sh, so no audit hook sees it; run.sh (which tests do run) names it.
    edit(proj, "pkg/_lib.sh", "hi", "bye")
    assert selected(proj) == ids("test_sh")


def test_data_file(proj):
    edit(proj, "pkg/data.yaml", "k: v", "k: w")
    assert selected(proj) == ids("test_data")


def test_module_level_constant_follows_readers(proj):
    # test_const reads a.CONST; b.LIMIT is derived from it at import time; test_h reads LIMIT.
    edit(proj, "pkg/a.py", "CONST = 3", "CONST = 4")
    assert selected(proj) == ids("test_const", "tests/test_y.py::test_h")


def test_adding_import_or_function_selects_only_import_canaries(proj):
    edit(proj, "pkg/a.py", "import os", "import os\nimport json\ndef new():\n    return json")
    reasons = json.loads(selected_raw(proj))["selected"]
    assert reasons and all("import canary" in r for r in reasons.values()) and len(reasons) <= 5


def test_import_crash_is_caught(proj):
    # CONST = None makes `CONST * 2` in pkg/b.py raise at import: no reader of CONST runs,
    # but the canaries do, so the run fails instead of passing green.
    edit(proj, "pkg/a.py", "CONST = 3", "CONST = 3\nBROKEN = 1 / 0")
    r = proj.runpytest_subprocess("--testscope", "-p", "no:cacheprovider")
    assert r.ret != 0


def test_removing_a_function_selects_its_callers(proj):
    edit(proj, "pkg/a.py", "def g():\n    return 2\n", "")
    assert selected(proj) == ids("test_g", "test_sub")


def test_fixture_change_and_new_fixture(proj):
    edit(proj, "tests/conftest.py", "return 7", "return 8")
    assert selected(proj) == ids("tests/test_y.py::test_fx")


def test_config_change_runs_everything(proj):
    edit(proj, "pyproject.toml", "pythonpath", "# x\npythonpath")
    assert selected(proj) == ids(*ALL)


def test_last_failed_is_rerun(proj):
    edit(proj, "pkg/a.py", "return 1", "return 5")
    proj.runpytest_subprocess("--testscope", "-p", "no:cacheprovider")
    edit(proj, "pkg/a.py", "return 2", "return 2 or 0")  # unrelated change; test_f still broken
    assert ids("test_f") <= selected(proj)


def test_nothing_affected_exits_zero(proj):
    edit(proj, "pkg/a.py", "def load():", "def unused():\n    return 0\ndef load():")
    proj.runpytest_subprocess("--testscope", "-p", "no:cacheprovider")  # module change selects some
    (proj.path / "pkg/a.py").write_text(textwrap.dedent(FILES["pkg/a.py"]))
    r = proj.runpytest_subprocess("--testscope", "-p", "no:cacheprovider")
    assert r.ret == 0



def _no_map_project(pytester, monkeypatch, tmp_path_factory, claude_sh):
    import subprocess
    for path, src in FILES.items():
        p = pytester.path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src))
    bin_ = tmp_path_factory.mktemp("bin")  # outside the project: not a change
    (bin_ / "claude").write_text(claude_sh)
    (bin_ / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("TESTSCOPE_AI", "claude")
    git = lambda *a: subprocess.run(["git", *a], cwd=pytester.path, check=True, capture_output=True)
    git("init", "-q"); git("add", "."); git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    edit(pytester, "pkg/b.py", "* 2", "* 2 + 0")
    return bin_


def test_ai_without_map_picks_from_diff_and_caches(pytester, monkeypatch, tmp_path_factory):
    calls = tmp_path_factory.mktemp("calls") / "calls.txt"
    monkeypatch.setenv("TS_CALLS", str(calls))
    _no_map_project(pytester, monkeypatch, tmp_path_factory,
                    "#!/bin/sh\ncat > /dev/null\necho x >> \"$TS_CALLS\"\n"
                    "echo '{\"structured_output\": {\"add\": [\"tests/test_y.py\", \"nope.py\"]}, \"usage\": {}}'\n")
    for _ in range(2):
        r = pytester.runpytest_subprocess("-p", "no:cacheprovider")
        r.assert_outcomes(passed=2)  # only tests/test_y.py; unknown paths are dropped
        assert "picked by AI" in r.stdout.str()
    assert calls.read_text().count("x") == 1  # second run served from the cache


def test_ai_failure_runs_everything(pytester, monkeypatch, tmp_path_factory):
    _no_map_project(pytester, monkeypatch, tmp_path_factory, "#!/bin/sh\necho boom >&2\nexit 1\n")
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    r.assert_outcomes(passed=9)
    assert "ai step failed" in r.stdout.str()


def test_package_upgrade_selects_only_its_users(pytester, monkeypatch):
    site = pytester.path / "vend" / "site-packages"  # outside the repo's tracked files
    (site / "fakepkg").mkdir(parents=True)
    (site / "fakepkg" / "__init__.py").write_text("def ping():\n    return 1\n")
    meta = site / "fakepkg-1.0.dist-info"
    meta.mkdir()
    (meta / "METADATA").write_text("Metadata-Version: 2.1\nName: fakepkg\nVersion: 1.0\n")
    (meta / "top_level.txt").write_text("fakepkg\n")
    monkeypatch.setenv("PYTHONPATH", str(site))
    for path, src in FILES.items():
        p = pytester.path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src))
    (pytester.path / ".gitignore").write_text("vend/\n")
    (pytester.path / "tests/test_z.py").write_text("import fakepkg\ndef test_ping(): assert fakepkg.ping() == 1\n")
    # A cache: only the first caller runs the package's code; the second still depends on it.
    (pytester.path / "pkg/c.py").write_text("import fakepkg\n_C = {}\ndef cached():\n"
                                            "    if 'v' not in _C:\n        _C['v'] = fakepkg.ping()\n    return _C['v']\n")
    (pytester.path / "tests/test_c.py").write_text("from pkg import c\ndef test_c1(): assert c.cached() == 1\n"
                                                   "def test_c2(): assert c.cached() == 1\n")
    pytester.runpytest_subprocess("--testscope-record", "-p", "no:cacheprovider").assert_outcomes(passed=12)
    assert selected(pytester) == set()
    (meta / "METADATA").write_text("Metadata-Version: 2.1\nName: fakepkg\nVersion: 1.1\n")
    sel = json.loads(selected_raw(pytester))["selected"]
    assert set(sel) == {"tests/test_z.py::test_ping", "tests/test_c.py::test_c1", "tests/test_c.py::test_c2"}
    assert "fakepkg==1.0 -> fakepkg==1.1" in sel["tests/test_z.py::test_ping"]


def test_fail_first_order():
    from testscope.selection import order_files
    why = {"tests/a.py::t": "pkg/x.py: import canary (module-level change)",
           "tests/b.py::t": "pkg/x.py::f changed",
           "tests/c.py::t": "failed last run",
           "tests/e.py::t": "pkg/x.py::f changed",
           "tests/d.py::t": "pkg/data.yaml references pkg/x.yaml"}
    assert order_files(why, {"tests/b.py::t": 5.0, "tests/e.py::t": 0.1}) == [
        "tests/c.py", "tests/e.py", "tests/b.py", "tests/d.py", "tests/a.py"]


def test_full_run_refreshes_the_map(proj):
    edit(proj, "pyproject.toml", "pythonpath", "# x\npythonpath")
    r = proj.runpytest_subprocess("--testscope", "-p", "no:cacheprovider")
    r.stdout.fnmatch_lines(["*recording a fresh map*", "*recorded 9 tests*"])
    assert selected(proj) == set()  # the config change is now in the map
    edit(proj, "pkg/a.py", "return 1", "return 5")
    assert ids("test_f") <= selected(proj)


def test_partial_full_run_keeps_the_old_map(proj):
    before = (proj.path / ".testscope/map.json").read_text()
    edit(proj, "pyproject.toml", "pythonpath", "# x\npythonpath")
    r = proj.runpytest_subprocess("--testscope", "-p", "no:cacheprovider", "-k", "test_h")
    r.stdout.fnmatch_lines(["*map not refreshed*1 of 9*"])
    assert (proj.path / ".testscope/map.json").read_text() == before


def test_autorecord_off(proj, monkeypatch):
    monkeypatch.setenv("TESTSCOPE_AUTORECORD", "off")
    before = (proj.path / ".testscope/map.json").read_text()
    edit(proj, "pyproject.toml", "pythonpath", "# x\npythonpath")
    assert selected(proj) == ids(*ALL)
    assert (proj.path / ".testscope/map.json").read_text() == before


def test_full_run_refreshes_the_map_under_xdist(proj):
    pytest.importorskip("xdist")
    edit(proj, "pyproject.toml", "pythonpath", "# x\npythonpath")
    r = proj.runpytest_subprocess("--testscope", "-p", "no:cacheprovider", "-n", "2")
    r.stdout.fnmatch_lines(["*recorded 9 tests*"])
    edit(proj, "pkg/a.py", "return 1", "return 5")
    assert ids("test_f") <= selected(proj) < ids(*ALL)


def test_no_resource_warnings_under_filterwarnings_error(pytester):
    # Projects with filterwarnings=error turn any file testscope leaves open into a test error.
    pytester.makepyprojecttoml("[tool.pytest.ini_options]\nfilterwarnings = ['error']\n")
    pytester.makefile(".yaml", data="k: v\n")
    pytester.makepyfile(m="def f():\n    return 1\n",
                        test_m="from m import f\ndef test_f(): assert f() == 1\ndef test_g(): pass\n")
    pytester.syspathinsert()
    r = pytester.runpytest_subprocess("--testscope-record", "-p", "no:cacheprovider")
    assert r.ret == 0, r.stdout.str()
    edit(pytester, "m.py", "return 1", "return 2")
    pytester.runpytest_subprocess("--testscope", "-p", "no:cacheprovider")  # test_f fails
    edit(pytester, "m.py", "return 2", "return 1")
    r = pytester.runpytest_subprocess("--testscope", "-p", "no:cacheprovider")  # reads lastfailed
    r.assert_outcomes(passed=1)
    assert r.ret == 0, r.stdout.str()
