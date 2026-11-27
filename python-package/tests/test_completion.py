"""Bash completion: generated words, behavior in Bash, and per-user installation and removal."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from loaf import cli, completion


BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "Bash is required for completion tests.")
class CompletionScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = TemporaryDirectory()
        cls.root = Path(cls.folder.name)
        cls.script = cls.root / "loaf.bash"
        cls.script.write_text(completion.script(), encoding="utf-8")
        (cls.root / "project").mkdir()
        (cls.root / "models.ini").touch()

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def complete(self, *words):
        """Complete the last word as Bash would; '=' is a word of its own, as Bash splits it."""
        program = (f'source "$1"; compopt() {{ :; }}; COMP_WORDS=(loaf "${{@:2}}"); COMP_CWORD={len(words)}; '
                   '_loaf; printf "%s\\n" "${COMPREPLY[@]}"')
        result = subprocess.run([BASH, "--norc", "-c", program, "bash", str(self.script), *words],
                                capture_output=True, text=True, cwd=self.root, check=True)
        self.assertEqual(result.stderr, "")
        return [line for line in result.stdout.splitlines() if line]

    def test_script_is_valid_bash_and_marks_its_origin(self):
        subprocess.run([BASH, "-n", str(self.script)], check=True)
        self.assertTrue(completion.owned(self.script))
        self.assertIn("It is removed with the Debian package.", completion.script(system=True).splitlines()[0])

    def test_commands_subcommands_and_types(self):
        for words, expected in (
            (("",), list(cli.COMMAND_HELP)),
            (("doc",), ["docs", "docker"]),
            (("docker", ""), ["ps", "rm"]),
            (("docker", "rm", ""), ["all", "containers", "images", "volumes", "networks"]),
            (("integration", "sys"), ["systemd"]),
            (("completion", ""), ["install", "remove", "print"]),
            (("docs", "rea"), ["readme"]),
            (("package", ""), ["binary", "debian"]),
            (("setup", ""), ["tests"]),
            (("backup", "models", "vo"), ["volumes"]),
        ):
            with self.subTest(words=words):
                self.assertEqual(self.complete(*words), expected)

    def test_options_follow_the_command_and_skip_option_values(self):
        self.assertEqual(self.complete("--"), ["--help", "--project-dir", "--system", "--verbose"])
        self.assertEqual(self.complete("docker", "rm", "--"),
                         ["--config-dir", "--namespace", "--dry-run", "--yes", "--help", "--project-dir", "--system",
                          "--verbose"])
        self.assertIn("--no-remove-containers", self.complete("stop", "-"))
        self.assertNotIn("-n", self.complete("stop", "-"))
        # A command without positional words offers its options right away.
        self.assertIn("--wait-timeout", self.complete("offline", ""))
        self.assertEqual(self.complete("--project-dir", "/tmp", "restore", "mo"), ["models"])
        self.assertEqual(self.complete("backup", "--namespace", "=", "v1", "vol"), ["volumes"])
        self.assertEqual(self.complete("frobnicate", "--h"), ["--help"])

    def test_option_values_complete_directories_files_or_nothing(self):
        self.assertEqual(self.complete("--project-dir", "pro"), ["project"])
        self.assertEqual(self.complete("--project-dir", "mod"), [])
        self.assertEqual(self.complete("download", "--models-file", "mod"), ["models.ini"])
        self.assertEqual(self.complete("download", "--models-file", "=", "mod"), ["models.ini"])
        self.assertEqual(self.complete("offline", "--namespace", ""), [])

    @unittest.skipUnless(completion.BASH_COMPLETION.is_file(), "bash-completion is not installed.")
    def test_bash_completion_loads_the_user_file_on_first_use(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, XDG_DATA_HOME=folder), \
                patch.object(completion.assets, "debian_package", return_value=False):
            completion.install()
            program = f'source {completion.BASH_COMPLETION}; __load_completion loaf; complete -p loaf'
            result = subprocess.run([BASH, "--norc", "-c", program], capture_output=True, text=True,
                                    env={**os.environ, "XDG_DATA_HOME": folder})
        self.assertEqual(result.stdout.strip(), "complete -F _loaf loaf", result.stderr)


class CompletionInstallTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        for change in (patch.dict(os.environ, XDG_DATA_HOME=str(self.home)),
                       patch.object(completion.assets, "debian_package", return_value=False)):
            change.start()
            self.addCleanup(change.stop)
        os.environ.pop("BASH_COMPLETION_USER_DIR", None)
        self.target = self.home / "bash-completion/completions/loaf"

    def invoke(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["completion", *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_install_and_remove_manage_only_their_own_file(self):
        code, output, _ = self.invoke("install")
        self.assertEqual(code, 0)
        self.assertIn(f"Installed Bash completion in {self.target}.", output)
        self.assertEqual(self.target.read_text(encoding="utf-8"), completion.script())
        self.assertEqual(self.invoke("install")[0], 0)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])
        code, output, _ = self.invoke("remove")
        self.assertEqual(code, 0)
        self.assertIn(f"Removed {self.target}.", output)
        self.assertFalse(self.target.exists())
        self.assertIn("No loaf Bash completion", self.invoke("remove")[1])

        self.target.write_text("# someone else's completion\n", encoding="utf-8")
        for command in ("install", "remove"):
            with self.subTest(command=command):
                code, _, errors = self.invoke(command)
                self.assertEqual(code, 1)
                self.assertIn("was not created by loaf", errors)
                self.assertEqual(self.target.read_text(encoding="utf-8"), "# someone else's completion\n")

    def test_bash_completion_user_directory_is_respected(self):
        custom = self.home / "custom"
        with patch.dict(os.environ, BASH_COMPLETION_USER_DIR=f"{custom}:/elsewhere"):
            self.invoke("install")
        self.assertTrue(completion.owned(custom / "completions/loaf"))
        self.assertFalse(self.target.exists())

    def test_debian_package_provides_the_system_completion(self):
        with patch.object(completion.assets, "debian_package", return_value=True):
            code, output, _ = self.invoke("install")
            self.assertEqual(code, 0)
            self.assertIn(f"The loaf Debian package provides Bash completion in {completion.SYSTEM_PATH}.", output)
            self.assertFalse(self.target.exists())
            self.assertIn(f"keeps {completion.SYSTEM_PATH}", self.invoke("remove")[1])

    def test_print_and_invalid_arguments(self):
        code, output, _ = self.invoke("print")
        self.assertEqual((code, output), (0, completion.script() + "\n"))
        self.assertFalse(self.target.exists())
        for arguments, message in ((), "completion requires a command: install, remove or print."),\
                (("enable",), "invalid completion command. Use install, remove or print."),\
                (("install", "--dry-run"), "Unexpected option for completion."):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                self.invoke(*arguments)


if __name__ == "__main__":
    unittest.main()
