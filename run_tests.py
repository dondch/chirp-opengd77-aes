#!/usr/bin/env python3
"""Dependency-free test runner (no pytest required).

Sets up sys.path the same way conftest.py does, then runs every ``test_*``
function in the ``tests`` package.  ``pytest`` also works if you have it.

    python run_tests.py
    CHIRP_SRC=/path/to/chirp python run_tests.py
"""
import importlib
import os
import pkgutil
import sys
import traceback

# CHIRP's logger redirects stdout/stderr to a debug.log when stdin is not a TTY
# (e.g. under a test runner) unless this is set.  Must precede any chirp import.
os.environ.setdefault("CHIRP_TESTENV", "1")

HERE = os.path.dirname(os.path.abspath(__file__))

# CHIRP checkout first, then HERE on top, so this repo's ``tests`` and
# ``opengd77_aes`` win over any same-named packages inside the CHIRP checkout.
_chirp_src = os.environ.get("CHIRP_SRC") or os.path.join(HERE, "..", "chirp")
_chirp_src = os.path.abspath(_chirp_src)
if os.path.isdir(os.path.join(_chirp_src, "chirp")):
    sys.path.insert(0, _chirp_src)
sys.path.insert(0, HERE)

try:
    import chirp.chirp_common  # noqa: F401
except Exception as e:  # pragma: no cover
    sys.exit("Cannot import chirp (set CHIRP_SRC to a CHIRP checkout): %s" % e)


def main():
    import tests as tests_pkg
    passed = failed = 0
    failures = []
    for mod in pkgutil.iter_modules(tests_pkg.__path__):
        if not mod.name.startswith("test_"):
            continue
        module = importlib.import_module("tests.%s" % mod.name)
        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            fn = getattr(module, name)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
                print("PASS %s.%s" % (mod.name, name))
            except Exception:
                failed += 1
                failures.append((mod.name, name, traceback.format_exc()))
                print("FAIL %s.%s" % (mod.name, name))
    print("\n%d passed, %d failed" % (passed, failed))
    for modname, name, tb in failures:
        print("\n=== %s.%s ===\n%s" % (modname, name, tb))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
