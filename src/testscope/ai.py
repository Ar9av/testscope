"""No map yet: an LLM reads the diff and a test index and names the test files to run.
Once a map exists it is not used (measured: it found nothing the map missed).

Backends (TESTSCOPE_AI):
  anthropic  Anthropic API, needs ANTHROPIC_API_KEY (pip install 'testscope[ai]')
  claude     the Claude Code CLI (`claude -p`), uses the user's Claude Code login
"""
import ast, json, os, shutil, subprocess, tempfile, time

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
    model = model or os.environ.get("TESTSCOPE_AI_MODEL") or DEFAULT_MODEL
    effort = effort or os.environ.get("TESTSCOPE_AI_EFFORT", "low")
    index = index if index is not None else test_index(root)
    d = diff(root)
    note = ""
    if len(d) > MAX_DIFF:  # ponytail: hard cap; the file list below stays complete
        d, note = d[:MAX_DIFF], f"\n[diff truncated to {MAX_DIFF} chars; the changed-file list is complete]"
    known = {line.split(":", 1)[0] for line in index.splitlines()}
    change = f"Changed files:\n" + "\n".join(changed) + f"\n\nDiff:\n{d}{note}"
    t = time.time()
    if backend == "claude":
        add, info = _claude(index, change, model, effort)
    else:
        add, info = _anthropic(index, change, model, effort)
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


def _claude(index, change, model, effort):
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not found on PATH")
    # Minimal context: no tools, MCP, skills, settings, hooks or project files. A plain
    # `claude -p` loads the whole agent context (hundreds of thousands of tokens per call).
    cmd = [exe, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
           "--disable-slash-commands", "--setting-sources", "", "--no-session-persistence",
           "--system-prompt", SYSTEM, "--json-schema", json.dumps(SCHEMA), "--effort", effort]
    if model:
        cmd += ["--model", model]
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "TESTSCOPE", "TESTSCOPE_AI")}
    # An empty directory, so no CLAUDE.md is found. Its path must stay the same between calls:
    # the CLI puts the working directory in the prompt, and a new path breaks the prompt cache.
    cwd = os.path.join(tempfile.gettempdir(), "testscope-claude")
    os.makedirs(cwd, exist_ok=True)
    r = subprocess.run(cmd, input=f"Test index (path: repo modules it imports):\n{index}\n\n{change}",
                       cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise RuntimeError(f"claude -p failed: {r.stderr.strip()[:300] or r.stdout.strip()[:300]}")
    d = json.loads(r.stdout)
    u = d.get("usage", {})
    info = {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
            "cache_write": u.get("cache_creation_input_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
            "usd": d.get("total_cost_usd"), "usd_note": "list price; billed to your Claude plan when logged in with claude.ai"}
    return (d.get("structured_output") or {}).get("add", []), info
