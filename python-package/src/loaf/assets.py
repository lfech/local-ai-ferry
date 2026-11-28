"""Locate deployment data, describe the installation and link the Docker project."""

import importlib.metadata
import os
from pathlib import Path
import re
import sys


DATA_DIR = Path(__file__).resolve().parent / "data"
CHECKOUT_DATA = Path("python-package/src/loaf/data")
# Only the Debian package build writes this marker into its deployment data.
DEBIAN_PACKAGE_MARKER = DATA_DIR / "debian-package"
# The release wheel and the Debian package carry the standalone executable that backups include.
STANDALONE_EXECUTABLE = DATA_DIR / "standalone/loaf"
SYSTEM_PROJECT_DIR = Path("/var/lib/loaf")
DISTRIBUTION = "local-ai-ferry"


def debian_package() -> bool:
    return DEBIAN_PACKAGE_MARKER.is_file()


def system_deployment_installed() -> bool:
    """Whether the Debian package prepared the system deployment on this host, whichever loaf runs."""
    return SYSTEM_PROJECT_DIR.is_dir()


def source_checkout() -> Path | None:
    """Return the checkout when this launcher runs from its source tree."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / CHECKOUT_DATA).resolve() == DATA_DIR else None


def standalone_executable() -> Path | None:
    executable = os.environ.get("LOAF_RELEASE_EXECUTABLE")
    runtime = Path(sys.base_prefix).parent
    return Path(executable) if executable and (runtime / ".loaf-release").is_file() else None


def release_version() -> str:
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        checkout = source_checkout()
        metadata = checkout / "python-package/pyproject.toml" if checkout else None
        if metadata and (match := re.search(r'(?m)^version = "([^"]+)"$', metadata.read_text(encoding="utf-8"))):
            return match[1]
        return "unknown"


def installation() -> str:
    if debian_package():
        return "Debian package"
    if (executable := standalone_executable()) is not None:
        return f"standalone executable {executable}"
    if (checkout := source_checkout()) is not None:
        return f"source checkout {checkout}"
    return "Python package"


