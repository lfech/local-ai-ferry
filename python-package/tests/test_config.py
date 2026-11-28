"""Project INI validation and launcher precedence without Docker."""

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from loaf import assets, cli, config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="loaf INI ")
        self.addCleanup(folder.cleanup)
        self.project = Path(folder.name).resolve()
        self.config_dir = self.project / "config"
        self.config_dir.mkdir()
        self.path = self.config_dir / "config.ini"
        self.path.touch()

    def test_missing_file_uses_built_in_defaults(self):
        settings = config.load(self.config_dir)
        self.assertEqual(settings, config.Settings())
        self.assertTrue(settings.auth)

    def test_packaged_files_repeat_the_built_in_defaults(self):
        defaults = config.Settings()
        self.assertEqual(config.load(assets.DATA_DIR / "config"), defaults)
        # Direct Compose calls and plain Docker builds use these fallbacks without loaf.
        docker = assets.DATA_DIR / "docker"
        for path, pattern, expected in (
            (assets.DATA_DIR / "compose.yml", r"LOAF_INI_OLLAMA_IMAGE:-([^}]+)\}", defaults.ollama_image),
            (assets.DATA_DIR / "compose.yml", r"LOAF_INI_WEBUI_IMAGE:-([^}]+)\}", defaults.webui_image),
            (assets.DATA_DIR / "compose.yml", r"LOAF_TERMINAL_BASE_IMAGE:-([^}]+)\}", defaults.terminal_server_image),
            (assets.DATA_DIR / "compose.yml", r"LOAF_TERMINAL_NETWORK:-([^}]+)\}", defaults.terminal_server_network),
            (assets.DATA_DIR / "compose.yml", r"LOAF_TERMINAL_COMMAND_WAIT_SECONDS:-([^}]+)\}",
             str(defaults.terminal_server_command_wait_seconds)),
            (docker / "ollama/Dockerfile", r"ARG OLLAMA_IMAGE=(\S+)", defaults.ollama_image),
            (docker / "open-webui/Dockerfile", r"ARG OPEN_WEBUI_IMAGE=(\S+)", defaults.webui_image),
            (docker / "terminal-server/Dockerfile", r"ARG OPEN_TERMINAL_IMAGE=(\S+)", defaults.terminal_server_image),
        ):
            with self.subTest(path=path.name, pattern=pattern):
                self.assertEqual(re.findall(pattern, path.read_text(encoding="utf-8")), [expected])

    def test_ini_reads_literal_paths_and_typed_values(self):
        self.path.write_text(
            "[launcher]\nmodels_file = lists/%demo $new.ini\nwait_timeout = 450\ndocker_env_enable = yes\n"
            "[webui]\nbind_address = ::1\nport = 3100\n"
            "[online]\nollama_cloud = no\n"
            "[transfer]\ncompression_level = 0\n"
            "[terminal_server]\ncommand_wait_seconds = 0\n",
            encoding="utf-8-sig",
        )
        settings = config.load(self.config_dir)
        self.assertTrue(settings.docker_env_enable)
        self.assertEqual(settings.terminal_server_command_wait_seconds, 0)
        self.assertEqual(settings.compose_variables()["LOAF_TERMINAL_COMMAND_WAIT_SECONDS"], "0")
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

    def test_terminal_network_defaults_to_internet_and_rejects_the_old_name(self):
        self.assertEqual(config.load(self.config_dir).terminal_server_network, "internet")
        for value in ("isolated", "host", "local", "internet"):
            with self.subTest(value=value):
                self.path.write_text(f"[terminal_server]\nnetwork = {value}\n")
                self.assertEqual(config.load(self.config_dir).compose_variables()["LOAF_TERMINAL_NETWORK"], value)
        self.path.write_text("[terminal_server]\nnetwork = connected\n")
        with self.assertRaisesRegex(config.ConfigError, "expected isolated, host, local or internet"):
            config.load(self.config_dir)

    def test_builds_use_the_apt_settings_only_with_apt_repos(self):
        step = cli.Step(("terminal",), ("build", "terminal-server"))
        self.path.write_text("[terminal_server]\nenabled = true\napt_source_file=config/local mirror.sources\n"
                             "apt_repository_dir = mirror\n")
        settings = config.load(self.config_dir)
        # Without --with-apt-repos, a build reads neither setting, so missing files do not matter.
        plain = cli.Compose(self.project, settings=settings).variables(step)
        self.assertFalse([name for name in plain if "APT" in name], plain)
        compose = cli.Compose(self.project, settings=settings, build_apt_sources=True)
        self.assertFalse([name for name in compose.variables() if "APT" in name])
        with self.assertRaisesRegex(FileNotFoundError, "Terminal server APT source file is missing"):
            compose.variables(step)
        source = self.config_dir / "local mirror.sources"
        source.write_text("Types: deb\nURIs: file:/run/loaf-apt/repository\nSuites: ./\nTrusted: yes\n")
        with self.assertRaisesRegex(FileNotFoundError, "APT repository directory is missing"):
            compose.variables(step)
        repository = self.project / "mirror"
        repository.mkdir()
        (repository / "Packages.xz").write_bytes(b"")
        variables = compose.variables(step)
        # Buildx gets the source's text, not its file, and the repository as a named build context.
        self.assertEqual(variables["LOAF_TERMINAL_BUILD_APT_SOURCE"], source.read_text())
        self.assertEqual(variables["LOAF_TERMINAL_BUILD_APT_SOURCE_FORMAT"], "sources")
        self.assertEqual(variables["LOAF_TERMINAL_BUILD_APT_SOURCE_HASH"], sha256(source.read_bytes()).hexdigest())
        self.assertEqual(variables["LOAF_TERMINAL_BUILD_APT_REPOSITORY_DIR"], str(repository))
        self.assertNotIn("LOAF_TERMINAL_APT_SOURCE_FILE", variables)
        source.write_text("Types: deb\nURIs: http://modified.test/debian\nSuites: trixie\nComponents: main\n")
        self.assertNotEqual(compose.variables(step)["LOAF_TERMINAL_BUILD_APT_SOURCE_HASH"],
                            variables["LOAF_TERMINAL_BUILD_APT_SOURCE_HASH"])
        self.path.write_text("[terminal_server]\napt_source_file=config/local.txt\n")
        with self.assertRaisesRegex(config.ConfigError, "expected a Debian APT file"):
            config.load(self.config_dir)

    def test_every_terminal_start_applies_the_current_apt_settings(self):
        self.path.write_text("[terminal_server]\nenabled = true\napt_source_file = config/offline.sources\n"
                             "apt_repository_dir = mirror\n")
        compose = cli.Compose(self.project, settings=config.load(self.config_dir))
        start = cli.Step(("terminal",), ("up", "--no-build", "--pull", "never", "terminal-server"))
        stop = cli.Step(("terminal",), ("stop", "terminal-server"))
        build = cli.Step(("terminal",), ("build", "terminal-server"))
        overlays = [str(cli.assets.DATA_DIR / name) for name in
                    ("compose.terminal-runtime.yml", "compose.terminal-apt-source.yml", "compose.terminal-apt-repository.yml")]
        command = compose.command(start)
        self.assertTrue(all(overlay in command for overlay in overlays))
        for step in (stop, build):
            self.assertFalse(set(overlays) & set(compose.command(step)))
            self.assertFalse([name for name in compose.variables(step) if "APT" in name])
        with self.assertRaisesRegex(FileNotFoundError, "APT source file is missing"):
            compose.variables(start)
        source = self.config_dir / "offline.sources"
        source.write_text("Types: deb\nURIs: http://mirror.test/debian\nSuites: trixie\nComponents: main\n")
        with self.assertRaisesRegex(FileNotFoundError, "APT repository directory is missing"):
            compose.variables(start)
        repository = self.project / "mirror"
        repository.mkdir()
        with self.assertRaisesRegex(FileNotFoundError, "has no APT index"):
            compose.variables(start)
        (repository / "Packages.xz").write_bytes(b"")
        before = compose.variables(start)
        self.assertEqual(before["LOAF_TERMINAL_APT_SOURCE_FILE"], str(source))
        self.assertEqual(before["LOAF_TERMINAL_APT_SOURCE_FORMAT"], "sources")
        self.assertEqual(before["LOAF_TERMINAL_APT_REPOSITORY_DIR"], str(repository))
        # Each setting brings its own file: the launcher writes the repository's source beside the configured one.
        self.assertEqual(compose.repository_source(repository), "deb [trusted=yes] file:/run/loaf-apt/repository ./\n")
        compose.stage_terminal_runtime()
        written = self.project / cli.TERMINAL_RUNTIME_DIR / cli.REPOSITORY_SOURCE
        self.assertEqual(written.read_text(), compose.repository_source(repository))
        self.assertEqual(before["LOAF_TERMINAL_APT_REPOSITORY_SOURCE_HASH"], sha256(written.read_bytes()).hexdigest())
        # A changed source changes the terminal server's label, so the next start recreates it.
        source.write_text("Types: deb\nURIs: http://other.test/debian\nSuites: trixie\nComponents: main\n")
        self.assertNotEqual(before["LOAF_TERMINAL_APT_SOURCE_HASH"],
                            compose.variables(start)["LOAF_TERMINAL_APT_SOURCE_HASH"])
        # A source that names the repository itself keeps APT from seeing it twice.
        source.write_text("Types: deb\nURIs: file:/run/loaf-apt/repository\nSuites: ./\nTrusted: yes\n")
        self.assertTrue(compose.repository_source(repository).startswith("#"))
        # The repository applies on its own; without a source, the image keeps its APT sources.
        self.path.write_text("[terminal_server]\nenabled = true\napt_repository_dir = mirror\n")
        alone = cli.Compose(self.project, settings=config.load(self.config_dir))
        command = alone.command(start)
        self.assertIn(overlays[0], command)
        self.assertNotIn(overlays[1], command)
        self.assertIn(overlays[2], command)
        self.assertEqual(alone.variables(start)["LOAF_TERMINAL_APT_REPOSITORY_DIR"], str(repository))
        self.assertNotIn("LOAF_TERMINAL_APT_SOURCE_FILE", alone.variables(start))
        # The current provisioner serves even without either APT setting.
        for network in ("isolated", "host", "local", "internet"):
            with self.subTest(network=network):
                self.path.write_text(f"[terminal_server]\nenabled = true\nnetwork = {network}\n")
                plain = cli.Compose(self.project, settings=config.load(self.config_dir))
                self.assertIn(overlays[0], plain.command(start))
                self.assertFalse(set(overlays[1:]) & set(plain.command(start)))
                plain.stage_terminal_runtime()
                self.assertFalse(written.exists())

    def test_repository_sources_cover_flat_and_suite_repositories(self):
        compose = cli.Compose(self.project, settings=config.Settings())
        repository = self.project / "mirror"
        trixie = repository / "dists/trixie"
        trixie.mkdir(parents=True)
        (trixie / "InRelease").write_text("-----BEGIN PGP SIGNED MESSAGE-----\nSuite: stable\n"
                                          "Components: main contrib\n")
        local = repository / "dists/local"
        local.mkdir()
        (local / "Release").write_text("Codename: local\nComponents: main\n")
        (repository / "dists/stable").symlink_to("trixie")
        (repository / "dists/broken").mkdir()
        # A signed suite needs the image's keys; an unsigned one is trusted. Links and suites without Release go.
        self.assertEqual(compose.repository_source(repository),
                         "deb [trusted=yes] file:/run/loaf-apt/repository local main\n"
                         "deb file:/run/loaf-apt/repository trixie main contrib\n")
        (repository / "Packages").write_text("")
        self.assertTrue(compose.repository_source(repository).startswith(
            "deb [trusted=yes] file:/run/loaf-apt/repository ./\n"))
        (repository / "Packages").unlink()
        for suite in (trixie, local):
            for name in ("InRelease", "Release"):
                (suite / name).unlink(missing_ok=True)
        with self.assertRaisesRegex(FileNotFoundError, "no APT index"):
            compose.repository_source(repository)

    def test_only_an_active_repository_uri_overrides_the_generated_source(self):
        repository = self.project / "mirror"
        repository.mkdir()
        (repository / "Packages").touch()
        uri = "file:/run/loaf-apt/repository"
        generated = f"deb [trusted=yes] {uri} ./\n"
        for suffix, content, uses_repository in (
            ("list", f"# deb {uri} ./\n", False),
            ("list", f"deb http://mirror.test/debian trixie main # {uri}\n", False),
            ("list", f"deb-src [trusted=yes] {uri} ./\n", False),
            ("list", "deb [trusted=yes] file:///run/loaf-apt/repository/ ./\n", True),
            ("sources", f"Types: deb\nURIs: {uri}\nSuites: ./\nEnabled: no\n", False),
            ("sources", f"Types: deb\nURIs: {uri}\nSuites: ./\nEnabled: false\n", False),
            ("sources", f"Types: deb-src\nURIs: {uri}\nSuites: ./\nTrusted: yes\n", False),
            ("sources", f"Types:deb\nURIs:{uri}\nSuites:./\nEnabled:\n no\n", False),
            ("sources", "Types: deb\nURIs: http://mirror.test/debian\nSuites: trixie\nComponents: main\n"
                        "Signed-By: /run/loaf-apt/repository/key.gpg\n", False),
            ("sources", f"Types: deb\nURIs: http://mirror.test/debian\n {uri}\nSuites: ./\n", True),
        ):
            with self.subTest(content=content):
                source = self.config_dir / f"mirror.{suffix}"
                source.write_text(content)
                compose = cli.Compose(self.project, settings=config.Settings(terminal_server_apt_source_file=source))
                self.assertEqual(compose.repository_source(repository),
                                 "# apt_source_file names this repository.\n" if uses_repository else generated)

    def test_an_apt_source_must_match_its_format(self):
        def parsed(name):
            settings = config.Settings(terminal_server_apt_source_file=Path("config") / name)
            return cli.Compose(self.project, settings=settings).terminal_apt_source()

        for name, content, message in (
            ("deb822.list", "# mirror\nTypes: deb\nURIs: https://mirror.test/debian\n", "Use .sources"),
            ("one-line.sources", "deb https://mirror.test/debian trixie main\n", "Use .list"),
            ("missing-component.list", "deb https://mirror.test/debian trixie\n", "invalid Debian APT syntax"),
            ("missing-suite.sources", "Types: deb\nURIs: https://mirror.test/debian\n", "invalid Debian APT syntax"),
        ):
            with self.subTest(name=name):
                (self.config_dir / name).write_text(content)
                with self.assertRaisesRegex(config.ConfigError, message):
                    parsed(name)
        for name, content in (("good.list", "deb https://mirror.test/debian trixie main\n"),
                              ("good.sources", "Types: deb\nURIs: https://mirror.test/debian\nSuites: trixie\nComponents: main\n"),
                              ("empty.list", "")):
            (self.config_dir / name).write_text(content)
            self.assertEqual(parsed(name).path, self.config_dir / name)
        mirror = self.config_dir / "mirror"
        mirror.write_text("Types: deb\nURIs: https://mirror.test/debian\nSuites: trixie\nComponents: main\n")
        (self.config_dir / "linked.sources").symlink_to(mirror.name)
        self.assertEqual(parsed("linked.sources").path, mirror)
        (self.config_dir / "bad.list").write_bytes(b"# invalid byte \xff\n")
        with self.assertRaisesRegex(config.ConfigError, "bad.list must be UTF-8"):
            parsed("bad.list")

    def test_runtime_apt_sources_follow_the_network_mode(self):
        path = self.config_dir / "mirror.list"
        repo = self.project / "repo"
        repo.mkdir()
        (repo / "Packages").touch()
        for network, uri, mounted, accepts in (
            ("isolated", "http://host.docker.internal:8001/debian", False, False),
            ("isolated", "file:/run/loaf-apt/repository", False, False),
            ("isolated", "file:/run/loaf-apt/repository", True, True),
            ("host", "http://host.docker.internal:8001/debian", False, True),
            ("host", "https://deb.debian.org/debian", True, False),
            ("host", "file:/run/loaf-apt/repository", True, True),
            ("local", "http://mirror.lan/debian", False, True),
        ):
            with self.subTest(network=network, uri=uri, mounted=mounted):
                path.write_text(f"deb {uri} {'./' if uri.startswith('file:') else 'trixie main'}\n")
                settings = config.Settings(terminal_server_apt_source_file=path, terminal_server_network=network,
                                           terminal_server_apt_repository_dir=repo if mounted else None)
                compose = cli.Compose(self.project, settings=settings)
                if accepts:
                    self.assertEqual(compose.terminal_apt_inputs(serving=True)[0].path, path)
                else:
                    with self.assertRaisesRegex(config.ConfigError, f"network = {network} requires"):
                        compose.terminal_apt_inputs(serving=True)
                self.assertEqual(compose.terminal_apt_inputs(serving=False)[0].path, path)

    def test_local_model_selection_uses_packaged_files_only_when_missing_locally(self):
        # The running loaf's packaged files: its defaults and model lists.
        data = self.project / "packaged-data"
        defaults = data / "config"
        defaults.mkdir(parents=True)
        (data / "compose.yml").touch()
        packaged = patch.object(cli.assets, "DATA_DIR", data)
        packaged.start()
        self.addCleanup(packaged.stop)
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

    def test_edit_text_changes_single_lines_and_keeps_comments(self):
        text = "# Header\n[launcher]\n# Blank selects loaf.\nversion = v1\n\n[terminal_server]\n# enabled = false\n"
        edited = config.edit_text(text, {("launcher", "namespace"): "v2", ("terminal_server", "enabled"): "false",
                                         ("terminal_server", "secret_file"): "/etc/loaf/terminal-server.key",
                                         ("webui", "port"): "3100"})
        self.assertEqual(edited, "# Header\n[launcher]\n# Blank selects loaf.\nnamespace = v2\n\n[terminal_server]\n"
                                 "secret_file = /etc/loaf/terminal-server.key\nenabled = false\n\n[webui]\nport = 3100\n")
        self.assertEqual(config.edit_text("[launcher]\r\nversion = v1\r\n", {}), "[launcher]\r\nnamespace = v1\r\n")
        with self.assertRaises(ValueError):
            config.edit_text("", {("launcher", "namespace"): "a\nb"})

    def test_bad_ini_stops_before_docker(self):
        for ini, error in (
            ("[webui]\nport = 65536\n", "port from 1 to 65535"),
            ("[webui]\nauth = maybe\n", "expected true or false"),
            ("[ollama]\nstartup_timeout = 0\n", "positive integer"),
            ("[transfer]\ncompression_level = -1\n", "compression level from 0 to 9"),
            ("[online]\nwebui_downloads = maybe\n", "expected true or false"),
            ("[webui]\nbind_address = example.org\n", "expected an IPv4 or IPv6 address"),
            ("[launcher]\nnamespace = Invalid.1\n", "expected a lowercase Compose name"),
            ("[launcher]\nversion = v2\n", "unknown setting [launcher] version. It is now [launcher] namespace."),
            ("[launcher]\nwait_timeout = -1\n", "expected 0 to wait without a limit"),
            ("[launcher]\ndocker_env_enable = maybe\n", "expected true or false"),
            ("[terminal_server]\ncommand_wait_seconds = 241\n", "expected 0 to return at once or at most 240 seconds"),
            ("[terminal_server]\ncommand_wait_seconds = -1\n", "expected 0 to return at once or at most 240 seconds"),
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
                    cli.main(["--project-dir", str(self.project), "run", "offline"])
                run.assert_not_called()
                self.assertEqual(result.exception.code, 2)
                self.assertIn("config.ini", diagnostics.getvalue())
                self.assertIn(error, diagnostics.getvalue())

    def test_configured_deployment_model_file_and_timeout_can_be_overridden(self):
        self.path.write_text(
            "[launcher]\nnamespace = v2\nmodels_file = model list.ini\nwait_timeout = 420\n",
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
        for value in ("(loaf-v2)", "hf.co/example/configured", "420"):
            self.assertIn(value, plan)
        self.assertNotIn("hf.co/example/default", plan)
        with patch("loaf.cli.subprocess.run") as override_run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main([*args, "--namespace=", "--models-file", str(override), "--wait-timeout", "50"]), 0)
        plan = output.getvalue()
        for value in ("(loaf)", "hf.co/example/override", "wait up to 50s"):
            self.assertIn(value, plan)
        self.assertNotIn("loaf-v2", plan)
        self.assertNotIn("hf.co/example/configured", plan)
        self.assertNotIn("hf.co/example/default", plan)
        self.assertFalse((self.project / ".run.lock").exists())
        run.assert_not_called()
        override_run.assert_not_called()

    def test_serving_applies_webui_settings(self):
        self.path.write_text("[webui]\nbind_address = ::1\nport = 3100\nauth = false\n", encoding="utf-8")
        with patch("loaf.cli.subprocess.run") as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["--project-dir", str(self.project), "run", "online"]), 0)
        self.assertIn("http://[::1]:3100", output.getvalue())
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["LOAF_WEBUI_BIND_ADDRESS"], "::1")
        self.assertEqual(env["LOAF_WEBUI_PORT"], "3100")
        self.assertEqual(env["LOAF_WEBUI_AUTH"], "false")

    def test_verification_environment_overrides_ini_opt_ins(self):
        self.path.write_text(
            "[launcher]\nnamespace = v2\n[tests]\nrun_docker_tests = true\nrun_gpu_tests = true\n"
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
