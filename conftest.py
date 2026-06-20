"""pytest path setup.

Makes ``import opengd77_aes`` work (repo root on the path) and locates a CHIRP
source checkout so ``import chirp`` resolves.  Override the checkout location
with the CHIRP_SRC environment variable; otherwise a sibling ``chirp/`` dir is
assumed (i.e. ``<parent>/chirp`` containing the ``chirp`` package).
"""
import os
import sys

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

# Neutralize the driver's inter-chunk USB delays for the fake-radio tests.
try:
    import opengd77_aes as _drv
    _drv.time.sleep = lambda *a, **k: None
except Exception:
    pass
