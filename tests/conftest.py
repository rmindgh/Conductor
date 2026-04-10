"""
Test harness for remote-control.py.

The module file name contains a hyphen (remote-control.py), which Python can't
import directly. This conftest loads it via importlib so tests can `from
remote_control import ...`.
"""

import importlib.util
import sys
from pathlib import Path

_CONDUCTOR_DIR = Path(__file__).parent.parent
_RC_PATH = _CONDUCTOR_DIR / "remote-control.py"

# Load remote-control.py as the module "remote_control"
spec = importlib.util.spec_from_file_location("remote_control", _RC_PATH)
remote_control = importlib.util.module_from_spec(spec)
sys.modules["remote_control"] = remote_control
spec.loader.exec_module(remote_control)
