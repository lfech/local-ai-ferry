"""Exercise the status command without a running Docker daemon."""

from contextlib import redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from loaf import cli, readiness, transfer
from tests import ROOT


class ReadinessTests(unittest.TestCase):
    def test_model_probe_follows_v2_manifest_links_to_existing_blobs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            blobs = root / "models/blobs"
            blobs.mkdir(parents=True)
            blob = blobs / "sha256-test"
            blob.touch()
            tag = root / "models/manifests-v2/ollama.com/library/test/latest"
            tag.parent.mkdir(parents=True)
            tag.symlink_to("../../../../blobs/sha256-test")

            def output(_compose, command):
                if command[1] == "run":
                    subprocess.run(
                        ["/bin/sh", "-c", command[-1].replace("/check", str(root))],
                        check=True, capture_output=True,
                    )
                return ""

            with patch.object(readiness, "docker_output", side_effect=output):
                self.assertIn("local model data found", readiness.check_volume(None, "image", "ollama", models=True))
                blob.unlink()
                with self.assertRaises(subprocess.CalledProcessError):
                    readiness.check_volume(None, "image", "ollama", models=True)

    def test_status_checks_both_profiles_and_recognizes_the_deployments_port(self):
        models = "loaf-v2-ollama"
        webui = "loaf-v2-open-webui"
        terminal = "loaf/open-terminal:v2"
        downloader, backend, frontend = transfer.image_names("loaf-v2")
        port = {"host_ip": "127.0.0.1", "published": "3100", "protocol": "tcp", "target": 8080}
        services = {
            "ollama-online": {"image": downloader},
            "ollama-offline": {"image": backend},
            "ollama-connected": {"image": backend},
            "open-webui": {"image": frontend, "ports": [port]},
            "open-webui-online": {"image": frontend, "ports": [port]},
        }
        running = {"State": "running", "Publishers": [
            {"URL": "127.0.0.1", "PublishedPort": 3100, "Protocol": "tcp"},
        ]}
        compose_version = "2.30.3"
        engine_version = "26.1.5"
        terminal_built = True
        fail_gpu = False
        webui_running = True
        models_missing = False
        calls = []

        def output(compose, command):
            self.assertEqual(compose.project_name, "loaf-v2")
            calls.append(command)
            if command[1] == "version":
                return engine_version
            if "version" in command:
                return compose_version
            if "config" in command:
                return json.dumps({"services": services})
            if command[1:3] == ["image", "inspect"]:
                if command[-1] == terminal and not terminal_built:
                    raise subprocess.CalledProcessError(1, command, stderr="No such image")
                return "linux/amd64"
            if command[1:3] == ["volume", "inspect"]:
                if command[-1] == webui or (models_missing and command[-1] == models):
                    raise subprocess.CalledProcessError(1, command, stderr="no such volume")
                self.assertEqual(command[-1], models)
                return "[]"
            if command[1] == "run":
                self.assertIn("--read-only", command)
                self.assertIn("never", command)
                if "--gpus" in command:
                    if fail_gpu:
                        raise subprocess.CalledProcessError(
                            125, command, stderr="docker: OCI runtime create failed.\n"
                            "nvidia-container-cli: no adapters were found.\nSee 'docker run --help'.",
                        )
                    return "GPU 0: test GPU"
                self.assertIn(f"type=volume,source={models},target=/check,volume-nocopy,readonly", command)
                return ""
            if "ps" in command:
                return json.dumps(running) if webui_running else ""
            self.fail(f"Unexpected Docker command: {command}")

        with (
            TemporaryDirectory() as config_dir,
            patch.dict(os.environ, {"DOCKER_HOST": "unix:///test.sock", "DOCKER_CONTEXT": ""}),
            patch.object(readiness, "docker_output", side_effect=output),
            patch.object(cli.Compose, "execute") as execute,
            redirect_stdout(io.StringIO()) as status,
        ):
            options = ["status", "--project-dir", str(ROOT), "--config-dir", config_dir, "--namespace", "v2"]
            self.assertEqual(cli.main(options), 0)
            self.assertEqual(execute.call_args.args[0].arguments, ("ps", "--all"))
            self.assertIn("Deployment  loaf-v2", status.getvalue())
            self.assertIn("Containers across all profiles", status.getvalue())
            self.assertIn("Preflight passed.", status.getvalue())
            # Only an enabled terminal server uses the legacy bridge isolation of Docker Engine 26 and 27.
            self.assertIn("OK    Docker Engine — 26.1.5", status.getvalue())
            self.assertIn("\nConfiguration\n  OK    Launcher configuration\n  OK    Compose configuration", status.getvalue())
            self.assertNotIn(terminal, status.getvalue())
            for engine_version, expected in (("27.5.1", "OK"), ("28.0.0", "OK"), ("29.0.0", "OK")):
                with self.subTest(engine_version=engine_version):
                    status.seek(0)
                    status.truncate()
                    self.assertEqual(cli.main(options), 0)
                    self.assertIn(f"{expected:<4}  Docker Engine — {engine_version}", status.getvalue())
            self.assertIn("127.0.0.1:3100/tcp belongs to this deployment", status.getvalue())
            self.assertIn(f"Volume {webui} — will be created", status.getvalue())
            # Before the first download, the model volume's check names the commands that create it.
            models_missing = True
            status.seek(0)
            status.truncate()
            self.assertEqual(cli.main(options), 1)
            self.assertIn(f"FAIL  Volume {models} — The model volume does not exist yet. Run loaf download",
                          status.getvalue())
            models_missing = False
            self.assertEqual({call[-1] for call in calls if call[1:3] == ["image", "inspect"]},
                             {downloader, backend, frontend})
            for compose_version, expected, exit_code in (
                ("2.26.0", "FAIL", 1),
                ("2.26.1-4", "WARN", 0),
                ("v2.29.7", "WARN", 0),
                ("2.30.0", "OK", 0),
            ):
                with self.subTest(compose_version=compose_version):
                    status.seek(0)
                    status.truncate()
                    self.assertEqual(cli.main(options), exit_code)
                    self.assertIn(f"{expected:<4}  Docker Compose —", status.getvalue())

            status.seek(0)
            status.truncate()
            fail_gpu = True
            self.assertEqual(cli.main(options), 1)
            report = status.getvalue()
            self.assertIn(
                f"FAIL  NVIDIA GPU access for {backend} — Docker could not verify GPU access. "
                "Run 'loaf status --cpu' for CPU readiness.\n",
                report,
            )
            self.assertNotIn("OCI runtime create failed", report)
            self.assertNotIn("nvidia-container-cli", report)
            self.assertIn("Preflight failed.", report)

            status.seek(0)
            status.truncate()
            calls.clear()
            self.assertEqual(cli.main([*options, "--cpu"]), 0)
            report = status.getvalue()
            self.assertNotIn("NVIDIA GPU access", report)
            self.assertIn("OK    Inference mode — CPU", report)
            self.assertIn(f"Volume {models} — readable", report)
            self.assertIn("Preflight passed.", report)
            self.assertFalse(any("--gpus" in call for call in calls))

            fail_gpu = False
            webui_running = False
            with patch.object(readiness.socket, "socket") as socket:
                probe = socket.return_value.__enter__.return_value
                probe.bind.side_effect = OSError(errno.EADDRINUSE, "Address already in use")
                self.assertEqual(cli.main(options), 1)
            probe.bind.assert_called_once_with(("127.0.0.1", 3100))
            self.assertIn("Cannot bind 127.0.0.1:3100/tcp", status.getvalue())

            webui_running = True
            services["terminal-server"] = {"image": terminal}
            (Path(config_dir) / "config.ini").write_text("[terminal_server]\nenabled = true\n", encoding="utf-8")
            engine_version = "27.5.1"
            with patch.object(readiness.config, "read_terminal_secret", return_value="test"):
                for terminal_built, expected, exit_code in ((True, "OK", 0), (False, "FAIL", 1)):
                    with self.subTest(terminal_built=terminal_built):
                        status.seek(0)
                        status.truncate()
                        self.assertEqual(cli.main(options), exit_code)
                        report = status.getvalue()
                        config_section, images_section = report.split("\nLocal images and GPU\n", 1)
                        self.assertIn("OK    Terminal server key file", config_section)
                        self.assertIn(f"{expected:<4}  Local image {terminal}", images_section)
                        self.assertIn("WARN  Docker Engine — 27.5.1 — Open Terminal uses legacy bridge isolation",
                                      config_section)
                        if not terminal_built:
                            self.assertIn("No such image Run loaf build, or loaf restore images on an offline host.",
                                          images_section)
                        self.assertNotIn("\nTerminal\n", report)
