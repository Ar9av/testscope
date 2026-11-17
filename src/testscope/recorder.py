"""Records what a test touches: Python functions it runs, repo files it opens/lists/execs.

Function deps use sys.monitoring PY_START and return DISABLE, so each code object
costs one callback per test (restart_events re-arms them), not one per line.
Imported by the pytest plugin and, in child Python processes, by _boot/sitecustomize.
"""
from __future__ import annotations

import os
import sys

TOOL_ID = 4  # 0-2 and 5 are named (debugger, coverage, profiler, optimizer)
_SKIP_PARTS = (os.sep + ".git" + os.sep, os.sep + ".testscope" + os.sep, os.sep + "__pycache__" + os.sep,
               os.sep + "site-packages" + os.sep, os.sep + ".venv" + os.sep, os.sep + "venv" + os.sep)


class Recorder:
    def __init__(self, root: str):
        self.root = os.path.realpath(root) + os.sep
        self.funcs: set[tuple[str, str]] = set()  # (relpath, qualname)
        self.files: set[str] = set()              # data files read or executed
        self.dirs: set[str] = set()               # directories listed
        self.active = False
        self._rel: dict[str, str | None] = {}

    def rel(self, path) -> str | None:
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not isinstance(path, str):
            return None
        r = self._rel.get(path, "")
        if r != "":
            return r
        full = os.path.realpath(path)
        r = None
        if full.startswith(self.root) and not any(p in full + os.sep for p in _SKIP_PARTS):
            r = full[len(self.root):]
        self._rel[path] = r
        return r

    # -- sys.monitoring -------------------------------------------------
    def _on_start(self, code, offset):
        if self.active:
            r = self.rel(code.co_filename)
            if r is not None and r.endswith(".py"):
                self.funcs.add((r, code.co_qualname))
            return sys.monitoring.DISABLE
        # Not recording right now: keep the event armed so the next test sees it.
        return None

    # -- audit events ---------------------------------------------------
    def _on_audit(self, event, args):
        if not self.active:
            return
        if event == "open":
            r = self.rel(args[0])
            if r is not None and not r.endswith((".py", ".pyc")):
                self.files.add(r)
        elif event in ("os.listdir", "os.scandir"):
            r = self.rel(args[0] if args and args[0] is not None else ".")
            if r is not None:
                self.dirs.add(r.rstrip(os.sep))
        elif event == "subprocess.Popen":
            argv = args[1]
            if isinstance(argv, (str, bytes)):
                argv = [argv]
            for a in argv or ():
                r = self.rel(a)
                if r is not None and os.path.isfile(self.root + r):
                    self.files.add(r)

    def install(self):
        mon = sys.monitoring
        mon.use_tool_id(TOOL_ID, "testscope")
        mon.register_callback(TOOL_ID, mon.events.PY_START, self._on_start)
        mon.set_events(TOOL_ID, mon.events.PY_START)
        sys.addaudithook(self._on_audit)  # cannot be removed; gated by self.active

    def start(self):
        self.funcs, self.files, self.dirs = set(), set(), set()
        sys.monitoring.restart_events()
        self.active = True

    def stop(self):
        self.active = False
        return self.funcs, self.files, self.dirs
