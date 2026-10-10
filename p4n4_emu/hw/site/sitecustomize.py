"""On PYTHONPATH under `p4n4-emu run`: replaces the hardware libraries with p4n4-emu's stubs."""
import importlib
import os
import sys
import traceback

_here = os.path.dirname(os.path.abspath(__file__))
try:
    import p4n4_emu  # noqa: F401
except ImportError:
    sys.path.append(os.environ["P4N4_EMU_PATH"])
try:
    from p4n4_emu.hw import shims

    shims.install_from_env()
except Exception:
    # Running on without the stubs would reach for real hardware: stop instead
    traceback.print_exc()
    print("p4n4-emu run: could not install the hardware stubs", file=sys.stderr)
    os._exit(70)

sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
_self = sys.modules.pop("sitecustomize")
try:
    importlib.import_module("sitecustomize")
except ImportError:
    sys.modules["sitecustomize"] = _self
