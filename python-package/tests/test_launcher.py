# test_launcher.py - Offline regression tests for parsing and mode transitions.
# Use stdlib unittest and mocks for Docker; only lock/CLI checks spawn real Python.
# Temporary directories isolate every fixture from the user's lists and volumes.

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import shlex
import unittest
from unittest.mock import patch

from loaf import cli
from tests import DATA, ROOT


# --- Model-list grammar and Windows-compatible text encodings ---
class ModelListTests(unittest.TestCase):
    def read(self, data, filename="models.ini"):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / filename
            path.write_bytes(data)
            return cli.read_models(path)

    def test_plain_text_lists_are_rejected(self):
        with self.assertRaisesRegex(cli.ModelFileError, "use a model INI"):
            self.read(b"hf.co/test/chat\n", "models.txt")

    def test_empty_file_fails(self):
        with self.assertRaisesRegex(cli.ModelFileError, 'no models selected'):
            self.read(b'# only a comment\n\n')

    def test_non_utf8_file_fails(self):
        with self.assertRaises(cli.ModelFileError):
            self.read(b'\xff\xfe')

    def test_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(cli.ModelFileError, 'Cannot read'):
                cli.read_models(Path(folder) / 'missing.ini')

    def test_ini_default_accepts_utf8_bom_and_crlf(self):
        data = b"\xef\xbb\xbf# Windows editor\r\n[models.default]\r\nmodel = hf.co/test/default:Q4_K_M\r\n"
        self.assertEqual(self.read(data, "models.ini"), [cli.Model("hf.co/test/default:Q4_K_M")])

    def test_named_models_replace_default_and_keep_first_occurrence(self):
        data = (
            b"[models.default]\nmodel = hf.co/test/default\n"
            b"[models.chat]\nmodel = hf.co/test/chat\n"
            b"[models.coder-2.5]\nmodel = hf.co/test/coder:coder-q4_k_m.gguf\n"
            b"[models.chat-copy]\nmodel = hf.co/test/chat\n"
        )
        self.assertEqual(self.read(data, "models.ini"), [cli.Model("hf.co/test/chat"), cli.Model("hf.co/test/coder:coder-q4_k_m.gguf")])

    def test_invalid_ini_entries_are_reported(self):
        for data, message in (
            (b"# no entries\n", "no models selected"),
            (b"[DEFAULT]\nmodel = hf.co/test/implicit\n[models.chat]\n", r"use \[models\.default\]"),
            (b"[model.chat]\nmodel = hf.co/test/chat\n", "invalid section"),
            (b"[models.chat]\nref = hf.co/test/chat\n", "requires model"),
            (b"[models.chat]\nmodel = https://hf.co/test/chat\n", r"\[models.chat\]: invalid model specifier"),
            (b"[models.chat]\nmodel = hf.co/test/chat\nmodel = hf.co/test/again\n", "already exists"),
        ):
            with self.subTest(data=data), self.assertRaisesRegex(cli.ModelFileError, message):
                self.read(data, "models.ini")

    def test_all_supplied_lists_parse(self):
        lists = [DATA / 'config/models.ini', *ROOT.glob('config/model-examples/*.ini')]
        for path in lists:
            with self.subTest(path=path):
                self.assertTrue(cli.read_models(path))

    def test_alias_is_exported_under_its_ollama_name(self):
        source = "hf.co/test/chat:Q4_K_M"
        model, = self.read(f"[models.chat]\nmodel = {source}\nname = Friendly-Chat\n".encode())
        self.assertEqual(model.name, "Friendly-Chat:latest")
        # Only the display name stays visible after download.
        self.assertEqual([step.arguments[-4:] for step in cli.plan("download", [model])[3:]], [
            ("ollama-online", "ollama", "pull", source),
            ("ollama", "cp", source, model.name),
            ("ollama-online", "ollama", "rm", source),
        ])
        self.assertEqual([step.arguments[-2:] for step in cli.plan("download", [model], export_definitions=True)[3:]],
                         [("export-modelfiles", model.name)])

    def test_a_source_also_selected_unnamed_keeps_its_long_name(self):
        source = "hf.co/test/chat:Q4_K_M"
        models = [cli.Model(source), cli.Model(source, "Friendly-Chat:latest")]
        steps = cli.plan("download", models)
        self.assertFalse(any("rm" in step.arguments for step in steps))
        self.assertEqual([step.arguments[-4:] for step in steps[3:]], [
            ("ollama-online", "ollama", "pull", source), ("ollama", "cp", source, "Friendly-Chat:latest"),
        ])
        exported = [step.arguments[-4:] for step in cli.plan("download", models, export_definitions=True)[3:]]
        self.assertEqual(exported, [
            ("-T", "ollama-online", "export-modelfiles", source), ("ollama", "cp", source, "Friendly-Chat:latest"),
            ("-T", "ollama-online", "export-modelfiles", "Friendly-Chat:latest"),
        ])

    def test_alias_collision_is_rejected_case_insensitively(self):
        with self.assertRaisesRegex(cli.ModelFileError, "duplicate model name"):
            self.read(b"[models.a]\nmodel = hf.co/test/a\nname = Chat\n"
                      b"[models.b]\nmodel = hf.co/test/b\nname = chat:latest\n")

# --- Ordering, failure boundaries and explicit Compose arguments ---
class PlanTests(unittest.TestCase):
    models = ['hf.co/a/one:Q4_K_M', 'hf.co/b/two:Q8_0']

    def test_download_stops_both_serving_profiles_then_pulls_in_file_order(self):
        steps = cli.plan('download', [cli.Model(model) for model in self.models])
        self.assertEqual(steps[1].arguments,
                         ('stop', 'ollama-connected', 'open-webui-online', 'ollama-offline', 'open-webui'))
        self.assertEqual(steps[2].profiles, ('download',))
        self.assertEqual(steps[2].arguments[-1], 'ollama-online')
        self.assertEqual([s.arguments[-1] for s in steps[3:]], self.models)
        self.assertTrue(all(s.arguments[3:5] == ('ollama', 'pull') for s in steps[3:]))

    def test_export_only_exports_selected_models_without_pulls_or_builds(self):
        steps = cli.plan('download', [cli.Model(model) for model in self.models], export_definitions=True)
        self.assertTrue(all(s.arguments[0] != 'build' for s in steps))
        self.assertEqual([s.arguments[3:] for s in steps[3:]], [('export-modelfiles', m) for m in self.models])

    def test_offline_has_no_download_export_or_build_step(self):
        steps = cli.plan('offline')
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[1].arguments, ('stop', 'ollama-online', 'ollama-connected', 'open-webui-online'))
        self.assertEqual(steps[2].arguments[-2:], ('ollama-offline', 'open-webui'))
        self.assertIn('--no-build', steps[2].arguments)
        self.assertEqual(steps[2].arguments[steps[2].arguments.index('--pull')+1], 'never')
        self.assertTrue(all(s.arguments[0] not in ('build', 'pull', 'exec') for s in steps))

    def test_online_switches_to_the_connected_pair_without_downloading_models(self):
        steps = cli.plan('online')
        self.assertEqual(steps[1].arguments, ('stop', 'ollama-online', 'ollama-offline', 'open-webui'))
        self.assertEqual(steps[2].profiles, ('online',))
        self.assertEqual(steps[2].arguments[-2:], ('ollama-connected', 'open-webui-online'))
        self.assertEqual([step.arguments[0] for step in steps], ['config', 'stop', 'up'])

    def test_import_restarts_backend_with_health_wait_and_leaves_webui_stopped(self):
        # Import must rerun the existing startup wrapper, not `up` an already
        # healthy backend unchanged or accidentally start WebUI through its profile.
        steps = cli.plan('offline', wait_timeout=123, import_definitions=True)
        self.assertEqual(steps[1], cli.Step(cli.PROFILES, ('stop',)))
        startup = steps[2].arguments
        self.assertEqual(startup[-1], 'ollama-offline')
        self.assertNotIn('open-webui', startup)
        self.assertIn('--no-build', startup)
        self.assertEqual(startup[startup.index('--pull')+1], 'never')
        self.assertIn('--wait', startup)
        self.assertEqual(startup[startup.index('--wait-timeout')+1], '123')
        self.assertTrue(all(s.arguments[0] not in ('build', 'pull', 'exec') for s in steps))

    def test_build_and_upgrade_build_each_image_once_without_switching_mode(self):
        for mode, options in (('build', ()), ('upgrade', ('--pull',))):
            with self.subTest(mode=mode):
                steps = cli.plan(mode)
                self.assertEqual(len(steps), 2)
                self.assertEqual(steps[1].arguments, ('build', *options, 'ollama-online', 'ollama-offline', 'open-webui'))

    def test_invalid_modes_timeouts_and_empty_model_lists_fail(self):
        for args in [('bad', self.models), ('download', []), ('export', [])]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                cli.plan(*args)
        for mode in cli.MODES:
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                cli.plan(mode, self.models, wait_timeout=0)

    def test_compose_paths_are_absolute_and_not_dependent_on_shell_cwd(self):
        # Spaces catch accidental shell splitting; absolute paths catch cwd leaks.
        with tempfile.TemporaryDirectory(prefix='project with spaces ') as folder:
            compose = cli.Compose(Path(folder))
            command = compose.command(cli.plan('offline')[0])
            self.assertEqual(command[command.index('--project-directory')+1], str(Path(folder).resolve()))
            self.assertEqual(command[command.index('--file')+1], str((Path(folder)/'compose.yml').resolve()))
            self.assertEqual(command[command.index('--project-name')+1], 'loaf')

    def test_subprocess_uses_argument_list_without_shell(self):
        compose = cli.Compose()
        step = cli.plan('download', [cli.Model(model) for model in self.models])[-1]
        with patch('loaf.cli.subprocess.run') as mocked, redirect_stderr(io.StringIO()):
            compose.execute(step)
        args, kwargs = mocked.call_args
        self.assertEqual(args[0][-1], self.models[-1])
        self.assertFalse(kwargs['shell'])
        self.assertTrue(kwargs['check'])
        self.assertEqual(kwargs['env']['LOAF_VERSION'], '')

    def test_failed_command_prevents_all_later_commands(self):
        # Fail the second planned operation, then prove no later operation ran.
        with tempfile.TemporaryDirectory() as folder:
            compose = cli.Compose()
            with patch.object(compose, 'execute', side_effect=[None, subprocess.CalledProcessError(5, 'docker')]) as mocked:
                with self.assertRaises(subprocess.CalledProcessError):
                    cli.execute_plan(compose, cli.plan('download', [cli.Model(model) for model in self.models]), lock_path=Path(folder)/'lock')
                self.assertEqual(mocked.call_count, 2)

    def test_failed_build_does_not_stop_running_services(self):
        # Config succeeds, build fails: the stop/up boundary must remain unreached.
        with tempfile.TemporaryDirectory() as folder:
            compose = cli.Compose()
            with patch.object(compose, 'execute', side_effect=[None, subprocess.CalledProcessError(3, 'docker')]) as mocked:
                with self.assertRaises(subprocess.CalledProcessError):
                    cli.execute_plan(compose, cli.plan('build'), lock_path=Path(folder)/'lock')
                self.assertEqual([call.args[0].arguments[0] for call in mocked.call_args_list], ['config', 'build'])

    def test_failed_pull_does_not_attempt_the_next_model(self):
        with tempfile.TemporaryDirectory() as folder:
            compose = cli.Compose()
            with patch.object(compose, 'execute', side_effect=[None, None, None, subprocess.CalledProcessError(4, 'docker')]) as mocked:
                with self.assertRaises(subprocess.CalledProcessError):
                    cli.execute_plan(compose, cli.plan('download', [cli.Model(model) for model in self.models]), lock_path=Path(folder)/'lock')
                self.assertEqual(mocked.call_count, 4)
                self.assertEqual(mocked.call_args.args[0].arguments[-1], self.models[0])

    def test_dry_run_does_not_run_docker_or_create_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder)/'lock'
            with patch('loaf.cli.subprocess.run') as mocked, redirect_stdout(io.StringIO()) as output:
                cli.execute_plan(cli.Compose(), cli.plan('offline'), lock_path=lock, dry_run=True)
            mocked.assert_not_called()
            self.assertFalse(lock.exists())
            self.assertIn('Start offline Ollama and Open WebUI', output.getvalue())
            self.assertNotIn('+ ', output.getvalue())

    def test_busy_project_lock_prevents_every_docker_step(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder)/'lock'
            compose = cli.Compose()
            with cli.project_lock(lock), patch.object(compose, 'execute') as mocked:
                with self.assertRaises(cli.AlreadyRunningError):
                    cli.execute_plan(compose, cli.plan('offline'), lock_path=lock)
                mocked.assert_not_called()

    def test_interruption_stops_steps_and_releases_lock(self):
        # Reacquisition after KeyboardInterrupt checks the context manager's finally.
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder)/'lock'
            compose = cli.Compose()
            with patch.object(compose, 'execute', side_effect=KeyboardInterrupt) as mocked:
                with self.assertRaises(KeyboardInterrupt):
                    cli.execute_plan(compose, cli.plan('offline'), lock_path=lock)
                self.assertEqual(mocked.call_count,1)
            with cli.project_lock(lock):
                pass


# --- Copyable commands are shell-specific; execution still uses argument lists ---
class CommandDisplayTests(unittest.TestCase):
    def test_powershell_quotes_apostrophes_and_literal_special_characters(self):
        # Patch sys.platform, not os.name: Path must retain the real host's flavor.
        arguments = ['python', "C:/O'Brien/cli.py", 'export', '$literal;still-an-argument']
        with patch('loaf.cli.sys.platform','win32'):
            displayed = cli.display_command(arguments)
        self.assertEqual(displayed, "& 'python' 'C:/O''Brien/cli.py' 'export' '$literal;still-an-argument'")

    def test_posix_display_round_trips_arguments(self):
        arguments = ['python3', '/folder with spaces/cli.py', 'export', "O'Brien"]
        with patch('loaf.cli.sys.platform','linux'):
            self.assertEqual(shlex.split(cli.display_command(arguments)),arguments)


# --- Real OS locking: contention, exceptions and a separate interpreter ---
class LockTests(unittest.TestCase):
    def test_overlapping_launches_are_rejected_and_release_allows_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'lock'
            with cli.project_lock(path):
                with self.assertRaises(cli.AlreadyRunningError):
                    with cli.project_lock(path):
                        self.fail('Second lock acquired')
            with cli.project_lock(path):
                pass

    def test_exception_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'lock'
            with self.assertRaises(RuntimeError):
                with cli.project_lock(path):
                    raise RuntimeError('test failure')
            with cli.project_lock(path):
                pass

    def test_lock_is_enforced_between_processes(self):
        # Exit 9 is a test sentinel for AlreadyRunningError, not a launcher exit
        # contract. A child process proves the lock is more than in-memory state.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'lock'
            code = 'from pathlib import Path; from loaf import cli; import sys;\ntry:\n with cli.project_lock(Path(sys.argv[1])): pass\nexcept cli.AlreadyRunningError: sys.exit(9)\n'
            with cli.project_lock(path):
                child = subprocess.run([sys.executable, '-c', code, str(path)], cwd=ROOT, capture_output=True)
                self.assertEqual(child.returncode, 9, child.stderr.decode())


# --- Public help and CLI diagnostics ---
class HelpTests(unittest.TestCase):
    def read_help(self, *arguments):
        with (
            redirect_stdout(io.StringIO()) as output,
            patch("loaf.cli.config.load") as load,
            patch("loaf.cli.subprocess.run") as run,
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(arguments)
        self.assertEqual(raised.exception.code, 0)
        load.assert_not_called()
        run.assert_not_called()
        return output.getvalue()

    def test_top_level_lists_commands_before_global_options(self):
        output = self.read_help("--help")
        self.assertTrue(output.startswith("usage: loaf [GLOBAL OPTIONS] COMMAND [OPTIONS]"))
        commands, global_options = output.split("\nGlobal options:\n", 1)
        self.assertEqual(
            [line.split()[0] for line in commands.split("\nCommands\n", 1)[1].splitlines() if line.strip()],
            list(cli.COMMAND_HELP),
        )
        for flag in ("-h, --help", "--project-dir PATH", "-v, --verbose"):
            self.assertIn(flag, global_options)
        for flag in ("--cpu", "--archive", "--wait-timeout", "--version"):
            self.assertNotIn(flag, output)

    def test_each_command_has_help_before_project_validation(self):
        for command in cli.COMMAND_HELP:
            with self.subTest(command=command):
                output = self.read_help("--project-dir", "offline", command, "-h")
                self.assertTrue(output.startswith(f"usage: loaf {command} "))
                self.assertIn("\nGlobal options:\n", output)

    def test_command_help_shows_relevant_flags_and_aliases(self):
        flags = {
            "install": ("--with-docker-deps", "--archive PATH"),
            "clean": ("--full", "--dry-run"),
            "online": ("--cpu", "--wait-timeout SECONDS"),
            "backup": ("--verify", "--no-terminal-server", "--export", "--no-export"),
            "restore": ("--import", "--no-import", "--archive PATH", "--cpu"),
            "stop": ("-n", "--remove-containers", "--no-remove-containers"),
            "test": ("--config-dir PATH", "--badges", "--cpu"),
        }
        for command, expected in flags.items():
            with self.subTest(command=command):
                options = self.read_help(command, "--help").split("\nGlobal options:\n", 1)[0]
                for flag in expected:
                    self.assertIn(flag, options)
                for flag in ("--cpu", "--full", "--verify", "--no-terminal-server", "--no-import", "--badges"):
                    self.assertEqual(flag in options, flag in expected, flag)

    def test_integration_help_lists_names_and_each_integration_has_its_own_flags(self):
        output = self.read_help("integration", "--help")
        for integration in cli.INTEGRATION_HELP:
            with self.subTest(integration=integration):
                self.assertIn(f"\n  {integration}", output)
                self.assertTrue(self.read_help("integration", integration, "--help").startswith(
                    f"usage: loaf integration {integration} ",
                ))
        systemd = self.read_help("integration", "systemd", "--help")
        self.assertIn("--version VERSION", systemd)
        self.assertNotIn("--models-file", systemd)
        continuation = self.read_help("integration", "continue", "--help")
        self.assertIn("--models-file PATH", continuation)
        self.assertNotIn("--version", continuation)
        self.assertNotIn("\nOptions:\n", self.read_help("integration", "tether", "--help"))


class CliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="loaf-cli-test-")
        self.addCleanup(folder.cleanup)
        self.project = Path(folder.name).resolve()
        (self.project / "compose.yml").touch()

    def invoke(self, *arguments):
        return cli.main(["--project-dir", str(self.project), *arguments])

    def make_source_directory(self):
        (self.project / "compose.yml").unlink()
        (self.project / "scripts").mkdir()
        for script in cli.PACKAGE_SCRIPTS.values():
            (self.project / script).touch()
        package = self.project / "python-package"
        package.mkdir()
        (package / "pyproject.toml").touch()

    def test_package_binary_previews_and_builds_the_selected_source_directory(self):
        self.make_source_directory()
        script = self.project / "scripts/build-release.sh"
        with patch("loaf.cli.subprocess.run") as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.invoke("package", "binary", "--dry-run"), 0)
            run.assert_not_called()
            self.assertIn(str(self.project / "dist/loaf"), output.getvalue())
            self.assertFalse((self.project / "dist").exists())

            run.return_value.returncode = 0
            self.assertEqual(self.invoke("package", "binary"), 0)
        self.assertEqual(run.call_args.args[0], ["bash", str(script), "--binary-only"])
        self.assertEqual(run.call_args.kwargs["cwd"], self.project)
        self.assertEqual(run.call_args.kwargs["env"]["LOAF_BUILD_PYTHON"], sys.executable)

    def test_package_debian_builds_the_package_from_the_cached_launcher_wheel(self):
        self.make_source_directory()
        script = self.project / "scripts/build-deb.sh"
        wheel = self.project / "install-cache/wheels/local_ai_ferry-1.0.0-py3-none-any.whl"
        with (
            patch("loaf.cli.subprocess.run") as run, patch("loaf.cli.setup.launcher_wheel", return_value=wheel) as build,
            patch("loaf.cli.shutil.which", return_value="/usr/bin/dpkg-deb"), redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(self.invoke("package", "debian", "--dry-run"), 0)
            run.assert_not_called()
            build.assert_not_called()
            self.assertIn(f"Debian package in {self.project / 'dist'}", output.getvalue())

            run.return_value.returncode = 0
            self.assertEqual(self.invoke("package", "debian"), 0)
        build.assert_called_once_with(self.project)
        self.assertEqual(run.call_args.args[0], ["bash", str(script), str(wheel), str(self.project / "dist")])
        self.assertEqual(run.call_args.kwargs["cwd"], self.project)
        self.assertEqual(run.call_args.kwargs["env"]["LOAF_BUILD_PYTHON"], sys.executable)

    def test_package_debian_requires_dpkg_deb_before_building_wheels(self):
        self.make_source_directory()
        with (
            patch("loaf.cli.subprocess.run") as run, patch("loaf.cli.setup.launcher_wheel") as build,
            patch("loaf.cli.shutil.which", return_value=None), redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(self.invoke("package", "debian"), 1)
        build.assert_not_called()
        run.assert_not_called()
        self.assertIn("requires dpkg-deb", errors.getvalue())

    def test_package_runs_targets_in_order_and_stops_at_the_first_failure(self):
        self.make_source_directory()
        with (
            patch("loaf.cli.subprocess.run") as run, patch("loaf.cli.setup.launcher_wheel") as build,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(self.invoke("package", "debian", "binary", "--dry-run"), 0)
            lines = output.getvalue().splitlines()
            self.assertIn("Debian package", lines[0])
            self.assertIn("standalone executable", lines[1])

            run.return_value.returncode = 3
            self.assertEqual(self.invoke("package", "binary", "debian"), 3)
        run.assert_called_once()
        build.assert_not_called()

    def test_live_command_trace_requires_verbose(self):
        for verbose in ((), ('-v',)):
            with (
                self.subTest(verbose=verbose),
                patch('loaf.cli.subprocess.run') as run,
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()) as status,
            ):
                self.assertEqual(self.invoke(*verbose, 'offline'), 0)
                self.assertEqual(run.call_count, 3)
                commands = [line for line in output.getvalue().splitlines() if line.startswith('+ ')]
                self.assertEqual(len(commands), 3 if verbose else 0)
                self.assertIn('are ready at', output.getvalue())
                if not verbose:
                    self.assertIn('Stop services from other profiles', status.getvalue())
                    self.assertIn('Start offline Ollama and Open WebUI', status.getvalue())

    def test_attach_follows_the_downloader_after_models_and_releases_the_lock(self):
        output = io.StringIO()

        def run(arguments, **kwargs):
            if 'logs' in arguments:
                with cli.project_lock(self.project / '.run.lock'):
                    self.assertIn('Downloads complete.', output.getvalue())
                raise KeyboardInterrupt

        with (
            patch('loaf.cli.subprocess.run', side_effect=run) as mocked,
            redirect_stdout(output), redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(self.invoke('-a', 'download'), 130)
        self.assertIn('pull', mocked.call_args_list[3].args[0])
        self.assertEqual(mocked.call_args.args[0][-7:], [
            '--profile', 'download', 'logs', '--follow', '--tail', '0', 'ollama-online',
        ])

    def test_attach_previews_the_matching_services_without_running_compose(self):
        scopes = {
            'offline': ('ollama-offline', 'open-webui'), 'online': ('ollama-connected', 'open-webui-online'),
            'download': ('ollama-online',),
        }
        for mode in (*cli.MODES, *cli.TRANSFER_MODES):
            with (
                self.subTest(mode=mode), patch('loaf.cli.subprocess.run') as run,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(self.invoke(mode, '-v', '--attach', '--dry-run'), 0)
            run.assert_not_called()
            suffix = ' '.join(('logs', '--follow', '--tail', '0', *scopes.get(mode, ())))
            self.assertTrue(output.getvalue().splitlines()[-1].endswith(suffix))
        self.assertFalse((self.project / '.run.lock').exists())

    def test_download_suggests_offline_with_project_path_and_version(self):
        with tempfile.TemporaryDirectory(prefix='models with spaces ') as folder:
            selected = Path(folder).resolve() / 'my models.ini'
            selected.write_text('[models.example]\nmodel = hf.co/owner/example:Q4_K_M\n', encoding='utf-8')
            with patch('loaf.cli.execute_plan') as run, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.invoke('download', '--version', 'v2', '--models-file', str(selected)), 0)
            self.assertEqual(run.call_args.args[0].project_name, 'loaf-v2')
            self.assertIn('--version',output.getvalue())
            self.assertIn('v2',output.getvalue())
            self.assertNotIn('--models-file', output.getvalue())
            self.assertTrue(output.getvalue().strip().endswith(' offline'))
            self.assertIn(str(self.project), output.getvalue())
            self.assertIn('loaf',output.getvalue())

    def test_missing_docker_is_reported_without_traceback(self):
        with patch('loaf.cli.execute_plan',side_effect=FileNotFoundError('Docker executable not found')), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.invoke('offline'), 1)
        self.assertIn('Docker executable not found',errors.getvalue())
        self.assertNotIn('Traceback',errors.getvalue())

    def test_interrupted_cli_returns_130(self):
        with patch('loaf.cli.execute_plan',side_effect=KeyboardInterrupt), redirect_stderr(io.StringIO()):
            self.assertEqual(self.invoke('offline'), 130)

    def test_signal_exit_is_normalized_to_one(self):
        with patch('loaf.cli.execute_plan',side_effect=subprocess.CalledProcessError(-9,'docker')), redirect_stderr(io.StringIO()):
            self.assertEqual(self.invoke('offline'), 1)

    def test_nonpositive_wait_timeout_is_rejected_before_docker(self):
        for value in ('0','-1'):
            with self.subTest(value=value), patch('loaf.cli.execute_plan') as mocked, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.invoke('offline', '--wait-timeout', value)
                self.assertEqual(raised.exception.code,2)
                mocked.assert_not_called()

    def test_invalid_file_is_rejected_before_docker(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'invalid.ini'
            path.write_text('[models.bad]\nmodel = hf.co/../invalid\n')
            with patch('loaf.cli.execute_plan') as mocked, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    self.invoke('download', '--models-file', str(path))
            self.assertEqual(exc.exception.code, 2)
            mocked.assert_not_called()

    def test_offline_does_not_require_model_list(self):
        # A transferred cache/config volume is sufficient to boot an offline host.
        with patch('loaf.cli.execute_plan') as mocked, redirect_stdout(io.StringIO()):
            self.assertEqual(self.invoke('offline', '--models-file', 'not-present', '--dry-run'), 0)
        self.assertEqual(mocked.call_args.args[1], cli.plan('offline'))

    def test_stop_removes_containers_by_default_and_allows_keeping_them(self):
        for flags, removes in (
            ([], True), (["-n"], False), (["--no-remove-containers"], False),
            (["--remove-containers"], True),
        ):
            with self.subTest(flags=flags), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["stop", "--dry-run", "--project-dir", str(ROOT), *flags]), 0)
                self.assertIn("Stop all deployment services", output.getvalue())
                self.assertEqual("Remove stopped deployment containers" in output.getvalue(), removes)
        step = cli.plan("stop", terminal_server=True)[-1]
        self.assertIn("terminal", step.profiles)
        self.assertEqual(step.arguments, ("rm", "--force"))

    def test_cpu_serving_uses_the_override_only_when_requested(self):
        for mode in ("online", "offline"):
            for cpu in (False, True):
                with (
                    self.subTest(mode=mode, cpu=cpu),
                    patch("loaf.cli.execute_plan") as execute,
                    redirect_stdout(io.StringIO()),
                ):
                    options = ["--cpu"] if cpu else []
                    self.assertEqual(self.invoke(mode, "--dry-run", *options), 0)
                compose, steps = execute.call_args.args
                files = (self.project / "compose.yml",)
                self.assertEqual(compose.files, files + ((DATA / "compose.cpu.yml",) if cpu else ()))
                self.assertEqual("Ollama on CPU" in steps[-1].description, cpu)

    def test_misplaced_arguments_and_options_name_the_supported_commands(self):
        for arguments, message in (
            ((), "A command is required"),
            (("offline", "extra"), "Unexpected arguments for offline"),
            (("offline", "systemd"), "Use the integration name with loaf integration"),
            (("integration",), "integration requires a name"),
            (("package",), "package requires a target: binary, debian or both"),
            (("package", "images"), "invalid package target"),
            (("package", "binary", "binary"), "Specify each package target once"),
            (("package", "debian", "--version", "v2"), "Unexpected option for package"),
            (("package", "binary", "--attach"), "--attach is available for Compose"),
            (("package", "debian"), "package requires the loaf source directory"),
            (("backup", "logs"), "Unknown archive type logs"),
            (("backup", "images", "images"), "Specify each archive type once"),
            (("offline", "-n"), "only supported by stop"),
            (("restore", "--verify"), "--verify is only supported by backup"),
            (("backup", "images", "--verify"), "--verify checks the whole archive"),
            (("backup", "volumes", "--no-terminal-server"), "creating a backup that includes images"),
            (("backup", "--delete"), "--delete is only supported by restore"),
            (("restore", "config", "--delete"), "--delete applies to the models type"),
            (("build", "--cpu"), "--cpu is only supported by"),
            (("restore", "--models-only"), "unrecognized arguments: --models-only"),
            (("setup", "--all"), "unrecognized arguments: --all"),
            (("offline", "--with-docker-deps"), "--with-docker-deps is only supported by setup"),
            (("offline", "--test-local-repository"), "only supported by setup and install"),
            (("setup", "--terminal-server-key", "--test-local-repository"), "--terminal-server-key is for key setup only"),
            (("setup", "--test-local-repository", "--with-docker-deps"), "only creates the test repository"),
            (("link-docker-project",), "invalid choice: 'link-docker-project'"),
            (("offline", "--link-docker-project"), "only supported by setup and install"),
            (("setup", "--link-docker-project", "--terminal-server-key"), "only links the Docker project"),
            (("setup", "tests", "--link-docker-project"), "only links the Docker project"),
            (("setup", "--link-docker-project", "--config-dir", "config"), "only links the Docker project"),
            (("setup", "--link-docker-project", "--version", "v2"), "Omit --version"),
            (("offline", "--badges"), "--badges is only supported by test"),
            (("offline", "--full"), "--full is only supported by clean"),
            (("offline", "--backup-dir", "backup"), "--backup-dir is only supported by backup"),
            (("backup", "--verify", "--attach"), "--attach is available for Compose"),
            (("clean", "--version", "v2"), "Unexpected option for clean"),
            (("restore", "--backup-dir", "a", "--archive", "b"), "Use either --backup-dir or --archive"),
            (("offline", "--archive", "a.tar.gz"), "--archive is for standalone setup"),
            (("offline", "--import"), "--import/--no-import are only supported by restore"),
            (("restore", "images", "--import"), "--import requires the models type"),
            (("backup", "images", "--export"), "--export requires the models type"),
            (("build", "--wait-timeout", "5"), "--wait-timeout is only supported by commands that start services"),
            (("setup", "extra"), "setup accepts only the tests argument"),
            (("setup", "tests", "--test-local-repository"), "setup tests installs only the test packages"),
            (("install", "tests", "--archive", "backup"), "setup tests installs only the test packages"),
            (("setup", "tests", "--version", "v2"), "Unexpected option for setup"),
        ):
            with (
                self.subTest(arguments=arguments), redirect_stderr(io.StringIO()) as errors,
                patch("loaf.cli.subprocess.run", side_effect=AssertionError("subprocess")) as run,
                self.assertRaises(SystemExit) as raised,
            ):
                self.invoke(*arguments)
            self.assertEqual(raised.exception.code, 2)
            self.assertIn(message, errors.getvalue())
            run.assert_not_called()

    def test_cpu_option_is_rejected_when_creating_a_backup(self):
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as raised:
            self.invoke("backup", "--cpu")
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--cpu is only supported", errors.getvalue())

    def test_no_terminal_server_only_applies_when_creating_a_backup(self):
        for arguments in (("restore",), ("backup", "--verify")):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as raised:
                    self.invoke(*arguments, "--no-terminal-server")
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("--no-terminal-server is only supported when creating a backup", errors.getvalue())

    def test_restore_dry_run_from_another_directory_needs_no_selection_file(self):
        with tempfile.TemporaryDirectory() as folder:
            child = subprocess.run([
                sys.executable, '-m', 'loaf', '--project-dir', str(self.project), 'restore', 'models',
                '--backup-dir', 'archives', '--dry-run',
            ], cwd=folder, capture_output=True, text=True)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn(f'Restore models from the newest backup in {Path(folder).resolve() / "archives"}', child.stdout)
        self.assertNotIn('+ ', child.stdout)

    def test_online_rejects_previous_download_options_before_docker(self):
        for options in (['--build'], ['--models-file', 'models.txt']):
            with self.subTest(options=options), patch('loaf.cli.execute_plan') as execute, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                self.invoke('online', *options)
            self.assertEqual(raised.exception.code, 2)
            execute.assert_not_called()

    def test_docker_failure_is_a_clean_nonzero_exit(self):
        with patch('loaf.cli.execute_plan', side_effect=subprocess.CalledProcessError(7, 'docker')), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(self.invoke('offline'), 7)

    def test_help_and_dry_run_work_from_another_directory(self):
        (self.project / "config").mkdir()
        (self.project / "config/models.ini").write_text("[models.default]\nmodel = hf.co/example/default:Q4_K_M\n", encoding="utf-8")
        with tempfile.TemporaryDirectory() as folder:
            child = subprocess.run([
                sys.executable, '-m', 'loaf', '--project-dir', str(self.project), 'download', '--dry-run',
            ], cwd=folder, capture_output=True, text=True)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn('hf.co/example/default:Q4_K_M', child.stdout)


if __name__ == '__main__':
    unittest.main()
