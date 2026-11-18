from testscope.fingerprint import py_fingerprint as fp

BASE = "class C:\n    x = 1\n    @dec\n    def m(self):\n        return 1  # c\n\ndef f():\n    return 2\n"


def test_moves_and_comments_are_not_changes():
    moved = "# moved\n\n\ndef f():\n    return 2\n\nclass C:\n    x = 1\n    @dec\n    def m(self):\n        # other\n        return 1\n"
    assert fp(moved)["funcs"] == fp(BASE)["funcs"]


def test_class_attribute_is_keyed_by_name():
    a, b = fp(BASE), fp(BASE.replace("x = 1", "x = 2"))
    changed = {k for k in a["stmts"] if a["stmts"][k] != b["stmts"].get(k)}
    assert changed == {"n:C.x"} and a["funcs"] == b["funcs"]


def test_decorator_is_part_of_the_function():
    a, b = fp(BASE), fp(BASE.replace("@dec", "@dec2"))
    assert a["funcs"]["C.m"] != b["funcs"]["C.m"] and a["funcs"]["f"] == b["funcs"]["f"]


def test_hash_has_no_version_specific_ast_dump():
    # Tokens are the same on every supported Python; a CI map from 3.12 must match 3.14.
    assert fp("def f():\n    return 2\n")["funcs"]["f"] == fp("def f():\n\n    return 2   # x\n")["funcs"]["f"]


def test_worktree_falls_back_to_main_map(tmp_path):
    import json, subprocess
    from testscope.selection import MAP_VERSION, load_map
    main, wt = tmp_path / "main", tmp_path / "wt"
    main.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=main, check=True, capture_output=True)
    git("init", "-q"); git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    git("worktree", "add", "-q", str(wt))
    assert load_map(str(wt)) is None
    (main / ".testscope").mkdir()
    (main / ".testscope" / "map.json").write_text(json.dumps({"version": MAP_VERSION, "from": "main"}))
    assert load_map(str(wt))["from"] == "main"
    (wt / ".testscope").mkdir()
    (wt / ".testscope" / "map.json").write_text(json.dumps({"version": MAP_VERSION, "from": "wt"}))
    assert load_map(str(wt))["from"] == "wt"
