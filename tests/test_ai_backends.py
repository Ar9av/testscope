"""Each agent-CLI backend against a fake CLI that prints that CLI's documented output shape."""
import json, os, subprocess

import pytest

from testscope import ai

ADD = '{"add": ["tests/test_y.py", "nope.py"]}'
FAKES = {
    "claude": ("claude", f"cat >/dev/null; echo '{json.dumps({'structured_output': json.loads(ADD), 'usage': {}})}'"),
    # codex writes the last message to the -o file and events to stdout
    "codex": ("codex", 'cat >/dev/null; while [ "$1" ]; do [ "$1" = -o ] && o=$2; shift; done; '
                       f"echo '{ADD}' > \"$o\"; "
                       """echo '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":4,"output_tokens":2}}'"""),
    "opencode": ("opencode", "echo '" + json.dumps({"type": "text", "part": {"text": "Sure: " + ADD}}) + "'; "
                             "echo '" + json.dumps({"type": "step_finish", "part": {"tokens": {"input": 7, "output": 1}, "cost": 0}}) + "'"),
    "cursor": ("cursor-agent", "printf '%s\\n' '" + json.dumps({"type": "result", "result": "```json\n" + ADD + "\n```"}) + "'"),
    "gemini": ("gemini", "cat >/dev/null; echo '" + json.dumps({"response": ADD, "stats": {}}) + "'"),
    "qwen": ("qwen", "cat >/dev/null; echo '" + json.dumps([{"type": "system"}, {"type": "result", "result": "",
             "structured_output": json.loads(ADD), "usage": {"input_tokens": 5, "output_tokens": 1}}]) + "'"),
    "copilot": ("copilot", f"echo 'Here you go. {ADD}'"),
}


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir(parents=True)
    (tmp_path / "pkg/a.py").write_text("X = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_y.py").write_text("from pkg import a\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.mark.parametrize("backend", sorted(FAKES))
def test_backend_parses_its_cli_output(backend, tmp_path, monkeypatch):
    exe, body = FAKES[backend]
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / exe).write_text(f"#!/bin/sh\n{body}\n")
    (bin_ / exe).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    add, info = ai.suggest(str(_repo(tmp_path / "r")), ["pkg/a.py"], backend=backend)
    assert add == {"tests/test_y.py"}  # paths not in the test index are dropped
    assert info["backend"] == backend


def test_backend_errors_are_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # git, but no agent CLIs
    with pytest.raises(RuntimeError, match="codex CLI not found"):
        ai.suggest(str(_repo(tmp_path / "r")), ["pkg/a.py"], backend="codex", index="tests/test_y.py")
    with pytest.raises(RuntimeError, match="unknown TESTSCOPE_AI backend"):
        ai.suggest(str(tmp_path / "r"), ["pkg/a.py"], backend="nope", index="tests/test_y.py")
    with pytest.raises(RuntimeError, match="no .*add"):
        ai._add("I could not decide.")
