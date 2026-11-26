# tests/__init__.py - Package marker for standard-library unittest discovery.
# Test groups and opt-in environment flags are documented in docs/verification.md.
"""Unit and explicitly enabled Docker integration tests."""

import os
from pathlib import Path

# Keep a host's /etc/loaf out of the fixtures. Layering tests set their own directory.
os.environ["LOAF_SYSTEM_CONFIG_DIR"] = ""

# --- Fixture paths belong to the checkout, independently of the CLI's cwd ---
TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent.parent
DATA = ROOT / "python-package/src/loaf/data"
