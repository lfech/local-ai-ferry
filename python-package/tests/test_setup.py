"""Host setup probes and package selection without modifying the host."""

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from loaf import cli, setup
from tests import ROOT


class SetupTests(unittest.TestCase):
    def quick_setup_project(self, directory):
        project = Path(directory).resolve()
        (project / "compose.yml").touch()
        return project

    def test_setup_prepares_the_installation_under_one_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.quick_setup_project(directory)

            def install(path, *, with_docker_deps):
                self.assertEqual(path, project)
                self.assertFalse(with_docker_deps)
                with self.assertRaises(cli.AlreadyRunningError), cli.project_lock(project / ".run.lock"):
                    self.fail("setup released its checkout lock")

            with (
                patch("loaf.cli.subprocess.run") as run,
                patch.object(setup, "install", side_effect=install) as prepare,
                patch.object(cli, "Compose") as compose,
            ):
                self.assertEqual(cli.main(["--project-dir", directory, "setup"]), 0)
            prepare.assert_called_once_with(project, with_docker_deps=False)
            key = project / cli.config.TERMINAL_SECRET
            cli.config.read_terminal_secret(key)
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            run.assert_not_called()
            compose.assert_not_called()

    def test_key_only_uses_the_configured_path_and_preserves_existing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            config_dir = project / "settings"
            config_dir.mkdir()
            (config_dir / "config.ini").write_text("[terminal_server]\nsecret_file=private/terminal.key\n")
            key = project / "private/terminal.key"
            command = ["--project-dir", directory, "setup", "--config-dir", str(config_dir), "--terminal-server-key"]
            with (
                patch.object(setup, "install") as install,
                patch.object(setup, "setup_standalone") as standalone,
                redirect_stdout(StringIO()) as log,
            ):
                self.assertEqual(cli.main([*command, "--dry-run"]), 0)
                self.assertFalse(key.parent.exists())
                self.assertEqual(cli.main(command), 0)
                original = key.read_bytes()
                self.assertEqual(cli.main(command), 0)
            self.assertEqual(key.read_bytes(), original)
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(original.decode(), log.getvalue())
            self.assertFalse((project / "config").exists())
            install.assert_not_called()
            standalone.assert_not_called()

    def test_setup_dry_run_does_not_execute_or_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.quick_setup_project(directory)
            with patch.object(setup, "install") as install, patch("loaf.cli.subprocess.run") as run:
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(cli.main(["--project-dir", directory, "setup", "--dry-run"]), 0)
            install.assert_not_called()
            run.assert_not_called()
            self.assertIn("cache Python wheels", output.getvalue())
            self.assertFalse((project / ".run.lock").exists())

    def test_setup_preserves_installer_failure_and_links_to_guidance(self):
        with tempfile.TemporaryDirectory() as directory:
            self.quick_setup_project(directory)
            with (
                patch.object(setup, "install", side_effect=subprocess.CalledProcessError(7, ["apt-get"])),
                patch.object(cli, "Compose") as compose,
                redirect_stderr(StringIO()) as errors,
            ):
                self.assertEqual(cli.main(["--project-dir", directory, "setup"]), 7)
            compose.assert_not_called()
            self.assertIn("apt-get failed (exit 7)", errors.getvalue())
            self.assertIn("docs/debian.md", errors.getvalue())

    def test_install_dry_run_creates_no_environment_or_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "compose.yml").touch()
            with patch.object(setup, "install") as install, redirect_stdout(StringIO()) as log:
                self.assertEqual(cli.main(["--project-dir", directory, "install", "--dry-run"]), 0)
            self.assertIn(".venv", log.getvalue())
            self.assertEqual(list(project.iterdir()), [project / "compose.yml"])
        install.assert_not_called()

    def test_setup_tests_installs_only_the_test_packages_under_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()

            def install(path):
                self.assertEqual(path, project)
                with self.assertRaises(cli.AlreadyRunningError), cli.project_lock(project / ".run.lock"):
                    self.fail("setup tests released its checkout lock")

            with (
                patch.object(setup, "install_test_packages", side_effect=install) as packages,
                patch.object(setup, "install") as full_setup,
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(cli.main(["--project-dir", directory, "setup", "tests", "--dry-run"]), 0)
                self.assertFalse((project / ".run.lock").exists())
                packages.assert_not_called()
                self.assertEqual(cli.main(["--project-dir", directory, "install", "tests"]), 0)
            packages.assert_called_once_with(project)
            full_setup.assert_not_called()
            self.assertIn(str(project / setup.PYTHON_WHEEL_DIR), output.getvalue())
            self.assertFalse((project / cli.config.TERMINAL_SECRET).exists())

    def test_setup_tests_reports_unusable_environments(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with redirect_stderr(StringIO()) as errors:
                self.assertEqual(cli.main(["--project-dir", directory, "setup", "tests"]), 1)
            self.assertIn("No test requirements", errors.getvalue())
            requirements = project / setup.TEST_REQUIREMENTS
            requirements.parent.mkdir(parents=True)
            requirements.write_text("httpx==1\n", encoding="utf-8")
            for version, base_prefix, message in (
                ((3, 10, 14), "/usr", "require Python 3.11 or newer"),
                ((3, 13, 5), sys.prefix, "virtualenv that runs loaf test"),
            ):
                with (
                    self.subTest(message), patch.object(sys, "version_info", version),
                    patch.object(sys, "base_prefix", base_prefix), patch.object(setup.subprocess, "run") as run,
                    self.assertRaisesRegex(setup.SetupError, message),
                ):
                    setup.install_test_packages(project)
                run.assert_not_called()

    @unittest.skipIf(sys.version_info < (3, 11), "setup tests requires Python 3.11 or newer.")
    def test_setup_tests_caches_wheels_and_installs_only_from_the_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            requirements = project / setup.TEST_REQUIREMENTS
            requirements.parent.mkdir(parents=True)
            requirements.write_text("aiohttp==1\n", encoding="utf-8")
            wheels = project / setup.PYTHON_WHEEL_DIR
            calls = []

            def pip(command, **kwargs):
                action = "install" if "install" in command else "wheel"
                offline = "--no-index" in command
                calls.append((action, offline))
                self.assertEqual(command[command.index("--find-links") + 1], str(wheels))
                if action == "install":
                    self.assertTrue(offline)
                    self.assertEqual(kwargs["env"]["PIP_CONFIG_FILE"], os.devnull)
                elif offline and not (wheels / "aiohttp-1-py3-none-any.whl").is_file():
                    return subprocess.CompletedProcess(command, 1, stdout="No matching distribution")
                else:
                    stage = Path(command[command.index("--wheel-dir") + 1])
                    for name in ("aiohttp-1-py3-none-any.whl", "yarl-1-py3-none-any.whl"):
                        (stage / name).write_bytes(requirements.read_bytes())
                return subprocess.CompletedProcess(command, 0, stdout="pip log\n")

            with (
                patch.object(sys, "base_prefix", "/usr"), patch.object(sys, "prefix", "/project/.venv"),
                patch.object(setup.subprocess, "run", side_effect=pip),
                redirect_stdout(StringIO()), redirect_stderr(StringIO()) as status,
            ):
                setup.install_test_packages(project)
                self.assertEqual(calls, [("wheel", True), ("wheel", False), ("install", True)])
                self.assertEqual(sorted(path.name for path in wheels.iterdir()), [
                    setup.TEST_WHEEL_RECORD, "aiohttp-1-py3-none-any.whl", "yarl-1-py3-none-any.whl",
                ])
                calls.clear()
                setup.install_test_packages(project)
                self.assertEqual(calls, [("install", True)])
                self.assertIn("Reuse the cached test package wheels", status.getvalue())
                calls.clear()
                (wheels / "yarl-1-py3-none-any.whl").write_bytes(b"damaged")
                setup.install_test_packages(project)
                self.assertEqual(calls, [("wheel", True), ("install", True)])
                self.assertTrue(setup.wheels_are_current(
                    wheels, json.loads((wheels / setup.TEST_WHEEL_RECORD).read_text())["inputs"],
                    setup.TEST_WHEEL_RECORD,
                ))
                self.assertFalse(setup.wheels_are_current(wheels, {}, setup.WHEEL_CACHE_RECORD))
            self.assertFalse([path for path in wheels.iterdir() if path.name.startswith(".loaf-wheels-")])

    def test_test_requirements_pin_the_bundled_open_terminal_version(self):
        pins = dict(
            line.split("==") for line in (ROOT / setup.TEST_REQUIREMENTS).read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )
        self.assertEqual(set(pins), {"aiohttp", "yarl", "httpx", "open-terminal"})
        self.assertEqual(pins["open-terminal"], cli.config.OPEN_TERMINAL_IMAGE.rsplit(":", 1)[1].removesuffix("-slim"))

    def test_setup_opts_in_to_docker_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.quick_setup_project(directory)
            package = project / setup.PYTHON_PACKAGE_DIR
            package.mkdir()
            (package / "pyproject.toml").touch()
            with (
                patch.object(setup, "prepare_docker") as docker,
                patch.object(setup, "install_python"),
                patch.object(setup, "install_toolkit"),
                redirect_stdout(StringIO()) as output,
            ):
                command = ["--project-dir", directory, "setup", "--with-docker-deps"]
                self.assertEqual(cli.main([*command, "--dry-run"]), 0)
                docker.assert_not_called()
                self.assertIn("Debian Docker", output.getvalue())
                self.assertEqual(cli.main(command), 0)
            docker.assert_called_once_with(project)

    def test_release_setup_creates_a_project_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "loaf").touch()
            command = ["--project-dir", str(project), "setup"]
            with (
                patch.object(setup, "bundled_release", return_value=project / "runtime"),
                patch.object(setup, "install_python") as install,
                patch.object(setup, "install_toolkit") as toolkit,
                redirect_stdout(StringIO()) as log,
                redirect_stderr(StringIO()),
            ):
                self.assertEqual(cli.main([*command, "--dry-run"]), 0)
                self.assertIn("default configuration", log.getvalue())
                self.assertEqual(list(project.iterdir()), [project / "loaf"])
                self.assertEqual(cli.main(command), 0)
                configuration = project / "config/config.ini"
                # Every key is commented so that system settings and packaged defaults stay in effect.
                self.assertEqual(cli.config.load(project / "config"), cli.config.Settings())
                self.assertIn("# ollama_image = ", configuration.read_text(encoding="utf-8"))
                self.assertFalse((project / "config/models.ini").exists())
                self.assertTrue((project / cli.config.TERMINAL_SECRET).is_file())
                self.assertTrue((project / "docker-project/compose.yml").is_file())
                self.assertEqual((project / "docs/.loaf-docs").read_text().strip(), cli.assets.release_version())
                self.assertTrue((project / "docs/README.md").is_file())
                configuration.write_text("# local changes\n", encoding="utf-8")
                self.assertEqual(cli.main(command), 0)
            self.assertEqual(configuration.read_text(encoding="utf-8"), "# local changes\n")
            self.assertFalse((project / ".venv").exists())
            self.assertFalse((project / ".git").exists())
            self.assertFalse((project / setup.PYTHON_PACKAGE_DIR).exists())
            install.assert_not_called()
            toolkit.assert_not_called()

    def test_release_setup_does_not_write_through_a_config_link(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            outside = base / "outside"
            project.mkdir()
            outside.mkdir()
            (project / "config").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(setup.SetupError, "local configuration directory"):
                setup.setup_standalone(project)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((project / "docker-project").is_symlink())

    def test_prepared_docker_packages_install_with_only_the_local_source(self):
        for suite in ("trixie", "bookworm"):
            with self.subTest(suite=suite), tempfile.TemporaryDirectory(prefix="checkout with spaces ") as directory:
                project = Path(directory)
                cache = project / setup.DOCKER_DEB_DIR / f"{suite}-amd64"
                cache.mkdir(parents=True)
                (cache / "Packages").touch()

                def apt(mode, *args):
                    source = next(arg.split("=", 1)[1] for arg in args if arg.startswith("Dir::Etc::sourcelist="))
                    self.assertEqual(Path(source).read_text(encoding="utf-8"), f"deb [trusted=yes] {cache.as_uri()} ./\n")
                    self.assertIn("Dir::Etc::sourceparts=-", args)
                    if mode == "install":
                        self.assertEqual(args[-len(setup.DOCKER_PACKAGES[suite]):], setup.DOCKER_PACKAGES[suite])

                with (
                    patch.object(setup, "docker_release", return_value=suite),
                    patch.object(setup, "docker_cache", return_value=cache),
                    patch.object(setup.shutil, "which", return_value=None),
                    patch.object(setup, "download_docker_packages") as download,
                    patch.object(setup, "apt", side_effect=apt) as apt_calls,
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()),
                ):
                    setup.prepare_docker(project)
                download.assert_not_called()
                self.assertEqual([call.args[0] for call in apt_calls.call_args_list], ["update", "install"])

    def test_docker_downloads_use_a_private_state_and_the_pinned_repository_key(self):
        key = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nfixture\n"
        for suite in ("trixie", "bookworm"):
            with self.subTest(suite=suite), tempfile.TemporaryDirectory() as directory:
                cache = Path(directory) / setup.DOCKER_DEB_DIR / f"{suite}-amd64"
                calls = []

                def run(command, **kwargs):
                    options = dict(arg.split("=", 1) for arg in command if "=" in arg and "::" in arg)
                    parts = Path(options["Dir::Etc::sourceparts"])
                    docker_list = parts / "loaf-docker.list"
                    calls.append((command[-1] if command[-1] == "update" else "install", options, docker_list.is_file()
                                  and docker_list.read_text(encoding="utf-8")))
                    if command[-1] != "update":
                        for name in setup.DOCKER_PACKAGES[suite]:
                            (Path(options["Dir::Cache::archives"]) / f"{name}_1_amd64.deb").write_bytes(b"deb")
                    return subprocess.CompletedProcess(command, 0)

                def control(command, **kwargs):
                    return f"Package: {Path(command[-1]).name.split('_')[0]}\nVersion: 1\n"

                with (
                    patch.object(setup.subprocess, "run", side_effect=run),
                    patch.object(setup.subprocess, "check_output", side_effect=control),
                    patch.object(setup, "package_architecture", return_value="amd64"),
                    patch.object(setup, "urlopen", return_value=BytesIO(key)),
                    patch.object(setup, "DOCKER_KEY_SHA256", sha256(key).hexdigest()),
                ):
                    setup.download_docker_packages(cache, suite)
                self.assertEqual([call[0] for call in calls], ["update", "install"])
                for _, options, docker_list in calls:
                    self.assertEqual(options["Dir::Cache::pkgcache"], "")
                    self.assertFalse(options["Dir::State::lists"].startswith("/var/lib/apt"))
                    if suite == "bookworm":
                        self.assertIn(f"signed-by=", docker_list)
                        self.assertIn(f"] {setup.DOCKER_REPOSITORY} bookworm stable", docker_list)
                    else:
                        self.assertFalse(docker_list)
                index = (cache / "Packages").read_text(encoding="utf-8")
                for name in setup.DOCKER_PACKAGES[suite]:
                    self.assertIn(f"Package: {name}\n", index)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(setup.subprocess, "run") as run,
            patch.object(setup, "package_architecture", return_value="amd64"),
            patch.object(setup, "urlopen", return_value=BytesIO(b"replaced key")),
            self.assertRaisesRegex(setup.SetupError, "does not match the pinned SHA-256"),
        ):
            setup.download_docker_packages(Path(directory) / "bookworm-amd64", "bookworm")
        run.assert_not_called()

    def test_docker_dependencies_support_debian_12_and_13_only(self):
        for release, supported in (({"ID": "debian", "VERSION_CODENAME": "bookworm"}, True),
                                   ({"ID": "debian", "VERSION_CODENAME": "trixie"}, True),
                                   ({"ID": "debian", "VERSION_CODENAME": "bullseye"}, False),
                                   ({"ID": "ubuntu", "VERSION_CODENAME": "noble"}, False)):
            with self.subTest(release=release), patch.object(setup, "freedesktop_os_release", return_value=release):
                if supported:
                    self.assertEqual(setup.docker_release(), release["VERSION_CODENAME"])
                else:
                    with self.assertRaisesRegex(setup.SetupError, "Debian 12 .bookworm. or 13 .trixie."):
                        setup.docker_release()

    def test_apt_python_installs_matching_venv_package(self):
        python = Path("/usr/bin/python3.13")
        with (
            patch.object(setup.shutil, "which", return_value="/usr/bin/dpkg-query"),
            patch.object(setup.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
            ]),
            patch.object(setup.subprocess, "check_output", return_value="3.13\n"),
            patch.object(setup, "apt") as apt,
        ):
            setup.ensure_venv_support(python)
        apt.assert_called_once_with("install", "python3.13-venv")

    def test_pyenv_with_ensurepip_never_runs_apt(self):
        with (
            patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
            patch.object(setup, "apt") as apt,
        ):
            setup.ensure_venv_support(Path("/home/test/.pyenv/versions/3.12.7/bin/python"))
        self.assertEqual(run.call_count, 1)
        apt.assert_not_called()

    def test_broken_pyenv_does_not_install_an_unrelated_system_package(self):
        with (
            patch.object(setup.shutil, "which", return_value="/usr/bin/dpkg-query"),
            patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)),
            patch.object(setup, "apt") as apt,
        ):
            with self.assertRaisesRegex(setup.SetupError, "ensurepip"):
                setup.ensure_venv_support(Path("/home/test/.pyenv/versions/3.12.7/bin/python"))
        apt.assert_not_called()

    def test_toolkit_backup_matches_installed_versions(self):
        packages = {name: ("1.20.1-1", "amd64") for name in setup.TOOLKIT_PACKAGES}
        expected = [(name, *metadata) for name, metadata in packages.items()]
        newer = [(name, "1.20.2-1", "amd64") for name in packages]
        candidates = [(key, Path("_".join(key) + ".deb")) for key in [*expected, *newer]]
        with (
            patch.object(setup.shutil, "which", return_value="/usr/bin/dpkg-deb"),
            patch.object(setup, "installed_toolkit", return_value=packages),
            patch.object(setup, "deb_packages", return_value=candidates),
        ):
            self.assertEqual(setup.toolkit_files(Path("/project")), [path for _, path in candidates[:4]])

    def test_setup_installs_cached_debs_with_package_sources_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            cache = project / setup.TOOLKIT_DIR
            cache.mkdir(parents=True)
            packages = [cache / f"{name}_1.20.1-1_amd64.deb" for name in setup.TOOLKIT_PACKAGES]
            for package in packages:
                package.write_bytes(b"fixture installer")

            def install(mode, recommends, source_option, source, parts_option, parts, *files):
                self.assertEqual((mode, recommends), ("install", "--no-install-recommends"))
                self.assertEqual((source_option, parts_option), ("-o", "-o"))
                source_path = Path(source.removeprefix("Dir::Etc::sourcelist="))
                self.assertEqual(source_path.read_bytes(), b"")
                self.assertEqual(parts, f"Dir::Etc::sourceparts={source_path.parent}")
                self.assertEqual(files, tuple(str(path) for path in packages))

            with (
                patch.object(setup, "installed_toolkit", return_value={}),
                patch.object(setup, "staged_toolkit", return_value=packages) as staged,
                patch.object(setup, "toolkit_files", return_value=packages),
                patch.object(setup, "apt", side_effect=install) as apt,
                redirect_stdout(StringIO()), redirect_stderr(StringIO()),
            ):
                setup.install_toolkit(project)
            self.assertEqual(staged.call_args.args[0][0], cache)
            apt.assert_called_once()
            self.assertEqual(sorted(cache.iterdir()), sorted(packages))

    def test_setup_downloads_missing_debs_and_reuses_them_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            cache = project / setup.TOOLKIT_DIR
            version, arch = "1.20.1-1", "amd64"
            installed = dict.fromkeys(setup.TOOLKIT_PACKAGES, (version, arch))
            names = {name: f"{name}_{version}_{arch}.deb" for name in installed}

            def deb_packages(directories):
                for folder in directories:
                    for name, filename in names.items():
                        if (path := folder / filename).is_file():
                            yield (name, version, arch), path

            def download(command, *, cwd, check):
                self.assertTrue(check)
                self.assertEqual(command, ["apt-get", "download", *(f"{name}:{arch}={version}" for name in names)])
                for filename in names.values():
                    (cwd / filename).write_bytes(b"downloaded installer")

            with (
                patch.object(setup, "APT_CACHE", project / "apt-cache"),
                patch.object(setup.shutil, "which", return_value="/usr/bin/tool"),
                patch.object(setup, "installed_toolkit", return_value=installed),
                patch.object(setup, "deb_packages", side_effect=deb_packages),
                patch.object(setup.subprocess, "run", side_effect=download) as run,
                redirect_stdout(StringIO()),
            ):
                setup.install_toolkit(project)
                setup.install_toolkit(project)
            run.assert_called_once()
            self.assertEqual({path.name for path in cache.iterdir()}, set(names.values()))

    def test_install_uses_the_wheel_cache_and_installs_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            wheels = project / setup.PYTHON_WHEEL_DIR
            python = setup.venv_python(project / ".venv")
            package = project / setup.PYTHON_PACKAGE_DIR
            package.mkdir()
            (package / "pyproject.toml").touch()
            entry = python.with_name("loaf")
            entry.parent.mkdir(parents=True)
            entry.touch(mode=0o755)
            (project / ".venv/pyvenv.cfg").touch()
            with (
                patch.object(setup, "interpreter_works", return_value=True),
                patch.object(setup, "command_works", return_value=True),
                patch.object(setup, "prepare_python_wheels", return_value=wheels) as prepare,
                patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout="successful pip install log\n",
                )) as run,
                redirect_stdout(StringIO()) as output,
                redirect_stderr(StringIO()) as status,
            ):
                setup.install_python(project)
                prepare.assert_called_once_with(project, python, allow_downloads=True)
                command = run.call_args.args[0]
                setup.install_python(project)
                run.assert_called_once()
            self.assertEqual(command[-6:], ["install", "--no-index", "--find-links", str(wheels), "-e", str(package)])
            self.assertNotIn("--no-deps", command)
            self.assertIn("already installed", status.getvalue())
            self.assertNotIn("successful pip install log", output.getvalue() + status.getvalue())

    def test_failed_pip_install_displays_the_captured_log(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            wheels = project / setup.PYTHON_WHEEL_DIR
            wheels.mkdir(parents=True)
            package = project / setup.PYTHON_PACKAGE_DIR
            package.mkdir()
            (package / "pyproject.toml").touch()
            with (
                patch.object(setup, "interpreter_works", return_value=True),
                patch.object(setup, "command_works", return_value=True),
                patch.object(setup, "prepare_python_wheels", return_value=wheels),
                patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["pip"], 7, stdout="specific pip failure\n",
                )),
                redirect_stderr(StringIO()) as errors,
            ):
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    setup.install_python(project)
            self.assertEqual(failure.exception.returncode, 7)
            self.assertIn("specific pip failure", errors.getvalue())
            self.assertFalse((project / ".venv/.loaf-editable").exists())

    def test_only_install_retries_wheel_resolution_with_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            wheel_dir = project / setup.PYTHON_WHEEL_DIR
            wheel_dir.mkdir(parents=True)
            package = project / setup.PYTHON_PACKAGE_DIR
            package.mkdir()
            (package / "pyproject.toml").touch()

            def run_wheel(command, **kwargs):
                stage = Path(command[command.index("--wheel-dir") + 1])
                backend = stage / "backend-1-py3-none-any.whl"
                if command[-1] == "backend>=1":
                    if "--no-index" in command:
                        return subprocess.CompletedProcess(command, 1, stdout="Missing build backend")
                    backend.write_bytes(b"build wheel")
                else:
                    self.assertTrue(backend.is_file())
                    (stage / "launcher-1-py3-none-any.whl").write_bytes(b"new launcher wheel")
                return subprocess.CompletedProcess(command, 0, stdout="successful pip build log\n")

            with (
                patch.object(setup.subprocess, "check_output", return_value=b'[["backend>=1"], "3.13"]'),
                patch.object(setup.subprocess, "run", side_effect=run_wheel) as run,
                redirect_stderr(StringIO()) as status,
            ):
                with self.assertRaisesRegex(setup.SetupError, "Missing build backend"):
                    setup.prepare_python_wheels(project, Path(sys.executable))
                run.assert_called_once()
                self.assertIn("--no-index", run.call_args.args[0])
                self.assertFalse(list(wheel_dir.iterdir()))
                run.reset_mock()
                self.assertEqual(setup.prepare_python_wheels(project, Path(sys.executable), allow_downloads=True), wheel_dir)
            self.assertEqual(["--no-index" in call.args[0] for call in run.call_args_list], [True, False, True])
            self.assertIn("Fetch missing Python requirements", status.getvalue())
            self.assertNotIn("Missing build backend", status.getvalue())
            self.assertNotIn("successful pip build log", status.getvalue())
            self.assertEqual(sorted(path.name for path in wheel_dir.iterdir()), [
                setup.WHEEL_CACHE_RECORD, "backend-1-py3-none-any.whl", "launcher-1-py3-none-any.whl",
            ])

    def test_reuses_current_wheels_and_refreshes_after_changes_or_damage(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            wheels = project / setup.PYTHON_WHEEL_DIR
            package = project / setup.PYTHON_PACKAGE_DIR
            package.mkdir()
            metadata = package / "pyproject.toml"
            metadata.touch()
            module = package / "launcher.py"
            module.write_text("before", encoding="utf-8")
            wheel = wheels / "launcher-1-py3-none-any.whl"

            def build(command, **kwargs):
                target = Path(command[command.index("--wheel-dir") + 1])
                (target / wheel.name).write_bytes(module.read_bytes())
                return subprocess.CompletedProcess(command, 0, stdout="build log")

            with (
                patch.object(setup.subprocess, "check_output", return_value=b'[[], "3.13"]'),
                patch.object(setup.subprocess, "run", side_effect=build) as run,
                redirect_stderr(StringIO()) as status,
            ):
                setup.prepare_python_wheels(project, Path(sys.executable), allow_downloads=True)
                tests = package / "tests"
                tests.mkdir()
                (tests / "test_launcher.py").touch()
                deb_cache = project / setup.TOOLKIT_DIR
                deb_cache.mkdir(parents=True)
                (deb_cache / "nvidia-container-toolkit_example.deb").touch()
                setup.prepare_python_wheels(project, Path(sys.executable))
                run.assert_called_once()
                module.write_text("after!", encoding="utf-8")
                setup.prepare_python_wheels(project, Path(sys.executable))
                self.assertEqual(wheel.read_bytes(), b"after!")
                wheel.write_bytes(b"broken")
                setup.prepare_python_wheels(project, Path(sys.executable))
                self.assertEqual(run.call_count, 3)
                self.assertEqual(wheel.read_bytes(), b"after!")
            self.assertIn("Reuse the cached Python wheels", status.getvalue())

    def test_launcher_wheel_selects_the_current_build_from_the_cache_record(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            wheels = project / setup.PYTHON_WHEEL_DIR
            wheels.mkdir(parents=True)
            # An older launcher release stays in the cache next to the current build.
            (wheels / "local_ai_ferry-0.1.0-py3-none-any.whl").touch()
            record = wheels / setup.WHEEL_CACHE_RECORD
            current = {"local_ai_ferry-1.0.0-py3-none-any.whl": "a", "setuptools-80-py3-none-any.whl": "b"}
            record.write_text(json.dumps({"inputs": {}, "wheels": current}), encoding="utf-8")
            with patch.object(setup, "prepare_python_wheels", return_value=wheels) as prepare:
                self.assertEqual(setup.launcher_wheel(project), wheels / "local_ai_ferry-1.0.0-py3-none-any.whl")
            prepare.assert_called_once_with(project, allow_downloads=True)
            record.write_text(json.dumps({"inputs": {}, "wheels": {"setuptools-80-py3-none-any.whl": "b"}}),
                              encoding="utf-8")
            with patch.object(setup, "prepare_python_wheels", return_value=wheels):
                with self.assertRaisesRegex(setup.SetupError, "Expected one launcher wheel"):
                    setup.launcher_wheel(project)

    def test_unsuccessful_rebuild_restores_previous_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            environment = project / ".venv"
            environment.mkdir()
            config = environment / "pyvenv.cfg"
            config.write_text("command = /usr/bin/python3 -m venv /previous/.venv\n", encoding="utf-8")
            with (
                patch.object(setup, "interpreter_works", return_value=False),
                patch.object(setup, "ensure_venv_support"),
                patch.object(setup.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "venv")),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    setup.install_python(project)
            self.assertIn("/previous/.venv", config.read_text(encoding="utf-8"))
            self.assertEqual(list(project.iterdir()), [environment])

    def test_python_310_activation_script_detects_relocation_without_command_metadata(self):
        with tempfile.TemporaryDirectory(prefix="checkout with spaces ") as directory:
            environment = Path(directory).resolve() / ".venv"
            (environment / "bin").mkdir(parents=True)
            (environment / "pyvenv.cfg").write_text("version = 3.10.12\n", encoding="utf-8")
            activate = environment / "bin/activate"
            previous = environment.parent / "previous checkout" / ".venv"
            activate.write_text(f'VIRTUAL_ENV="{previous.as_posix()}"\n', encoding="utf-8")
            self.assertTrue(setup.environment_relocated(environment))
            activate.write_text(f'VIRTUAL_ENV="{environment.as_posix()}"\n', encoding="utf-8")
            self.assertFalse(setup.environment_relocated(environment))

    def test_venv_with_an_unavailable_base_home_needs_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory).resolve() / ".venv"
            base = getattr(sys, "_base_executable", sys.executable)
            subprocess.run([base, "-m", "venv", "--without-pip", str(environment)], check=True)
            python = setup.venv_python(environment)
            self.assertTrue(setup.interpreter_works(python, environment))
            config = environment / "pyvenv.cfg"
            lines = config.read_text(encoding="utf-8").splitlines()
            missing = environment.parent / "missing Python home"
            config.write_text("\n".join(
                f"home = {missing}" if line.startswith("home = ") else line for line in lines
            ) + "\n", encoding="utf-8")
            self.assertFalse(setup.interpreter_works(python, environment))


class TestRepositoryTests(unittest.TestCase):
    def test_local_repository_lists_its_tools_and_replaces_only_its_own_directory(self):
        calls = []

        def docker(command, **kwargs):
            calls.append(command)
            if command[1:3] == ["image", "inspect"]:
                return subprocess.CompletedProcess(command, 1 if "missing" in command[-1] else 0)
            mount = next(part for part in command if part.startswith("type=bind,"))
            repository = Path(mount.split("source=", 1)[1].split(",target=", 1)[0])
            self.assertEqual(command[command.index("--user") + 1], "0:0")
            self.assertNotIn("--network", command)
            self.assertEqual(command[-len(setup.TEST_REPOSITORY_TOOLS):], list(setup.TEST_REPOSITORY_TOOLS))
            (repository / "jq_1.7_amd64.deb").write_bytes(b"package")
            (repository / "Packages.gz").write_bytes(b"index")
            (repository / setup.TEST_REPOSITORY_LIST).write_text("jq\ntree\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory(prefix="loaf repository ") as folder:
            project = Path(folder)
            with patch.object(setup.subprocess, "run", side_effect=docker), redirect_stdout(StringIO()) as output:
                self.assertEqual(setup.create_test_repository(project, "loaf/open-terminal:latest"), ["jq", "tree"])
                self.assertEqual(setup.create_test_repository(project, "loaf/open-terminal:latest"), ["jq", "tree"])
            repository = project / setup.TEST_REPOSITORY_DIR
            self.assertEqual(sorted(path.name for path in repository.iterdir()),
                             ["Packages.gz", "jq_1.7_amd64.deb", setup.TEST_REPOSITORY_LIST])
            self.assertEqual((project / setup.TEST_REPOSITORY_SOURCE).read_text(),
                             "deb [trusted=yes] file:/run/loaf-apt/repository ./\n")
            self.assertIn("  jq\n  tree\n", output.getvalue())
            self.assertIn("apt_repository_dir = apt-test-repository", output.getvalue())
            self.assertFalse(list(project.glob(".loaf-repository-*")))

            with patch.object(setup.subprocess, "run", side_effect=docker), self.assertRaisesRegex(
                setup.SetupError, "terminal image loaf/missing:latest is missing",
            ):
                setup.create_test_repository(project, "loaf/missing:latest")
            (repository / setup.TEST_REPOSITORY_LIST).unlink()
            calls.clear()
            with patch.object(setup.subprocess, "run", side_effect=docker), self.assertRaisesRegex(
                setup.SetupError, "is not a loaf test repository",
            ):
                setup.create_test_repository(project, "loaf/open-terminal:latest")
            self.assertEqual(calls, [])
