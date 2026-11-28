"""Check integration output through the CLI without starting services."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from loaf import cli
from tests import ROOT


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="loaf % $ project ")
        self.addCleanup(folder.cleanup)
        self.project = Path(folder.name).resolve()
        self.config = self.project / "config dir"
        self.config.mkdir()
        (self.config / "config.ini").write_text(
            "[launcher]\nnamespace = demo\n[webui]\nbind_address = ::\nport = 3100\n", encoding="utf-8",
        )

    def generate(self, mode, *options):
        with (
            redirect_stdout(StringIO()) as output,
            redirect_stderr(StringIO()) as errors,
            patch("loaf.cli.subprocess.run") as run,
        ):
            self.assertEqual(cli.main([
                "--project-dir", str(self.project), "--config-dir", str(self.config), "integration", mode, *options,
            ]), 0)
        run.assert_not_called()
        self.assertEqual(errors.getvalue(), "")
        self.assertFalse((self.project / ".run.lock").exists())
        return output.getvalue()

    def test_tether_prints_installation_and_release_link_without_compose(self):
        instructions = self.generate("tether")
        self.assertIn("https://github.com/open-webui/tether/releases\n", instructions)
        self.assertIn("sudo apt install ./tether_amd64.deb", instructions)
        self.assertIn("Connect to Open WebUI", instructions)

    def test_integration_requires_a_supported_name(self):
        for arguments, message in (
            (["integration"], "integration requires a name"),
            (["integration", "unknown"], "invalid choice"),
            (["status", "continue"], "with loaf integration"),
        ):
            with self.subTest(arguments=arguments), redirect_stderr(StringIO()) as errors:
                with self.assertRaises(SystemExit) as failure:
                    cli.main(arguments)
                self.assertEqual(failure.exception.code, 2)
                self.assertIn(message, errors.getvalue())

    @unittest.skipUnless(sys.platform == "linux", "systemd integration requires Linux")
    def test_systemd_uses_project_and_pins_the_namespace_for_start_and_stop(self):
        (self.project / "config").mkdir(exist_ok=True)
        (self.project / "config/config.ini").touch()
        unit = self.generate("systemd")
        self.assertIn("Type=exec\n", unit)
        self.assertNotIn("RemainAfterExit", unit)
        self.assertIn("WorkingDirectory=" + str(self.project).replace("%", "%%") + "\n", unit)
        for key, mode in (("ExecStart", "offline --attach"), ("ExecStop", "stop")):
            line, = [line for line in unit.splitlines() if line.startswith(key + "=")]
            self.assertIn('"--config-dir" "' + str(self.config).replace("%", "%%") + '"', line)
            self.assertIn('"--namespace" "demo"', line)
            self.assertTrue(line.endswith(" " + mode))
        self.assertIn('"--namespace="', self.generate("systemd", "--namespace="))
        # Like the packaged unit, it needs Docker and leaves the terminal server time to stop its chats.
        self.assertIn("Requires=docker.service\nAfter=docker.service network-online.target\n", unit)
        self.assertIn("TimeoutStopSec=90\n", unit)
        packaged = (ROOT / "packaging/debian/loaf.service").read_text(encoding="utf-8")
        self.assertIn("TimeoutStopSec=90\n", packaged)
        # A host without NVIDIA GPU access serves on CPU.
        start, = [line for line in self.generate("systemd", "--cpu").splitlines() if line.startswith("ExecStart=")]
        self.assertTrue(start.endswith(" offline --cpu --attach"), start)

    def test_continue_uses_the_webui_url_and_selected_ollama_ids(self):
        (self.config / "models.ini").write_text(
            "[models.chat]\nmodel = hf.co/owner/chat:Q4\nname = Chat\n"
            "[models.edit]\nmodel = hf.co/owner/edit\n"
            "[models.apply]\nmodel = hf.co/owner/apply:Q5\n", encoding="utf-8",
        )
        yaml = self.generate("continue")
        for model_id in ("Chat:latest", "hf.co/owner/edit:latest", "hf.co/owner/apply:Q5"):
            self.assertIn(f'model: "{model_id}"', yaml)
        self.assertEqual(yaml.count('apiBase: "http://[::1]:3100/api"'), 3)
        self.assertEqual(yaml.count("apiKey: REPLACE_WITH_OPEN_WEBUI_API_KEY"), 3)
        # Safe defaults: Continue's default roles and its own capability detection, with hints to widen them.
        self.assertEqual(yaml.count("    roles: [chat, edit, apply, summarize]\n"), 3)
        self.assertEqual(yaml.count("    # capabilities: [tool_use, image_input]\n"), 3)
        for role in ("embed", "rerank", "autocomplete"):
            self.assertNotIn(role, "".join(line for line in yaml.splitlines(True) if not line.startswith("#")))
        self.assertIn("# - autocomplete needs a code model", yaml)
        override = self.project / "override.ini"
        override.write_text("[models.default]\nmodel = hf.co/owner/override\n", encoding="utf-8")
        yaml = self.generate("continue", "--models-file", str(override))
        self.assertIn('model: "hf.co/owner/override:latest"', yaml)
        self.assertNotIn('model: "Chat:latest"', yaml)
