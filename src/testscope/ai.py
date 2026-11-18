"""No map yet: an LLM reads the diff and a test index and names the test files to run.
Once a map exists it is not used (measured: it found nothing the map missed).

Backends (TESTSCOPE_AI):
  anthropic  Anthropic API, needs ANTHROPIC_API_KEY (pip install 'testscope[ai]')
  claude     Claude Code CLI (`claude -p`), the user's Claude Code login
  codex      Codex CLI (`codex exec`), the user's Codex login (ChatGPT plan) or API key
  opencode   opencode (`opencode run`), whatever provider opencode is set up with
  cursor     Cursor CLI (`cursor-agent -p`), the user's Cursor login
  gemini     Gemini CLI (`gemini -p`), the user's Google login or GEMINI_API_KEY
  qwen       Qwen Code (`qwen`), whatever auth Qwen Code is set up with
  copilot    GitHub Copilot CLI (`copilot -p`), the user's Copilot login
Each CLI runs read-only in an empty directory. TESTSCOPE_AI_MODEL picks the model.
"""
import ast, json, os, re, shutil, subprocess, tempfile, time

from .fingerprint import file_imports, module_index, repo_files

DEFAULT_MODEL = "claude-opus-5-5"
MAX_DIFF = 60_000  # chars
# $ per 1M tokens: input, output, cache write (5m), cache read
PRICES = {"claude-opus-5-5": (4, 20, 5, 0.20), "claude-sonnet-5-5": (2, 10, 2.5, 0.10),
          "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01)}
SYSTEM = ("You pick pytest test files for a code change. Name the test files that this change "
          "could plausibly make fail: tests that exercise the changed behavior, read a changed "
          "data file, or import a changed module. Return only paths from the test index. Return "
          "an empty list when no test can be affected. Every extra file costs run time, so do "
          "not list files that only share a topic with the change.")
SCHEMA = {"type": "object", "properties": {"add": {"type": "array", "items": {"type": "string"}}},
          "required": ["add"], "additionalProperties": False}


def test_index(root):
    """One line per test file: path and the repo modules it imports. Stable, so it caches."""
    files = repo_files(root)
    mods = module_index(dict.fromkeys(f for f in files if f.endswith(".py")))
    out = []
    for f in sorted(files):
        if not (os.path.basename(f).startswith("test_") and f.endswith(".py")):
            continue
        try:
            with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
            deps = sorted(d[:-3].replace("/", ".") for d in file_imports(f, tree, mods))
        except (SyntaxError, OSError):
            deps = []
        out.append(f"{f}: {', '.join(deps)}" if deps else f)
    return "\n".join(out)


def diff(root):
    return subprocess.run(["git", "diff", "--no-color", "--no-ext-diff", "HEAD"], cwd=root,
                          capture_output=True, text=True).stdout


def suggest(root, changed, backend=None, model=None, effort=None, index=None):
    """Return (set of extra test files, info dict). Raises on backend errors."""
    backend = backend or os.environ.get("TESTSCOPE_AI", "anthropic")
    # The Anthropic backends default to Opus 5.5; other CLIs use their own default model.
    model = model or os.environ.get("TESTSCOPE_AI_MODEL") or (DEFAULT_MODEL if backend in ("anthropic", "claude") else None)
    effort = effort or os.environ.get("TESTSCOPE_AI_EFFORT", "low")
    index = index if index is not None else test_index(root)
    d = diff(root)
    note = ""
    if len(d) > MAX_DIFF:  # ponytail: hard cap; the file list below stays complete
        d, note = d[:MAX_DIFF], f"\n[diff truncated to {MAX_DIFF} chars; the changed-file list is complete]"
    known = {line.split(":", 1)[0] for line in index.splitlines()}
    change = f"Changed files:\n" + "\n".join(changed) + f"\n\nDiff:\n{d}{note}"
    t = time.time()
    if backend not in BACKENDS:
        raise RuntimeError(f"unknown TESTSCOPE_AI backend {backend!r}; use one of {', '.join(BACKENDS)}")
    add, info = BACKENDS[backend](index, change, model, effort)
    info.update(backend=backend, model=model, effort=effort, seconds=round(time.time() - t, 1))
    return {f for f in add if f in known}, info


def _anthropic(index, change, model, effort):
    import anthropic  # optional dependency: pip install 'testscope[ai]'
    client = anthropic.Anthropic()
    # Server-side refusal fallback where the model has one (not Haiku).
    fb = {} if "haiku" in model else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    r = client.beta.messages.create(
        model=model, max_tokens=16000, **fb,
        system=SYSTEM, output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "text", "text": f"Test index (path: repo modules it imports):\n{index}",
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": change}]}])
    u = r.usage
    info = {"input": u.input_tokens, "output": u.output_tokens,
            "cache_write": u.cache_creation_input_tokens or 0, "cache_read": u.cache_read_input_tokens or 0}
    p = PRICES.get(r.model or model)
    if p:
        info["usd"] = round((info["input"] * p[0] + info["output"] * p[1] + info["cache_write"] * p[2]
                             + info["cache_read"] * p[3]) / 1e6, 5)
    if r.stop_reason == "refusal":
        return [], dict(info, refusal=True)
    text = next(b.text for b in r.content if b.type == "text")
    return json.loads(text)["add"], info


def _run(name, cmd, stdin=None):
    """Run an agent CLI in an empty directory, so it finds no project files or rules. The path
    stays the same between calls: CLIs put the working directory in the prompt, and a new path
    breaks the prompt cache."""
    if not shutil.which(cmd[0]):
        raise RuntimeError(f"{cmd[0]} CLI not found on PATH")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "TESTSCOPE", "TESTSCOPE_AI")}
    cwd = os.path.join(tempfile.gettempdir(), "testscope-agent")
    os.makedirs(cwd, exist_ok=True)
    r = subprocess.run(cmd, input=stdin, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise RuntimeError(f"{name} failed: {r.stderr.strip()[-300:] or r.stdout.strip()[-300:]}")
    return r.stdout, cwd


def _prompt(index, change):
    """For CLIs with no system-prompt or schema flag: everything in one user message."""
    return (f"{SYSTEM}\nDo not use tools. Reply with only a JSON object: {{\"add\": [test file paths]}}.\n\n"
            f"Test index (path: repo modules it imports):\n{index}\n\n{change}")


def _add(text):
    """The "add" list from the last JSON object in a model's text reply."""
    for m in reversed(list(re.finditer(r'\{[^{}]*"add"\s*:\s*\[[^\]]*\][^{}]*\}', text or ""))):
        try:
            return json.loads(m.group())["add"]
        except (ValueError, KeyError):
            continue
    raise RuntimeError(f"no {{\"add\": [...]}} in reply: {(text or '')[:200]!r}")


def _claude(index, change, model, effort):
    # Minimal context: no tools, MCP, skills, settings, hooks or project files. A plain
    # `claude -p` loads the whole agent context (hundreds of thousands of tokens per call).
    cmd = ["claude", "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
           "--disable-slash-commands", "--setting-sources", "", "--no-session-persistence",
           "--system-prompt", SYSTEM, "--json-schema", json.dumps(SCHEMA), "--effort", effort]
    if model:
        cmd += ["--model", model]
    out, _ = _run("claude -p", cmd, f"Test index (path: repo modules it imports):\n{index}\n\n{change}")
    d = json.loads(out)
    u = d.get("usage", {})
    info = {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
            "cache_write": u.get("cache_creation_input_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
            "usd": d.get("total_cost_usd"), "usd_note": "list price; billed to your Claude plan when logged in with claude.ai"}
    return (d.get("structured_output") or {}).get("add", []), info


def _codex(index, change, model, effort):
    # Read-only sandbox, no session file, no user config or AGENTS.md rules. Uses the Codex
    # login (ChatGPT plan) or OPENAI_API_KEY, whichever `codex login` set up.
    with tempfile.TemporaryDirectory() as d:
        schema, last = os.path.join(d, "schema.json"), os.path.join(d, "last.txt")
        with open(schema, "w") as fh:
            json.dump(SCHEMA, fh)
        cmd = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
               "-s", "read-only", "--json", "--output-schema", schema, "-o", last,
               "-c", f"model_reasoning_effort={effort}"]
        if model:
            cmd += ["-m", model]
        out, _ = _run("codex exec", cmd + ["-"], _prompt(index, change))
        with open(last) as fh:
            text = fh.read()
    u = {}
    for line in out.splitlines():
        if '"turn.completed"' in line:
            u = json.loads(line).get("usage", {})
    info = {"input": u.get("input_tokens", 0) - u.get("cached_input_tokens", 0), "output": u.get("output_tokens", 0),
            "cache_write": 0, "cache_read": u.get("cached_input_tokens", 0)}
    return json.loads(text)["add"], info


def _opencode(index, change, model, effort):
    # The read-only "plan" agent, no plugins. The model is whatever opencode is set up with
    # (TESTSCOPE_AI_MODEL=provider/model, e.g. github-copilot/... for a Copilot plan).
    cmd = ["opencode", "run", "--format", "json", "--pure", "--agent", "plan"]
    if model:
        cmd += ["-m", model]
    out, _ = _run("opencode run", cmd + [_prompt(index, change)])
    text, info = "", {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "usd": 0}
    for line in out.splitlines():
        ev = json.loads(line) if line.startswith("{") else {}
        part = ev.get("part", {})
        if ev.get("type") == "text":
            text += part.get("text", "")
        elif ev.get("type") == "step_finish":
            t = part.get("tokens", {})
            info["input"] += t.get("input", 0)
            info["output"] += t.get("output", 0)
            info["cache_read"] += t.get("cache", {}).get("read", 0)
            info["cache_write"] += t.get("cache", {}).get("write", 0)
            info["usd"] += part.get("cost") or 0
    return _add(text), info


def _cursor(index, change, model, effort):
    # "ask" mode is read-only. Uses the Cursor login (`cursor-agent login`) or CURSOR_API_KEY.
    cmd = ["cursor-agent", "-p", "--output-format", "json", "--mode", "ask", "--trust"]
    if model:
        cmd += ["--model", model]
    out, _ = _run("cursor-agent -p", cmd + [_prompt(index, change)])
    return _add(json.loads(out).get("result")), {}


def _gemini(index, change, model, effort):
    # "plan" approval mode is read-only. Uses the Google login or GEMINI_API_KEY.
    cmd = ["gemini", "-o", "json", "--approval-mode", "plan", "-p", "Follow the instructions above."]
    if model:
        cmd += ["-m", model]
    out, _ = _run("gemini -p", cmd, _prompt(index, change))
    return _add(json.loads(out).get("response")), {}


def _qwen(index, change, model, effort):
    # "plan" approval mode is read-only; --json-schema makes the reply structured.
    cmd = ["qwen", "-o", "json", "--approval-mode", "plan", "--json-schema", json.dumps(SCHEMA)]
    if model:
        cmd += ["-m", model]
    out, _ = _run("qwen", cmd, _prompt(index, change))
    d = json.loads(out)
    r = next((e for e in reversed(d) if e.get("type") == "result"), {}) if isinstance(d, list) else d
    if r.get("is_error"):
        raise RuntimeError(f"qwen failed: {json.dumps(r.get('error'))[:300]}")
    u = r.get("usage", {})
    info = {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0), "cache_write": 0, "cache_read": 0}
    so = r.get("structured_output")
    return (so["add"] if isinstance(so, dict) and "add" in so else _add(r.get("result"))), info


def _copilot(index, change, model, effort):
    # Non-interactive: tools that need approval are denied. Uses `copilot login` or a GitHub token.
    cmd = ["copilot", "-s", "--no-ask-user", "-p", _prompt(index, change)]
    if model:
        cmd += ["--model", model]
    out, _ = _run("copilot -p", cmd)
    return _add(out), {}


BACKENDS = {"anthropic": _anthropic, "claude": _claude, "codex": _codex, "opencode": _opencode,
            "cursor": _cursor, "gemini": _gemini, "qwen": _qwen, "copilot": _copilot}
