from testscope.fingerprint import py_fingerprint as fp

BASE = "class C:\n    x = 1\n    @dec\n    def m(self):\n        return 1  # c\n\ndef f():\n    return 2\n"


def test_moves_and_comments_are_not_changes():
    moved = "# moved\n\n\ndef f():\n    return 2\n\nclass C:\n    x = 1\n    @dec\n    def m(self):\n        # other\n        return 1\n"
    assert fp(moved)["funcs"] == fp(BASE)["funcs"]


def test_class_attribute_is_module_level():
    a, b = fp(BASE), fp(BASE.replace("x = 1", "x = 2"))
    assert a["module"] != b["module"] and a["funcs"] == b["funcs"]


def test_decorator_is_part_of_the_function():
    a, b = fp(BASE), fp(BASE.replace("@dec", "@dec2"))
    assert a["funcs"]["C.m"] != b["funcs"]["C.m"] and a["funcs"]["f"] == b["funcs"]["f"]


def test_hash_has_no_version_specific_ast_dump():
    # Tokens are the same on every supported Python; a CI map from 3.12 must match 3.14.
    assert fp("def f():\n    return 2\n")["funcs"]["f"] == fp("def f():\n\n    return 2   # x\n")["funcs"]["f"]
