"""Build the Debian package and install it in clean Debian 12 and 13 containers."""

import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from pathlib import Path

from tests import ROOT, TESTS_DIR
from tests.docker_support import DOCKER_TESTS, command


@unittest.skipUnless(DOCKER_TESTS and shutil.which("dpkg-deb"), "Set RUN_DOCKER_TESTS=1 on a host with dpkg-deb.")
class DebianPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = TemporaryDirectory(prefix="loaf-deb-")
        cls.addClassCleanup(folder.cleanup)
        cls.output = Path(folder.name)
        pip = [sys.executable, "-m", "pip", "wheel", "--quiet", "--no-deps", "--wheel-dir", str(cls.output)]
        cache = ROOT / "install-cache/wheels"
        if cache.is_dir():
            pip += ["--find-links", str(cache)]
        command([*pip, str(ROOT / "python-package")], timeout=300)
        wheel, = cls.output.glob("*.whl")
        command(["bash", str(ROOT / "scripts/build-deb.sh"), str(wheel), str(cls.output)], timeout=300)
        package, = cls.output.glob("loaf_*_all.deb")
        shutil.copyfile(package, cls.output / "loaf.deb")
        cls.package = package

    def test_control_fields_conffiles_and_files(self):
        control = command(["dpkg-deb", "--field", str(self.package)])
        self.assertIn("Architecture: all\n", control)
        self.assertIn("Depends: python3 (>= 3.10), docker-ce-cli | docker-cli | docker.io,", control)
        files = command(["dpkg-deb", "--contents", str(self.package)])
        for path in ("./usr/bin/loaf", "./etc/loaf/config.ini", "./usr/share/man/man1/loaf.1.gz",
                     "./usr/lib/systemd/system/loaf.service", "./usr/share/doc/loaf/copyright",
                     "./usr/lib/python3/dist-packages/loaf/data/debian-package",
                     "./usr/lib/python3/dist-packages/loaf/data/docs/installation.md"):
            self.assertIn(path, files)
        self.assertNotIn("__pycache__", files)
        self.assertNotIn("-record.md", files)
        conffiles = subprocess.run(
            ["dpkg-deb", "--info", str(self.package), "conffiles"], capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(conffiles, "/etc/loaf/config.ini\n")

    def test_package_installs_runs_and_purges_on_debian_12_and_13(self):
        for image in ("debian:12-slim", "debian:13-slim"):
            with self.subTest(image=image):
                output = command([
                    "docker", "run", "--rm", "--pull", "missing",
                    "--mount", f"type=bind,source={self.output},target=/deb,readonly",
                    "--mount", f"type=bind,source={TESTS_DIR / 'debian_package_checks.sh'},target=/checks.sh,readonly",
                    image, "sh", "/checks.sh",
                ], timeout=900)
                self.assertIn("Debian package checks passed", output)
