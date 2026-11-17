"""Loaded in child Python processes started by a recorded test (via PYTHONPATH).

Records the child's deps and writes them to $TESTSCOPE_OUT at exit, then chains
to any real sitecustomize this one shadows.
"""
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))


def _start():
    out, root = os.environ.get("TESTSCOPE_OUT"), os.environ.get("TESTSCOPE_ROOT")
    if not out or not root or sys.version_info < (3, 12):
        return
    import atexit, json
    try:
        from testscope.recorder import Recorder
    except ImportError:  # child runs an interpreter without testscope installed
        return

    rec = Recorder(root)
    try:
        rec.install()
    except Exception:
        return
    rec.start()

    def flush():
        if not rec.active:
            return
        funcs, files, dirs = rec.stop()
        try:
            with open(os.path.join(out, f"{os.getpid()}-{id(rec)}.json"), "w") as fh:
                json.dump({"funcs": sorted(funcs), "files": sorted(files), "dirs": sorted(dirs)}, fh)
        except OSError:
            pass

    atexit.register(flush)
    real_exit = os._exit

    def _exit(code):  # os._exit skips atexit
        flush()
        real_exit(code)

    os._exit = _exit


_start()

# Chain to the sitecustomize we shadow, if any.
_mod = sys.modules.pop("sitecustomize", None)
_saved = list(sys.path)
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
try:
    import sitecustomize  # noqa: F401
except ImportError:
    if _mod is not None:
        sys.modules["sitecustomize"] = _mod
finally:
    sys.path[:] = _saved
