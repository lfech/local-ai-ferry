"""Deployment files, settings layers, system mode and bundled guides."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import grp
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import unittest
from unittest.mock import patch

from loaf import assets, cli, config, docs, locks, setup


class DeploymentFilesTests(unittest.TestCase):
    """Every installation uses its own deployment files; a project holds settings and data."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="loaf-files-")
        self.addCleanup(folder.cleanup)
        self.project = Path(folder.name).resolve()

    def plan(self, *arguments):
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = cli.main(["--project-dir", str(self.project), "-v", *arguments, "run", "offline", "--dry-run"])
        return code, output.getvalue(), errors.getvalue()

    def test_a_project_with_settings_uses_this_loafs_files_and_ignores_its_own(self):
        (self.project / "config").mkdir()
        (self.project / "config/config.ini").touch()
        # Neither a Compose file in the project nor a docker-project link from an earlier release changes them.
        (self.project / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        (self.project / "docker-project").symlink_to(self.project, target_is_directory=True)
        code, output, _ = self.plan()
        self.assertEqual(code, 0)
        self.assertIn(f"--file {assets.DATA_DIR / 'compose.yml'}", output)
        self.assertNotIn(f"--file {self.project / 'compose.yml'}", output)

    def test_a_directory_without_settings_is_no_project(self):
        (self.project / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.plan()
        settings = self.project / "settings"
        settings.mkdir()
        (settings / "config.ini").touch()
        # The settings of --config-dir count as well.
        self.assertEqual(self.plan("--config-dir", str(settings))[0], 0)


class SettingsLayerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="loaf-layers-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.system = self.root / "etc-loaf"
        self.system.mkdir()
        self.project = self.root / "project"
        (self.project / "config").mkdir(parents=True)
        (self.project / "config/config.ini").touch()
        environment = patch.dict(os.environ, {"LOAF_SYSTEM_CONFIG_DIR": str(self.system)})
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, directory, text, name="config.ini"):
        (directory / name).write_text(text, encoding="utf-8")

    def test_project_settings_override_system_settings_which_override_defaults(self):
        self.write(self.system, "[webui]\nport = 3100\nauth = false\n[launcher]\nwait_timeout = 400\n")
        self.write(self.project / "config", "[webui]\nport = 3200\n")
        layers = config.layers(assets.DATA_DIR / "config", self.project / "config")
        self.assertEqual(layers, (assets.DATA_DIR / "config", self.system, self.project / "config"))
        settings = config.load(*layers)
        self.assertEqual((settings.port, settings.auth, settings.wait_timeout), (3200, False, 400))

    def test_only_the_project_can_move_or_skip_the_system_settings(self):
        other = self.root / "other"
        other.mkdir()
        self.write(other, "[webui]\nport = 3300\n")
        self.write(self.system, "[webui]\nport = 3100\n")
        defaults = assets.DATA_DIR / "config"
        self.write(self.project / "config", f"[launcher]\nsystem_config = {other}\n")
        self.assertEqual(config.load(*config.layers(defaults, self.project / "config")).port, 3300)
        self.write(self.project / "config", "[launcher]\nsystem_config =\n")
        self.assertEqual(config.layers(defaults, self.project / "config"), (defaults, self.project / "config"))
        self.write(self.project / "config", "[launcher]\nsystem_config = relative\n")
        with self.assertRaisesRegex(config.ConfigError, "absolute directory"):
            config.layers(defaults, self.project / "config")
        self.write(self.project / "config", "")
        self.write(self.system, f"[launcher]\nsystem_config = {other}\n")
        with self.assertRaisesRegex(config.ConfigError, "only in a project"):
            config.layers(defaults, self.project / "config")

    def test_models_come_from_the_highest_layer_with_a_list(self):
        layers = config.layers(assets.DATA_DIR / "config", self.project / "config")
        self.assertEqual(cli.select_models_file(layers, None, None), assets.DATA_DIR / "config/models.ini")
        self.write(self.system, "", "models.ini")
        self.assertEqual(cli.select_models_file(layers, None, None), self.system / "models.ini")
        self.write(self.project / "config", "", "models.ini")
        self.assertEqual(cli.select_models_file(layers, None, None), self.project / "config/models.ini")

    def test_status_names_the_settings_and_warns_about_a_shared_namespace(self):
        self.write(self.system, "[webui]\nport = 3100\n")
        with (
            patch.object(cli.readiness, "check", return_value=True), patch.object(cli.Compose, "execute"),
            redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(cli.main(["--project-dir", str(self.project), "status"]), 0)
            self.assertIn(f"packaged defaults, {self.system / 'config.ini'}", output.getvalue())
            self.assertIn("same Docker namespace loaf.", errors.getvalue())
            errors.truncate(0)
            self.assertEqual(cli.main(["--project-dir", str(self.project), "--namespace", "v2", "status"]), 0)
            self.assertNotIn("Warning", errors.getvalue())
        with (
            patch.object(setup, "install"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(cli.main(["--project-dir", str(self.project), "setup"]), 0)
        self.assertIn("same Docker namespace", errors.getvalue())


class SystemModeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="loaf-system-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.system_config = self.root / "etc"
        self.system_config.mkdir()
        self.state = self.root / "var-lib-loaf"
        self.state.mkdir()
        for patcher in (
            patch.dict(os.environ, {"LOAF_SYSTEM_CONFIG_DIR": str(self.system_config)}),
            patch.object(assets, "SYSTEM_PROJECT_DIR", self.state),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *arguments, cwd=None):
        with (
            patch.object(cli, "execute_plan") as execute,
            patch.object(cli.Path, "cwd", return_value=cwd or self.root),
            redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors,
        ):
            status = cli.main(list(arguments))
        return status, execute, output.getvalue(), errors.getvalue()

    def test_system_mode_uses_the_system_settings_and_state_directory(self):
        (self.system_config / "config.ini").write_text("[webui]\nport = 3111\n", encoding="utf-8")
        status, execute, _, _ = self.run_cli("--system", "run", "offline")
        self.assertEqual(status, 0)
        compose = execute.call_args.args[0]
        self.assertEqual(compose.project_dir, self.state)
        self.assertEqual(compose.files[0], assets.DATA_DIR / "compose.yml")
        self.assertEqual(compose.settings.port, 3111)
        self.assertEqual(execute.call_args.kwargs["lock_path"], self.state / ".run.lock")

    def test_the_debian_package_defaults_to_the_system_deployment(self):
        project = self.root / "portable"
        (project / "config").mkdir(parents=True)
        (project / "config/config.ini").touch()
        (project / "compose.yml").touch()
        with patch.object(assets, "debian_package", return_value=True):
            status, execute, _, errors = self.run_cli("run", "offline", cwd=project)
            self.assertEqual(execute.call_args.args[0].project_dir, self.state)
            self.assertIn("Add --project-dir .", errors)
            status, execute, _, _ = self.run_cli("--project-dir", str(project), "run", "offline")
            self.assertEqual(execute.call_args.args[0].project_dir, project)

    def test_system_mode_refuses_project_options_and_a_missing_state_directory(self):
        for arguments, message in (
            (("--system", "--project-dir", ".", "status"), "Omit --project-dir and --config-dir"),
            (("--system", "setup", "--with-docker-deps"), "uses the Debian package's dependencies"),
            (("--system", "setup", "tests"), "uses the Debian package's dependencies"),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                self.run_cli(*arguments)
        # The example names a part that the system deployment accepts.
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            cli.main(["--system", "setup", "--force"])
        self.assertIn("for example: loaf setup terminal-server-key --force", errors.getvalue())
        self.state.rmdir()
        status, _, _, errors = self.run_cli("--system", "status")
        self.assertEqual(status, 1)
        self.assertIn("system deployment directory", errors)

    def test_system_setup_creates_a_group_readable_key(self):
        group = grp.getgrgid(os.getgid()).gr_name
        (self.system_config / "config.ini").write_text(
            f"[terminal_server]\nsecret_file = {self.system_config / 'terminal-server.key'}\n", encoding="utf-8",
        )
        key = self.system_config / "terminal-server.key"
        with patch.object(setup, "SYSTEM_GROUP", group):
            # Plain setup leaves the key to setup terminal-server-key, which the Debian package runs.
            status, _, output, _ = self.run_cli("--system", "setup")
            self.assertEqual(status, 0)
            self.assertFalse(key.exists())
            self.assertIn("prepared the system deployment", output)
            status, _, output, _ = self.run_cli("--system", "setup", "terminal-server-key")
        self.assertEqual(status, 0)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o640)
        self.assertEqual(key.stat().st_gid, os.getgid())

    def test_a_setgid_directory_shares_its_lock_with_the_group(self):
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o2770)
        with locks.project_lock(shared / ".run.lock"):
            pass
        self.assertEqual(stat.S_IMODE((shared / ".run.lock").stat().st_mode), 0o660)
        with locks.project_lock(self.root / ".run.lock"):
            pass
        self.assertEqual(stat.S_IMODE((self.root / ".run.lock").stat().st_mode), 0o600)


class GuideTests(unittest.TestCase):
    def test_version_reports_the_release_installation_and_default_deployment(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["version"]), 0)
        report = output.getvalue()
        self.assertIn(f"loaf {assets.release_version()}\n", report)
        self.assertIn(f"Deployment data: {assets.DATA_DIR}", report)
        self.assertIn("Default deployment: current directory", report)
        with patch.object(assets, "debian_package", return_value=True), redirect_stdout(io.StringIO()) as output:
            cli.main(["version"])
        self.assertIn("Installation: Debian package", output.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["version", "--dry-run"])

    def test_docs_list_every_user_guide_and_refuse_unknown_names(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["docs"]), 0)
        listing = output.getvalue()
        for name in ("readme", "installation", "configuration", "transfer", "architecture", "loaf.1"):
            self.assertRegex(listing, rf"(?m)^  {name} ")
        self.assertNotIn("record", listing)
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(["docs", "missing"]), 1)
        self.assertIn("No guide named 'missing'", errors.getvalue())

    def test_links_point_into_the_exported_docs_or_the_release(self):
        files = {
            PurePosixPath("README.md"): None,
            PurePosixPath("docs/launcher.md"): None,
        }
        with tempfile.TemporaryDirectory() as folder:
            guide = Path(folder) / "guide.md"
            guide.write_text(
                "[R](../README.md) [S](launcher.md#select-models) [C](../python-package/src/loaf/data/compose.yml) "
                "[W](../.github/workflows/verify.yml) [U](https://example.invalid/x) [A](#local)\n"
                "[RR](/README.md) [RS](/docs/launcher.md#L2) [RW](/.github/workflows/verify.yml#L3)\n",
                encoding="utf-8",
            )
            files[PurePosixPath("docs/guide.md")] = guide
            rendered = docs.render(PurePosixPath("docs/guide.md"), files, "https://git.example/loaf/blob/v1")
        self.assertEqual(rendered, (
            "[R](README.md) [S](launcher.md#select-models) "
            "[C](https://git.example/loaf/blob/v1/python-package/src/loaf/data/compose.yml) "
            "[W](https://git.example/loaf/blob/v1/.github/workflows/verify.yml) [U](https://example.invalid/x) "
            "[A](#local)\n"
            "[RR](README.md) [RS](launcher.md#L2) [RW](https://git.example/loaf/blob/v1/.github/workflows/verify.yml#L3)\n"
        ))

    def test_export_writes_a_stamped_copy_and_keeps_user_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            with redirect_stdout(io.StringIO()):
                docs.export(project)
                stamp = project / "docs" / docs.STAMP
                self.assertEqual(stamp.read_text().strip(), assets.release_version())
                self.assertTrue((project / "docs/examples").is_dir())
                self.assertTrue((project / "docs/loaf.1").is_file())
                self.assertFalse(list(project.glob("docs/*-record.md")))
                # Guides of another release and directories without a stamp stay unless force replaces them.
                stamp.write_text("0.0.1\n", encoding="utf-8")
                (project / "docs/obsolete.md").touch()
                docs.export(project)
                self.assertTrue((project / "docs/obsolete.md").exists())
                docs.export(project, force=True)
                self.assertFalse((project / "docs/obsolete.md").exists())
                stamp.unlink()
                (project / "docs/notes.md").touch()
                docs.export(project)
            self.assertTrue((project / "docs/notes.md").is_file())
            self.assertEqual(sorted(path.name for path in project.iterdir()), ["docs"])

    def test_manual_page_lists_every_command_and_describes_each_option_once(self):
        page = docs.manual_page()
        self.assertTrue(page.startswith(".TH LOAF 1 "))
        for name in cli.COMMAND_HELP:
            self.assertIn(f"\\fB{docs.roff(name)}\\fR {docs.roff(cli.command_arguments(name))}", page)
        sections = ("NAME", "SYNOPSIS", "DESCRIPTION", "COMMANDS", "OPTIONS", "GLOBAL OPTIONS", "FILES", "SEE ALSO")
        self.assertEqual(re.findall(r"^\.SH (.+)$", page, re.MULTILINE), list(sections))
        commands = page[page.index(".SH COMMANDS"):page.index(".SH OPTIONS")]
        # Commands name their options; the option sections describe each one once.
        self.assertIn("Options: \\fB\\-\\-config\\-dir\\fR, \\fB\\-\\-namespace\\fR", commands)
        self.assertNotIn("Use PATH for settings", commands)
        parser = argparse.ArgumentParser(add_help=False)
        cli.add_options(parser)
        for action in parser._actions:
            if action.help != argparse.SUPPRESS:
                with self.subTest(option=action.option_strings[-1]):
                    self.assertEqual(page.count(f".TP\n\\fB{docs.roff(', '.join(action.option_strings))}\\fR"), 1)
                    self.assertEqual(page.count(docs.roff(action.help)), 1)
        globals_section = page[page.index(".SH GLOBAL OPTIONS"):page.index(".SH FILES")]
        self.assertIn("\\-\\-system", globals_section)
        self.assertNotIn("\\-\\-config\\-dir", globals_section)


class CheckoutLauncherTests(unittest.TestCase):
    def test_setup_links_loaf_to_the_virtualenv_and_keeps_an_existing_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            launcher = setup.link_checkout_launcher(project)
            self.assertEqual(launcher.readlink(), Path(".venv/bin/loaf"))
            self.assertEqual(setup.link_checkout_launcher(project), launcher)
            launcher.unlink()
            launcher.symlink_to("dist/loaf")
            setup.link_checkout_launcher(project)
            self.assertEqual(launcher.readlink(), Path("dist/loaf"))

    def test_full_cleanup_removes_the_virtualenv_link_with_the_virtualenv(self):
        from loaf import cleanup

        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder).resolve()
            (project / "python-package/src/loaf").mkdir(parents=True)
            (project / "python-package/src/loaf/__init__.py").touch()
            (project / ".venv/bin").mkdir(parents=True)
            setup.link_checkout_launcher(project)
            runtime = project / "runtime"
            self.assertIn(project / "loaf", cleanup.project_targets(project, runtime, True))
            self.assertNotIn(project / "loaf", cleanup.project_targets(project, runtime, False))
