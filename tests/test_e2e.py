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
    r.assert_outcomes(passed=6)
    return pytester


def selected(p):
    r = p.runpytest_subprocess("--testscope", "-p", "no:cacheprovider")
    return set(json.loads((p.path / ".testscope/last-selection.json").read_text())["selected"])


def edit(p, path, old, new):
    f = p.path / path
    s = f.read_text()
    assert old in s
    f.write_text(s.replace(old, new))


def ids(*names):
    return {f"tests/test_x.py::{n}" for n in names}


def test_nothing_changed(proj):
    assert selected(proj) == set()


def test_function_body(proj):
    edit(proj, "pkg/a.py", "return 1", "return 1  # same\n    pass")
    assert selected(proj) == ids("test_f")


def test_function_reached_only_in_subprocess(proj):
    edit(proj, "pkg/a.py", "return 2", "return 2 or 0")
    assert selected(proj) == ids("test_g", "test_sub")


def test_data_file(proj):
    edit(proj, "pkg/data.yaml", "k: v", "k: w")
    assert selected(proj) == ids("test_data")


def test_module_level_constant(proj):
    edit(proj, "pkg/a.py", "CONST = 3", "CONST = 4")
    assert ids("test_const") <= selected(proj)


def test_config_change_runs_everything(proj):
    edit(proj, "pyproject.toml", "pythonpath", "# x\npythonpath")
    assert selected(proj) == ids("test_f", "test_g", "test_data", "test_const", "test_sub", "test_none")


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
