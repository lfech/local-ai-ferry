"""Project INI validation and launcher precedence without Docker."""

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from loaf import cli, config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="loaf INI ")
        self.addCleanup(folder.cleanup)
        self.project = Path(folder.name).resolve()
        self.config_dir = self.project / "config"
        self.config_dir.mkdir()
        self.path = self.config_dir / "config.ini"
        (self.project / "compose.yml").touch()

    def test_missing_file_uses_built_in_defaults(self):
        settings = config.load(self.config_dir)
        self.assertEqual(settings, config.Settings())
        self.assertTrue(settings.auth)

    def test_ini_reads_literal_paths_and_typed_values(self):
        self.path.write_text(
            "[launcher]\nmodels_file = lists/%demo $new.ini\nwait_timeout = 450\n"
            "[webui]\nbind_address = ::1\nport = 3100\n"
            "[online]\nollama_cloud = no\n"
            "[transfer]\ncompression_level = 0\n",
            encoding="utf-8-sig",
        )
        settings = config.load(self.config_dir)
        self.assertEqual(settings.models_file, Path("lists/%demo $new.ini"))
        self.assertEqual(settings.wait_timeout, 450)
        self.assertFalse(settings.ollama_cloud)
        self.assertEqual(settings.compression_level, 0)
        self.assertEqual(settings.webui_url(), "http://[::1]:3100")

    def test_local_settings_keep_unset_package_defaults(self):
        defaults = self.project / "defaults"
        defaults.mkdir()
        (defaults / "config.ini").write_text("[webui]\nport = 3201\nauth = true\n", encoding="utf-8")
        self.path.write_text("[webui]\nauth = false\n", encoding="utf-8")
        settings = config.load(defaults, self.config_dir)
        self.assertEqual(settings.port, 3201)
        self.assertFalse(settings.auth)

    def test_terminal_base_image_uses_the_images_section(self):
        defaults = self.project / "defaults"
        defaults.mkdir()
        (defaults / "config.ini").write_text("[images]\nterminal_server_image=upstream:default\n")
        self.path.write_text("[terminal_server]\ninclude_in_backup=true\n")
        settings = config.load(defaults, self.config_dir)
        self.assertTrue(settings.terminal_server_include_in_backup)
        self.assertEqual(settings.compose_variables()["LOAF_TERMINAL_BASE_IMAGE"], "upstream:default")
        with self.path.open("a") as source:
            source.write("[images]\nterminal_server_image=upstream:current\n")
        settings = config.load(defaults, self.config_dir)
        self.assertEqual(settings.compose_variables()["LOAF_TERMINAL_BASE_IMAGE"], "upstream:current")

    def test_retired_terminal_settings_are_rejected(self):
        for source, message in (
            ("[tool_server]\nenabled=true\n", r"unknown section \[tool_server\]"),
            ("[terminal-server]\nenabled=true\n", r"unknown section \[terminal-server\]"),
            ("[terminal_server]\nbase_image=upstream:test\n", "unknown setting.*base_image"),
            ("[tests]\nrun_tool_server_tests=true\n", "unknown setting.*run_tool_server_tests"),
        ):
            with self.subTest(ini=source), self.assertRaisesRegex(config.ConfigError, message):
                self.path.write_text(source)
                config.load(self.config_dir)

    def test_terminal_apt_source_file_applies_only_to_builds(self):
        step = cli.Step(("terminal",), ("build", "terminal-server"))
        defaults = cli.Compose(self.project, settings=config.load(self.config_dir))
        self.assertNotIn("LOAF_TERMINAL_APT_SOURCE_FILE", defaults.variables(step))
        self.path.write_text("[terminal_server]\napt_source_file=config/local mirror.sources\n")
        compose = cli.Compose(self.project, settings=config.load(self.config_dir))
        self.assertNotIn("LOAF_TERMINAL_APT_SOURCE_FILE", compose.variables())
        with self.assertRaisesRegex(FileNotFoundError, "Terminal server APT source file is missing"):
            compose.variables(step)
        source = self.config_dir / "local mirror.sources"
        source.write_text("Types: deb\nURIs: http://mirror.test/debian\nSuites: trixie\nComponents: main\n")
        variables = compose.variables(step)
        self.assertEqual(variables["LOAF_TERMINAL_APT_SOURCE_FILE"], str(source))
        self.assertEqual(variables["LOAF_TERMINAL_APT_SOURCE_FORMAT"], "sources")
        self.assertEqual(variables["LOAF_TERMINAL_APT_SOURCE_HASH"], sha256(source.read_bytes()).hexdigest())
        source.write_text("Types: deb\nURIs: http://modified.test/debian\nSuites: trixie\nComponents: main\n")
        self.assertNotEqual(compose.variables(step)["LOAF_TERMINAL_APT_SOURCE_HASH"],
                            variables["LOAF_TERMINAL_APT_SOURCE_HASH"])
        self.path.write_text("[terminal_server]\napt_source_file=config/local.txt\n")
        with self.assertRaisesRegex(config.ConfigError, "expected a Debian APT file"):
            config.load(self.config_dir)

    def test_runtime_apt_source_changes_the_terminal_start_without_a_build(self):
        self.path.write_text("[terminal_server]\nenabled = true\nruntime_apt_source_file = config/offline.sources\n")
        compose = cli.Compose(self.project, settings=config.load(self.config_dir))
        start = cli.Step(("terminal",), ("up", "--no-build", "--pull", "never", "terminal-server"))
        stop = cli.Step(("terminal",), ("stop", "terminal-server"))
        build = cli.Step(("terminal",), ("build", "terminal-server"))
        overlay = str(cli.assets.DATA_DIR / "compose.terminal-apt.yml")
        self.assertIn(overlay, compose.command(start))
        for step in (stop, build):
            self.assertNotIn(overlay, compose.command(step))
            self.assertNotIn("LOAF_TERMINAL_RUNTIME_APT_SOURCE_FILE", compose.variables(step))
        with self.assertRaisesRegex(FileNotFoundError, "runtime APT source file is missing"):
            compose.variables(start)
        source = self.config_dir / "offline.sources"
        source.write_text("first source\n")
        before = compose.variables(start)
        self.assertEqual(before["LOAF_TERMINAL_RUNTIME_APT_SOURCE_FILE"], str(source))
        self.assertEqual(before["LOAF_TERMINAL_RUNTIME_APT_SOURCE_FORMAT"], "sources")
        source.write_text("second source\n")
        self.assertNotEqual(before["LOAF_TERMINAL_RUNTIME_APT_SOURCE_HASH"],
                            compose.variables(start)["LOAF_TERMINAL_RUNTIME_APT_SOURCE_HASH"])

    def test_local_model_selection_uses_packaged_files_only_when_missing_locally(self):
        data = self.project / "python-package/src/loaf/data"
        defaults = data / "config"
        defaults.mkdir(parents=True)
        (data / "compose.yml").touch()
        (self.project / "compose.yml").unlink()
        (defaults / "config.ini").write_text("[launcher]\nwait_timeout = 480\n", encoding="utf-8")
        (defaults / "models.ini").write_text("[models.default]\nmodel = hf.co/example/bundled\n", encoding="utf-8")
        (defaults / "example.ini").write_text("[models.example]\nmodel = hf.co/example/example\n", encoding="utf-8")
        args = ["--project-dir", str(self.project), "download", "--dry-run"]

        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(args), 0)
        self.assertIn("hf.co/example/bundled", output.getvalue())

        self.path.write_text("[launcher]\nmodels_file = example.ini\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(args), 0)
        self.assertIn("hf.co/example/example", output.getvalue())
        self.assertIn("wait up to 480s", output.getvalue())
        self.assertNotIn("hf.co/example/bundled", output.getvalue())

        (self.config_dir / "example.ini").write_text("[models.local]\nmodel = hf.co/example/local\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(args), 0)
        self.assertIn("hf.co/example/local", output.getvalue())
        self.assertNotIn("hf.co/example/example", output.getvalue())

    def test_bad_ini_stops_before_docker(self):
        for ini, error in (
            ("[webui]\nport = 65536\n", "port from 1 to 65535"),
            ("[webui]\nauth = maybe\n", "expected true or false"),
            ("[ollama]\nstartup_timeout = 0\n", "positive integer"),
            ("[transfer]\ncompression_level = -1\n", "compression level from 0 to 9"),
            ("[online]\nwebui_downloads = maybe\n", "expected true or false"),
            ("[webui]\nbind_address = example.org\n", "expected an IPv4 or IPv6 address"),
            ("[launcher]\nversion = Invalid.1\n", "expected a lowercase Compose name"),
            ("[webui]\nprt = 3030\n", "unknown setting"),
            ("[web]\nport = 3030\n", "unknown section"),
            ("[webui]\nport = 3030\nport = 3031\n", "already exists"),
        ):
            with self.subTest(ini=ini):
                self.path.write_text(ini, encoding="utf-8")
                with (
                    patch("loaf.cli.subprocess.run") as run,
                    redirect_stderr(io.StringIO()) as diagnostics,
                    self.assertRaises(SystemExit) as result,
                ):
                    cli.main(["--project-dir", str(self.project), "offline"])
                run.assert_not_called()
                self.assertEqual(result.exception.code, 2)
                self.assertIn("config.ini", diagnostics.getvalue())
                self.assertIn(error, diagnostics.getvalue())

    def test_configured_deployment_model_file_and_timeout_can_be_overridden(self):
        self.path.write_text(
            "[launcher]\nversion = v2\nmodels_file = model list.ini\nwait_timeout = 420\n",
            encoding="utf-8",
        )
        (self.config_dir / "models.ini").write_text("[models.default]\nmodel = hf.co/example/default\n", encoding="utf-8")
        (self.config_dir / "model list.ini").write_text("[models.custom]\nmodel = hf.co/example/configured\n", encoding="utf-8")
        override = self.project / "override.ini"
        override.write_text("[models.override]\nmodel = hf.co/example/override\n", encoding="utf-8")
        args = ["--project-dir", str(self.project), "download", "--dry-run"]
        with patch("loaf.cli.subprocess.run") as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(args), 0)
        plan = output.getvalue()
        for value in ("v2-local-inference", "hf.co/example/configured", "420"):
            self.assertIn(value, plan)
        self.assertNotIn("hf.co/example/default", plan)
        with patch("loaf.cli.subprocess.run") as override_run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main([*args, "--version=", "--models-file", str(override), "--wait-timeout", "50"]), 0)
        plan = output.getvalue()
        for value in ("local-inference", "hf.co/example/override", "wait up to 50s"):
            self.assertIn(value, plan)
        self.assertNotIn("v2-local-inference", plan)
        self.assertNotIn("hf.co/example/configured", plan)
        self.assertNotIn("hf.co/example/default", plan)
        self.assertFalse((self.project / ".run.lock").exists())
        run.assert_not_called()
        override_run.assert_not_called()

    def test_serving_applies_webui_settings(self):
        self.path.write_text("[webui]\nbind_address = ::1\nport = 3100\nauth = false\n", encoding="utf-8")
        with patch("loaf.cli.subprocess.run") as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["--project-dir", str(self.project), "online"]), 0)
        self.assertIn("http://[::1]:3100", output.getvalue())
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["LOAF_WEBUI_BIND_ADDRESS"], "::1")
        self.assertEqual(env["LOAF_WEBUI_PORT"], "3100")
        self.assertEqual(env["LOAF_WEBUI_AUTH"], "false")

    def test_verification_environment_overrides_ini_opt_ins(self):
        self.path.write_text(
            "[launcher]\nversion = v2\n[tests]\nrun_docker_tests = true\nrun_gpu_tests = true\n"
            "allow_legacy_terminal_bridge = true\n",
            encoding="utf-8",
        )
        (self.project / "python-package/tests").mkdir(parents=True)
        env = {key: value for key, value in os.environ.items() if key not in (
            "RUN_DOCKER_TESTS", "RUN_GPU_TESTS", "RUN_CATALOG_TESTS", "ALLOW_LEGACY_TERMINAL_BRIDGE",
        )}
        env["RUN_GPU_TESTS"] = "0"
        with (
            patch.dict(os.environ, env, clear=True),
            patch("loaf.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run,
        ):
            self.assertEqual(cli.main(["--project-dir", str(self.project), "test"]), 0)
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(
            [child_env[flag] for flag in ("RUN_DOCKER_TESTS", "RUN_GPU_TESTS", "RUN_CATALOG_TESTS")],
            ["1", "0", "0"],
        )
        self.assertEqual(child_env["ALLOW_LEGACY_TERMINAL_BRIDGE"], "1")

    def test_verification_cpu_overrides_gpu_settings(self):
        (self.project / "python-package/tests").mkdir(parents=True)
        with (
            patch.dict(os.environ, RUN_DOCKER_TESTS="0", RUN_GPU_TESTS="1", RUN_CPU_TESTS="0"),
            patch("loaf.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run,
        ):
            self.assertEqual(cli.main(["--project-dir", str(self.project), "test", "--cpu"]), 0)
        child_env = run.call_args.kwargs["env"]
        self.assertEqual([child_env[flag] for flag in ("RUN_DOCKER_TESTS", "RUN_CPU_TESTS", "RUN_GPU_TESTS")],
                         ["1", "1", "0"])
