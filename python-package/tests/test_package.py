# test_package.py - Installed entry point, cwd selection and unittest delegation.
# Exercise the real virtualenv command in disposable project directories. Fixture
# suites are tiny and separate from this suite, preventing recursive `loaf test`.

from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.metadata
from io import StringIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest.mock import patch

from loaf import badges, cli
from tests import DATA, ROOT


# --- The installed command must belong to the interpreter running these tests ---
COMMAND = Path(sysconfig.get_path('scripts')) / ('loaf.exe' if sys.platform == 'win32' else 'loaf')


class PackageTests(unittest.TestCase):
    def invoke(self, folder, *arguments):
        return subprocess.run(
            [str(COMMAND), *arguments], cwd=folder, capture_output=True, timeout=60,
        )

    def project(self, folder):
        root = Path(folder)
        (root/'compose.yml').write_text('services: {}\n', encoding='utf-8')
        (root / 'config').mkdir()
        (root / 'config/models.ini').write_text('[models.default]\nmodel = hf.co/test/cwd-selected:Q4_K_M\n', encoding='utf-8')
        return root

    def fixture_suite(self, folder, *, fails=False):
        # A real unittest package lets discovery verify its top-level path without
        # loading the repository's Docker/GPU tests or recursively loading this file.
        tests = Path(folder) / 'python-package/tests'
        tests.mkdir(parents=True)
        (tests/'__init__.py').write_text('', encoding='utf-8')
        (tests/'test_fixture.py').write_text(
            'import sys\n'
            'import unittest\n'
            'class FixtureTests(unittest.TestCase):\n'
            '    def test_example(self):\n'
            '        print("fixture stdout")\n'
            '        print("fixture stderr", file=sys.stderr)\n'
            f'        self.assertTrue({not fails!r})\n'
            '    @unittest.skip("fixture skip")\n'
            '    def test_skipped(self): pass\n', encoding='utf-8',
        )
        return tests

    # --- Distribution registration and the two equivalent CLI entry points ---
    def test_distribution_is_editable_and_registers_loaf_without_runtime_dependencies(self):
        distribution = importlib.metadata.distribution('local-ai-ferry')
        entry, = [entry for entry in distribution.entry_points if entry.group == 'console_scripts']
        self.assertEqual(entry.name, 'loaf')
        self.assertIs(entry.load(), cli.main)
        self.assertFalse(distribution.requires)
        self.assertTrue(json.loads(distribution.read_text('direct_url.json'))['dir_info']['editable'])
        self.assertTrue(Path(cli.__file__).resolve().is_relative_to(ROOT/'python-package'/'src'))

    def test_installed_package_contains_repository_license_files(self):
        distribution = importlib.metadata.distribution("local-ai-ferry")
        self.assertEqual(distribution.metadata["License-Expression"], "BSD-3-Clause")
        for name in ("LICENSE.txt", "THIRD_PARTY_NOTICES.txt"):
            with self.subTest(file=name):
                license_file, = [path for path in distribution.files if path.parts[-2:] == ("licenses", name)]
                self.assertEqual(license_file.read_binary(), (ROOT / name).read_bytes())

    def test_console_and_module_help_match_outside_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            console = self.invoke(folder, '--help')
            module = subprocess.run([sys.executable,'-m','loaf','--help'], cwd=folder, capture_output=True)
        self.assertEqual(console.returncode, 0, console.stderr)
        self.assertEqual(module.returncode, 0, module.stderr)
        self.assertEqual(console.stdout, module.stdout)
        self.assertIn(b'usage: loaf', console.stdout)

    # --- Paths come from cwd or an explicit override, never the installation ---
    def test_every_workflow_defaults_to_current_directory(self):
        with tempfile.TemporaryDirectory(prefix='loaf project with spaces ') as folder:
            root = self.project(folder)
            # Restore previews need a backup; without one they fail like a real restore.
            for mode in (*cli.MODES, "backup"):
                with self.subTest(mode=mode):
                    result = self.invoke(folder, mode, '--dry-run')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    output = result.stdout.decode('utf-8')
                    self.assertIn(str(root.resolve()), output)
                    if mode == 'download':
                        self.assertIn('hf.co/test/cwd-selected:Q4_K_M', output)
            result = self.invoke(folder, 'restore', '--dry-run')
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f'No backup found in {root.resolve() / "backup"}'.encode(), result.stderr)
            self.assertFalse((root/'.run.lock').exists())

    def test_verbose_dry_run_prints_copyable_compose_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.project(folder)
            result = self.invoke(folder, '-v', 'offline', '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = result.stdout.splitlines()
            self.assertEqual(len(commands), 3)
            self.assertTrue(all(line.startswith(b'+ ') and b'LOAF_NAMESPACE' in line for line in commands))
            self.assertIn(b'docker', commands[0])
            self.assertIn(b'--no-build', commands[-1])
            self.assertFalse((root / '.run.lock').exists())

    def test_explicit_project_directory_works_from_another_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.project(folder)
            result = self.invoke(ROOT, 'download', '--project-dir', str(root), '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'hf.co/test/cwd-selected:Q4_K_M', result.stdout)

    def test_explicit_model_ini_replaces_the_project_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.project(folder)
            (root / 'config/models.ini').write_text('[models.default]\nmodel = hf.co/test/default\n', encoding='utf-8')
            (root / 'other models.ini').write_text('[models.chat]\nmodel = hf.co/test/alternate\n', encoding='utf-8')
            default = self.invoke(folder, 'download', '--dry-run')
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertIn(b'hf.co/test/default', default.stdout)
            self.assertNotIn(b'hf.co/test/cwd-selected', default.stdout)

            alternate = self.invoke(folder, 'download', '--models-file', 'other models.ini', '--dry-run')
            self.assertEqual(alternate.returncode, 0, alternate.stderr)
            self.assertIn(b'hf.co/test/alternate', alternate.stdout)
            self.assertNotIn(b'hf.co/test/default', alternate.stdout)

    def test_unusable_ini_fails_before_any_steps(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.project(folder)
            (root / 'config/models.ini').write_text('[models.default]\nmodel =\n', encoding='utf-8')
            for options, message in (
                ((), b'invalid model specifier'),
                (('--models-file', 'missing.ini'), b'Cannot read model list'),
            ):
                with self.subTest(options=options):
                    result = self.invoke(folder, 'download', '--dry-run', *options)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(message, result.stderr)
                    self.assertEqual(result.stdout, b'')
            self.assertFalse((root / '.run.lock').exists())

    def test_config_directory_can_be_selected_independently(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.project(folder)
            alternate = root / "custom config"
            alternate.mkdir()
            (alternate / "config.ini").write_text("[launcher]\nmodels_file = small.ini\n")
            (alternate / "small.ini").write_text("[models.test]\nmodel = hf.co/test/small\nname = Small-Chat\n")
            result = self.invoke(folder, '--config-dir', alternate.name, 'download', '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'Tag hf.co/test/small as Small-Chat:latest', result.stdout)
            self.assertNotIn(b'cwd-selected', result.stdout)

    def test_missing_compose_fails_before_docker(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.invoke(folder, 'offline', '--dry-run')
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'No compose.yml', result.stderr)
        self.assertEqual(result.stdout, b'')

    def test_checkout_uses_bundled_compose_and_model_defaults_without_a_link(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'python-package/src/loaf/data'
            shutil.copytree(DATA, data)
            result = self.invoke(folder, '-v', 'download', '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(data / 'compose.yml').encode(), result.stdout)
            self.assertIn(cli.read_models(DATA / 'config/models.ini')[0].source.encode(), result.stdout)

    def test_link_docker_project_previews_and_reuses_the_link(self):
        with tempfile.TemporaryDirectory(prefix='loaf workspace ') as folder:
            root = Path(folder)
            link = root / 'docker-project'
            preview = self.invoke(folder, 'setup', '--link-docker-project', '--dry-run')
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertFalse(link.is_symlink())
            result = self.invoke(folder, 'setup', '--link-docker-project')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), DATA)
            self.assertTrue((link / 'docker/open-webui/Dockerfile').is_file())
            self.assertTrue((link / 'config/models.ini').is_file())
            cpu_plan = self.invoke(folder, 'offline', '--cpu', '--dry-run', '-v')
            self.assertEqual(cpu_plan.returncode, 0, cpu_plan.stderr)
            self.assertIn(str(DATA / 'compose.cpu.yml').encode(), cpu_plan.stdout)
            again = self.invoke(folder, 'install', '--link-docker-project')
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn(b'already linked', again.stdout)
            plan = self.invoke(folder, 'download', '--dry-run')
            self.assertEqual(plan.returncode, 0, plan.stderr)
            self.assertIn(cli.read_models(DATA / 'config/models.ini')[0].source.encode(), plan.stdout)
            self.assertFalse((root / '.run.lock').exists())

    def test_link_docker_project_preserves_an_existing_path(self):
        with tempfile.TemporaryDirectory() as folder:
            link = Path(folder) / 'docker-project'
            link.symlink_to('missing', target_is_directory=True)
            result = self.invoke(folder, 'setup', '--link-docker-project')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(b'already exists', result.stderr)
            self.assertEqual(link.readlink(), Path('missing'))

    # --- Standard unittest supplies the summary, diagnostics and failure status ---
    def test_test_command_prints_summary_and_skip_count(self):
        with tempfile.TemporaryDirectory() as folder:
            self.fixture_suite(folder)
            result = self.invoke(folder, 'test')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'Ran 2 tests', result.stderr)
        self.assertIn(b'OK (skipped=1)', result.stderr)
        self.assertNotIn(b'test_example', result.stderr)
        self.assertEqual(result.stdout, b'')
        self.assertNotIn(b'fixture stderr', result.stderr)

    def test_verbose_and_repeated_v_show_test_names(self):
        with tempfile.TemporaryDirectory() as folder:
            self.fixture_suite(folder)
            # argparse accepts grouped repetitions of the same boolean short flag;
            # extra v's intentionally add no new verbosity machinery.
            for flag in ('-v','-vv','-vvv'):
                with self.subTest(flag=flag):
                    result = self.invoke(folder, 'test', flag)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(b'test_example', result.stderr)
                    self.assertEqual(result.stdout, b'')
                    self.assertNotIn(b'fixture stderr', result.stderr)

    def test_test_command_preserves_failure_status_and_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            self.fixture_suite(folder, fails=True)
            result = self.invoke(folder, 'test')
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'FAILED (failures=1, skipped=1)', result.stderr)
        self.assertIn(b'AssertionError', result.stderr)
        self.assertIn(b'fixture stdout', result.stderr)
        self.assertIn(b'fixture stderr', result.stderr)

    def test_badges_refresh_only_groups_with_executed_tests(self):
        with tempfile.TemporaryDirectory() as folder:
            tests = self.fixture_suite(folder)
            (tests / "test_docker.py").write_text(
                "import unittest\n"
                "class DockerTests(unittest.TestCase):\n"
                "    def test_volumes(self): pass\n"
                "    def test_unavailable_images(self): self.skipTest('no images')\n",
                encoding="utf-8",
            )
            (tests / "test_docker_gpu.py").write_text(
                "import unittest\n"
                "@unittest.skip('no GPU')\n"
                "class GpuWorkflowTests(unittest.TestCase):\n"
                "    def test_inference(self): pass\n"
                "class CpuWorkflowTests(unittest.TestCase):\n"
                "    cpu = True\n"
                "    def test_inference(self): pass\n",
                encoding="utf-8",
            )
            project = Path(folder)
            badges = project / "docs/badges"
            badges.mkdir(parents=True)
            gpu = badges / "gpu.svg"
            gpu.write_bytes(b"<svg>previous GPU badge</svg>")
            report = project / "docs/verification-record.md"
            history = "# Verification record\n\n## Initial run\n\nGPU tests passed.\n"
            report.write_text(history, encoding="utf-8")
            readme = project / "README.md"
            old_gpu = "[![Previous GPU](docs/badges/gpu.svg)](docs/verification-record.md#initial-run)"
            readme.write_text(
                "# Fixture\n\n"
                "[![Previous CLI](docs/badges/cli.svg)](docs/verification-record.md#initial-run)\n"
                "[![Previous Docker](docs/badges/docker.svg)](docs/verification-record.md#initial-run)\n"
                f"{old_gpu}\n",
                encoding="utf-8",
            )

            result = self.invoke(ROOT, "test", "-v", "--badges", "--project-dir", folder)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"Ran 6 tests", result.stderr)
            self.assertIn(b"OK (skipped=3)", result.stderr)
            self.assertIn(b"Updated test badges for CLI baseline, Docker.", result.stdout)
            self.assertCountEqual([path.name for path in badges.iterdir()], ["cli.svg", "docker.svg", "gpu.svg"])
            self.assertIn("1/1 passed", (badges / "cli.svg").read_text(encoding="utf-8"))
            self.assertIn("2/2 passed", (badges / "docker.svg").read_text(encoding="utf-8"))
            self.assertEqual(gpu.read_bytes(), b"<svg>previous GPU badge</svg>")
            self.assertIn(old_gpu, readme.read_text(encoding="utf-8"))
            self.assertNotIn("Previous CLI", readme.read_text(encoding="utf-8"))
            entry = report.read_text(encoding="utf-8")
            self.assertIn("| CLI baseline | 1/1 |\n| Docker | 2/2 |", entry)
            self.assertTrue(entry.endswith(history.split("\n\n", 1)[1]))

    def test_badges_count_subtests_once_and_preserve_test_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            tests = self.fixture_suite(folder)
            (tests / "test_fixture.py").write_text(
                "import unittest\n"
                "class FixtureTests(unittest.TestCase):\n"
                "    def test_failure(self):\n"
                "        with self.subTest(value=1): self.assertTrue(True)\n"
                "        with self.subTest(value=2): self.fail('failed subtest')\n"
                "    def test_partial_skip(self):\n"
                "        with self.subTest(value=1): self.assertTrue(True)\n"
                "        with self.subTest(value=2): self.skipTest('skipped subtest')\n"
                "    def test_skipped(self):\n"
                "        with self.subTest(value=1): self.skipTest('no tests ran')\n",
                encoding="utf-8",
            )
            result = self.invoke(folder, "test", "--badges")
            self.assertEqual(result.returncode, 1)
            self.assertIn(b"failed subtest", result.stderr)
            badge = Path(folder) / "docs/badges/cli.svg"
            svg = badge.read_text(encoding="utf-8")
            self.assertIn("1/2 passed", svg)
            self.assertIn("#b42318", svg)

    def test_badges_show_each_run_when_the_pass_count_stays_the_same(self):
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        result = unittest.TextTestRunner(stream=StringIO(), resultclass=badges.BadgeResult).run(suite)
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(badges, "datetime") as clock,
            redirect_stdout(StringIO()),
        ):
            project = Path(folder)
            readme = project / "README.md"
            readme.write_text("[![CLI](docs/badges/cli.svg)](docs/verification-record.md)\n", encoding="utf-8")
            svg = project / "docs/badges/cli.svg"
            for second, suffix in ((3, ""), (4, ""), (4, " #2")):
                with self.subTest(second=second, suffix=suffix):
                    now = datetime(2026, 11, 23, 16, 2, second, tzinfo=timezone.utc)
                    clock.now.return_value.astimezone.return_value = now
                    badges.update_badges(project, result)
                    timestamp = f"2026-11-23 16:02:{second:02d}{suffix}"
                    self.assertIn(f">{timestamp}</text>", svg.read_text(encoding="utf-8"))
                    self.assertIn(f"{timestamp} — 1/1 passed", readme.read_text(encoding="utf-8"))

    def test_badges_write_nothing_when_all_tests_are_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            tests = self.fixture_suite(folder)
            (tests / "test_fixture.py").write_text(
                "import unittest\n"
                "@unittest.skip('unavailable')\n"
                "class FixtureTests(unittest.TestCase):\n"
                "    def test_skipped(self): pass\n",
                encoding="utf-8",
            )
            result = self.invoke(folder, "test", "--badges")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"No executed tests", result.stdout)
            self.assertFalse((Path(folder) / "docs").exists())

    def test_missing_test_directory_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.invoke(folder, 'test')
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'No tests directory', result.stderr)

    def test_empty_suite_is_reported_as_unsuccessful(self):
        with tempfile.TemporaryDirectory() as folder:
            suite = Path(folder) / "python-package/tests"
            suite.mkdir(parents=True)
            (suite / "__init__.py").touch()
            result = self.invoke(folder, "test")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn(b"No tests discovered", result.stderr)

    def test_test_command_rejects_workflow_options(self):
        with tempfile.TemporaryDirectory() as folder:
            self.fixture_suite(folder)
            result = self.invoke(folder, 'test', '--dry-run')
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'test supports', result.stderr)
