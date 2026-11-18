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


def test_ai_adds_files_and_caches(proj, monkeypatch, tmp_path_factory):
    # Fake `claude` CLI: logs each call, always suggests test_y.py plus a path that is not a test.
    bin_ = proj.path / "fakebin"
    bin_.mkdir()
    exe = bin_ / "claude"
    exe.write_text("#!/bin/sh\ncat > /dev/null\necho x >> \"$TS_CALLS\"\n"
                   "echo '{\"structured_output\": {\"add\": [\"tests/test_y.py\", \"nope.py\"]},"
                   " \"usage\": {\"input_tokens\": 5, \"output_tokens\": 2}, \"total_cost_usd\": 0.001}'\n")
    exe.chmod(0o755)
    calls = tmp_path_factory.mktemp("calls") / "calls.txt"  # outside the project: not a change
    monkeypatch.setenv("PATH", f"{bin_}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("TS_CALLS", str(calls))
    monkeypatch.setenv("TESTSCOPE_AI", "claude")
    edit(proj, "pkg/a.py", "return 1", "return 1  # same\n    pass")
    assert selected(proj) == ids("test_f", "tests/test_y.py::test_h", "tests/test_y.py::test_fx")
    raw = json.loads((proj.path / ".testscope/last-selection.json").read_text())
    assert raw["ai"]["add"] == ["tests/test_y.py"]
    selected(proj)  # same change again: served from the cache
    assert calls.read_text().count("x") == 1


def test_ai_failure_never_fails_the_run(proj, monkeypatch):
    bin_ = proj.path / "fakebin"
    bin_.mkdir()
    (bin_ / "claude").write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
    (bin_ / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("TESTSCOPE_AI", "claude")
    edit(proj, "pkg/a.py", "return 1", "return 1  # same\n    pass")
    r = proj.runpytest_subprocess("--testscope", "-p", "no:cacheprovider")
    r.assert_outcomes(passed=1)
    assert "ai: skipped" in r.stdout.str()


def test_ai_without_map_picks_from_diff(pytester, monkeypatch, tmp_path_factory):
    import subprocess
    for path, src in FILES.items():
        p = pytester.path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src))
    bin_ = tmp_path_factory.mktemp("bin")
    (bin_ / "claude").write_text("#!/bin/sh\ncat > /dev/null\n"
                                 "echo '{\"structured_output\": {\"add\": [\"tests/test_y.py\"]}, \"usage\": {}}'\n")
    (bin_ / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("TESTSCOPE_AI", "claude")
    git = lambda *a: subprocess.run(["git", *a], cwd=pytester.path, check=True, capture_output=True)
    git("init", "-q"); git("add", "."); git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    edit(pytester, "pkg/b.py", "* 2", "* 2 + 0")
    r = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    r.assert_outcomes(passed=2)  # only tests/test_y.py
    assert "picked by AI" in r.stdout.str()
