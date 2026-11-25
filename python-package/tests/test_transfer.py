"""Exercise the CLI archive workflow with a real checkout and simulated Docker."""

from base64 import b64encode
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import gzip
from hashlib import sha256
import io
from itertools import combinations
import json
import os
from pathlib import Path
import posixpath
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from loaf import cli, setup, transfer
from loaf.progress import Progress
from tests import DATA, ROOT


def tar_payload(name, content, **files):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for filename, data in {name: content, **files}.items():
            member = tarfile.TarInfo(filename)
            if data is None:
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            else:
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
    return payload.getvalue()


def image_payload(tags):
    manifest = json.dumps([{"RepoTags": tags, "Config": "platform.json"}]).encode()
    return tar_payload("manifest.json", manifest, **{"platform.json": b'{"os":"linux","architecture":"amd64"}'})


@unittest.skipUnless(shutil.which("git"), "Git is required for checkout backup.")
class TransferTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="loaf transfer ")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        (self.project / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        (self.project / "legacy").mkdir()
        (self.project / "legacy/offline-images.tar").write_bytes(b"tracked older backup")
        shutil.copyfile(ROOT / ".gitignore", self.project / ".gitignore")
        self.git("init", "--quiet")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "Initial commit")
        (self.project / "compose.yml").write_text("name: edited\nservices: {}\n", encoding="utf-8")
        (self.project / ".env").write_text("PRIVATE=value\n", encoding="utf-8")
        (self.project / "offline-images.tar").write_bytes(b"older backup")
        (self.project / "config").mkdir()
        self.wheels = self.project / setup.PYTHON_WHEEL_DIR
        self.wheels.mkdir(parents=True)
        package = self.project / setup.PYTHON_PACKAGE_DIR
        package.mkdir()
        (package / "pyproject.toml").touch()
        (self.wheels / "setuptools-example.whl").write_bytes(b"cached build wheel")
        self.launcher_wheel = self.wheels / "local_ai_ferry-example.whl"
        self.fail_wheel_export = False
        (self.project / ".venv").mkdir()
        (self.project / ".venv/pyvenv.cfg").write_text("fixture virtualenv", encoding="utf-8")
        self.toolkit = self.project / setup.TOOLKIT_DIR / "nvidia-container-toolkit_fixture.deb"
        self.toolkit.parent.mkdir(parents=True)
        self.toolkit.write_bytes(b"fixture deb")
        self.docker_cache = self.project / setup.DOCKER_DEB_DIR / "trixie-amd64"
        self.docker_cache.mkdir(parents=True)
        (self.docker_cache / "Packages").write_bytes(b"fixture package index")
        (self.docker_cache / "docker.io_fixture.deb").write_bytes(b"fixture Docker installer")
        (self.project / setup.INSTALL_CACHE / "scratch.txt").touch()
        self.backup_dir = self.project / "transfer disk"
        self.executable = self.project / "dist/loaf"
        self.executable.parent.mkdir()
        self.executable.write_bytes(transfer.EXECUTABLE_HEADER + b"\n__LOAF_PAYLOAD__\n\x1f\x8bfixture")
        executable_patch = patch.dict(os.environ, LOAF_RELEASE_EXECUTABLE=str(self.executable))
        executable_patch.start()
        self.addCleanup(executable_patch.stop)
        self.timestamp = datetime(2026, 11, 20, 15, 0)
        clock_patch = patch("loaf.transfer.datetime")
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.clock.now.return_value = self.timestamp
        self.bundle = self.backup_dir / f"20261120-1500_{transfer.BUNDLE_NAME}"
        self.archive = self.bundle / f"{self.bundle.name}.tar.gz"
        self.run = subprocess.run
        self.popen = subprocess.Popen
        self.docker_calls = []
        self.images = image_payload(transfer.IMAGES)
        self.terminal_image = None
        self.volumes = tar_payload("ollama/models/blobs/sha256:fixture", b"model\x00data")
        self.definitions = b""
        self.fail_volume_backup = False
        self.fail_volume_restore = False
        self.fail_modelfile_check = False
        self.fail_compose_step = None
        self.workspace_volumes = {}

    def git(self, *arguments):
        return subprocess.check_output(["git", *arguments], cwd=self.project)

    def docker(self, command, **kwargs):
        if command[0] != "docker":
            return self.run(command, **kwargs)
        self.docker_calls.append(command)
        if self.fail_compose_step and self.fail_compose_step in command:
            raise subprocess.CalledProcessError(23, command)
        output = b""
        if command[1:3] == ["image", "ls"] and command[-1] == f"reference={self.terminal_image}":
            output = b"terminal-image-id\n"
        if command[1:3] == ["volume", "ls"]:
            label, value = command[-1].removeprefix("label=").split("=", 1)
            output = "\n".join(name for name, metadata in self.workspace_volumes.items()
                               if metadata["Labels"].get(label) == value).encode()
        if command[1:3] == ["volume", "inspect"]:
            output = json.dumps([self.workspace_volumes[name] for name in command[3:]]).encode()
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def docker_stream(self, command, **kwargs):
        if command[0] != "docker":
            return self.popen(command, **kwargs)
        self.docker_calls.append(command)
        volume = command[1] == "run"
        sending = command[1:3] == ["image", "load"] or "-i" in command
        if volume:
            self.assertIn("none", command)
            self.assertIn("never", command)
        payload = self.volumes if volume else self.images
        if "loaf-check-modelfiles" in command:
            payload = self.definitions
            operation = "sys.exit(1)" if self.fail_modelfile_check else "sys.stdout.buffer.write(data)"
        elif volume and sending and self.fail_volume_restore:
            operation = 'print("Restore requires empty volumes.", file=sys.stderr); sys.exit(23)'
        elif volume and not sending and self.fail_volume_backup:
            operation = 'sys.exit(1)'
        elif sending:
            operation = 'sys.exit(sys.stdin.buffer.read() != data)'
        else:
            operation = 'sys.stdout.buffer.write(data)'
        # Exercise actual subprocess pipes in both directions without a daemon.
        code = "import base64,sys; data=base64.b64decode(sys.argv[1]); " + operation
        return self.popen([sys.executable, "-c", code, b64encode(payload).decode("ascii")], **kwargs)

    def invoke(self, mode, *arguments, custom=True, project=None):
        options = ["--backup-dir", str(self.backup_dir)] if custom else []
        self.output = io.StringIO()
        self.progress = io.StringIO()
        with (
            patch("loaf.cli.subprocess.run", side_effect=self.docker),
            patch("loaf.transfer.subprocess.Popen", side_effect=self.docker_stream),
            patch("loaf.transfer.toolkit_files", return_value=[self.toolkit]),
            patch("loaf.transfer.prepare_python_wheels", side_effect=self.export_wheels),
            redirect_stdout(self.output),
            redirect_stderr(self.progress),
        ):
            return cli.main(["--project-dir", str(project or self.project), mode, *options, *arguments])

    def export_wheels(self, project):
        self.assertEqual(project, self.project.resolve())
        if self.fail_wheel_export:
            raise setup.SetupError("Cannot export Python wheels from the local cache.")
        self.launcher_wheel.write_bytes(b"fresh launcher wheel")
        (self.wheels / setup.WHEEL_CACHE_RECORD).write_text("{}", encoding="utf-8")
        return self.wheels

    def test_cli_workflow_bundles_the_executable_and_restores_both_docker_payloads(self):
        for mode in ("build", "upgrade", "online", "offline"):
            self.assertEqual(self.invoke(mode, custom=False), 0)
        self.assertEqual(self.invoke("backup"), 0)
        self.assertTrue(self.launcher_wheel.is_file())
        self.assertEqual(list(self.backup_dir.iterdir()), [self.bundle])
        self.assertEqual((self.bundle / "loaf").read_bytes(), self.executable.read_bytes())
        self.assertTrue(os.access(self.bundle / "loaf", os.X_OK))
        self.assertEqual(transfer.read_bundle(self.bundle).archive, self.archive)
        progress = self.progress.getvalue()
        for phase in ("Archive project.tar", "Save volumes.tar", "Save images.tar",
                      "Compress project.tar", "Compress volumes.tar", "Compress images.tar"):
            self.assertIn(phase, progress)
        self.assertIn("100%", progress)
        self.assertNotIn("\r", progress)
        with tarfile.open(self.archive, "r:gz") as bundle:
            self.assertEqual(bundle.getnames(), [*transfer.MEMBERS, transfer.CHECKSUMS])
            self.assertEqual(bundle.extractfile("volumes.tar").read(), self.volumes)
            self.assertEqual(bundle.extractfile("images.tar").read(), self.images)
            with tarfile.open(fileobj=io.BytesIO(bundle.extractfile("project.tar").read())) as project:
                self.assertIn(".git/HEAD", project.getnames())
                self.assertEqual(project.extractfile("compose.yml").read(), b"name: edited\nservices: {}\n")
                self.assertIn(f"{setup.PYTHON_WHEEL_DIR}/setuptools-example.whl", project.getnames())
                self.assertIn(f"{setup.PYTHON_WHEEL_DIR}/{setup.WHEEL_CACHE_RECORD}", project.getnames())
                self.assertEqual(
                    project.extractfile(self.launcher_wheel.relative_to(self.project).as_posix()).read(),
                    b"fresh launcher wheel",
                )
                self.assertEqual(project.extractfile(".venv/pyvenv.cfg").read(), b"fixture virtualenv")
                self.assertEqual(
                    project.extractfile(self.toolkit.relative_to(self.project).as_posix()).read(), b"fixture deb",
                )
                docker_cache = self.docker_cache.relative_to(self.project)
                self.assertEqual(project.extractfile(f"{docker_cache}/Packages").read(), b"fixture package index")
                self.assertIn(f"{docker_cache}/docker.io_fixture.deb", project.getnames())
                self.assertNotIn(f"{setup.INSTALL_CACHE}/scratch.txt", project.getnames())
                self.assertNotIn(".env", project.getnames())
                self.assertIn("offline-images.tar", project.getnames())
                self.assertIn("legacy/offline-images.tar", project.getnames())
                self.assertFalse(any(name.startswith(("backup/", "transfer disk/")) for name in project.getnames()))
                with TemporaryDirectory() as recovered:
                    options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
                    project.extractall(recovered, **options)
                    recovered_toolkit = Path(recovered) / setup.TOOLKIT_DIR / self.toolkit.name
                    self.assertEqual(recovered_toolkit.read_bytes(), self.toolkit.read_bytes())
                    restored_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=recovered)
                self.assertEqual(restored_head, self.git("rev-parse", "HEAD"))
        self.assertEqual(self.invoke("restore"), 0)
        progress = self.progress.getvalue()
        for phase in ("Unpack project.tar", "Unpack volumes.tar", "Unpack images.tar", "Load images.tar", "Restore volumes.tar"):
            self.assertIn(phase, progress)
        self.assertEqual(
            [command[1:3] for command in self.docker_calls if command[1] == "image"],
            [["image", "ls"], ["image", "save"], ["image", "load"]],
        )
        self.assertFalse(list(self.project.glob(".loaf-restore-*")))

    def test_each_component_set_creates_and_restores_only_its_payloads(self):
        settings = self.project / "config/config.ini"
        for size in range(1, len(transfer.COMPONENTS) + 1):
            for components in combinations(transfer.COMPONENTS, size):
                with self.subTest(components=components):
                    settings.write_text("[webui]\nport = 8818\n")
                    before = set(self.backup_dir.glob("*"))
                    self.docker_calls.clear()
                    self.assertEqual(self.invoke("backup", *components), 0, self.progress.getvalue())
                    folder, = set(self.backup_dir.glob("*")) - before
                    bundle = transfer.read_bundle(folder)
                    with tarfile.open(bundle.archive, "r:gz") as archive:
                        members = {f"{part}.tar" for part in components}
                        self.assertEqual(set(archive.getnames()), members | {transfer.CHECKSUMS})
                        with archive.extractfile(transfer.CHECKSUMS) as manifest:
                            self.assertEqual(set(json.load(manifest)), members)
                    self.assertEqual(any(call[1:3] == ["image", "save"] for call in self.docker_calls),
                                     "images" in components)
                    self.assertEqual(any("stop" in call for call in self.docker_calls), "volumes" in components)
                    self.assertEqual(any(call[1] == "run" for call in self.docker_calls), "volumes" in components)

                    self.docker_calls.clear()
                    self.assertEqual(self.invoke("backup", "--verify", "--archive", str(folder), custom=False),
                                     0, self.progress.getvalue())
                    self.assertEqual(self.docker_calls, [])
                    settings.write_text("[webui]\nport = 8820\n")
                    self.assertEqual(self.invoke("restore", "--archive", str(folder), custom=False),
                                     0, self.progress.getvalue())
                    self.assertEqual(any(call[1:3] == ["image", "load"] for call in self.docker_calls),
                                     "images" in components)
                    self.assertEqual(any(call[1] == "run" for call in self.docker_calls), "volumes" in components)
                    self.assertEqual(cli.config.load(self.project / "config").port,
                                     8818 if "project" in components else 8820)

    def test_restore_a_subset_from_a_full_archive_and_require_explicit_parts(self):
        self.assertEqual(self.invoke("backup"), 0, self.progress.getvalue())
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "project", "images"), 0, self.progress.getvalue())
        self.assertEqual(self.docker_calls, [["docker", "image", "load"]])

        self.assertEqual(self.invoke("backup", "project"), 0, self.progress.getvalue())
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "images", "project"), 1)
        self.assertIn("does not contain the requested components: images", self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(self.invoke("restore", "--import"), 1)
        self.assertIn("requires the volumes component", self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])

    def test_in_place_restore_can_apply_another_component_from_the_retained_bundle(self):
        (self.project / "config/config.ini").write_text("[terminal_server]\ninclude_in_backup = true\n")
        original_key = self.project / cli.config.TERMINAL_SECRET
        original_key.write_bytes(b"archived-terminal-credential-0123456789\n")
        self.assertEqual(self.invoke("backup"), 0, self.progress.getvalue())
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "project", "--no-import", project=self.bundle, custom=False),
                         0, self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])
        self.assertTrue((self.bundle / "loaf").is_symlink())
        key = self.bundle / cli.config.TERMINAL_SECRET
        self.assertFalse(key.exists())
        self.assertEqual(self.invoke("restore", "images", project=self.bundle, custom=False),
                         0, self.progress.getvalue())
        self.assertEqual(self.docker_calls, [["docker", "image", "load"]])
        self.assertEqual(self.invoke("restore", "volumes", project=self.bundle, custom=False),
                         0, self.progress.getvalue())
        self.assertEqual(key.read_bytes(), original_key.read_bytes())

    def test_empty_archives_and_terminal_metadata_without_volumes_are_rejected(self):
        path = self.project / "invalid.tar.gz"
        for metadata in (None, b"{}"):
            with self.subTest(terminal_metadata=metadata):
                entries = {} if metadata is None else {transfer.TERMINAL_METADATA: metadata}
                hashes = {name: sha256(data).hexdigest() for name, data in entries.items()}
                path.write_bytes(gzip.compress(tar_payload(transfer.CHECKSUMS, json.dumps(hashes).encode(), **entries)))
                self.assertEqual(self.invoke("restore", "--archive", str(path), custom=False), 1)
                expected = "no backup components" if metadata is None else "terminal metadata without the volumes"
                self.assertIn(expected, self.progress.getvalue())
                self.assertEqual(self.docker_calls, [])

    def test_volumes_only_round_trip_preserves_the_archived_terminal_key(self):
        settings = self.project / "config/config.ini"
        settings.write_text("[terminal_server]\ninclude_in_backup = true\n")
        key = self.project / cli.config.TERMINAL_SECRET
        original = b"archived-terminal-credential-0123456789\n"
        key.write_bytes(original)
        self.assertEqual(self.invoke("backup", "volumes"), 0, self.progress.getvalue())
        key.unlink()
        self.fail_compose_step = "inspect"
        self.assertEqual(self.invoke("restore", "volumes"), 1)
        self.assertIn("requires the local helper image", self.progress.getvalue())
        self.assertFalse(key.exists())
        self.fail_compose_step = None
        self.assertEqual(self.invoke("restore", "volumes"), 0, self.progress.getvalue())
        self.assertEqual(key.read_bytes(), original)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(original.strip().decode(), self.output.getvalue() + self.progress.getvalue())

    def test_checkout_backup_builds_and_bundles_the_current_executable(self):
        script = self.project / "scripts/build-release.sh"
        script.parent.mkdir()
        script.write_text("cp -- dist/prepared dist/loaf\n", encoding="utf-8")
        old = self.executable.parent / "running-loaf"
        old.write_bytes(self.executable.read_bytes())
        built = old.read_bytes() + b" newest"
        (self.executable.parent / "prepared").write_bytes(built)

        with patch.dict(os.environ, LOAF_RELEASE_EXECUTABLE=str(old)):
            self.assertEqual(self.invoke("backup", "--dry-run"), 0)
            self.assertIn("Build the standalone executable", self.output.getvalue())
            self.assertEqual(self.executable.read_bytes(), old.read_bytes())
            self.assertEqual(self.invoke("backup"), 0, self.progress.getvalue())
            self.assertEqual((self.bundle / "loaf").read_bytes(), built)

            script.write_text("exit 17\n", encoding="utf-8")
            self.docker_calls.clear()
            self.assertEqual(self.invoke("backup", "--verify"), 0)
            self.assertEqual(self.invoke("backup"), 17)

        self.assertEqual(self.docker_calls, [])
        self.assertEqual(list(self.backup_dir.iterdir()), [self.bundle])
        self.assertEqual((self.bundle / "loaf").read_bytes(), built)

    def test_backup_warns_about_modelfiles_and_publishes_the_original_snapshot(self):
        blob = "ollama/models/blobs/sha256-fixture"
        base = f"FROM /{blob}\n".encode()
        template = b'TEMPLATE """hello\nPARAMETER This is template text.\n"""\n'
        recipe = base + template + b"PARAMETER temperature 0.2\nPARAMETER top_k 10\n"
        missing = recipe.replace(b"sha256-fixture", b"sha256-missing")
        prefix = "ollama/config"
        self.volumes = tar_payload(blob, b"weights", **{
            f"{prefix}/current:latest.model": recipe,
            f"{prefix}/library/stale:latest.model": recipe,
            f"{prefix}/missing:latest.model": missing,
        })
        self.definitions = tar_payload(
            "current:latest.model",
            b"# Ollama generated this file.\n" + base + template + b"PARAMETER top_k 10\nPARAMETER temperature 0.2\n",
            **{"library/stale:latest.model": recipe.replace(b"temperature 0.2", b"temperature 0.8"),
               "missing:latest.model": missing},
        )
        self.assertEqual(self.invoke("backup"), 0)
        warnings = [line for line in self.progress.getvalue().splitlines() if line.startswith("Warning:")]
        self.assertCountEqual(warnings, [
            f"Warning: {prefix}/library/stale:latest.model: differs from the current Ollama definition.",
            f"Warning: {prefix}/missing:latest.model: missing model blob sha256-missing.",
        ])
        checker, = [call for call in self.docker_calls if "loaf-check-modelfiles" in call]
        self.assertIn("--read-only", checker)
        self.assertIn("type=volume,source=ollama,target=/ollama,volume-nocopy,readonly", checker)
        with tarfile.open(self.archive, "r:gz") as archive:
            self.assertEqual(archive.extractfile("volumes.tar").read(), self.volumes)
        self.assertIn("Backup saved to", self.output.getvalue())

    def test_backup_warns_and_continues_when_modelfiles_cannot_be_checked(self):
        self.volumes = tar_payload("ollama/config/fixture.model", b"FROM test:latest\n")
        self.fail_modelfile_check = True
        self.assertEqual(self.invoke("backup"), 0)
        self.assertIn("Warning: Could not compare saved Modelfiles", self.progress.getvalue())
        self.assertTrue(self.archive.is_file())

    def test_failed_wheel_export_preserves_previous_backup_before_docker(self):
        self.assertEqual(self.invoke("backup"), 0)
        previous = self.archive.read_bytes()
        self.fail_wheel_export = True
        self.docker_calls.clear()
        self.assertEqual(self.invoke("backup"), 1)
        self.assertIn("Cannot export Python wheels", self.progress.getvalue())
        self.assertEqual(self.archive.read_bytes(), previous)
        self.assertEqual(self.docker_calls, [])

    def test_backup_exports_selected_models_before_saving_and_restore_imports_after_loading(self):
        models = self.project / "selected models.ini"
        model = "hf.co/example/model:Q4_K_M"
        models.write_text(f"[models.test]\nmodel = {model}\n", encoding="utf-8")
        self.assertEqual(self.invoke("backup", "--export", "--models-file", str(models), "--wait-timeout", "42"), 0)
        compose = cli.Compose(self.project)
        export, = [call for call in self.docker_calls if "export-modelfiles" in call]
        self.assertEqual(export[-5:], ["exec", "-T", "ollama-online", "export-modelfiles", model])
        stop = compose.command(cli.Step(cli.PROFILES, ("stop",)))
        volumes = transfer.volume_command()
        self.assertLess(self.docker_calls.index(export), self.docker_calls.index(stop))
        self.assertLess(self.docker_calls.index(stop), self.docker_calls.index(volumes))
        self.assertTrue(any(call[-3:] == ["--wait-timeout", "42", "ollama-online"] for call in self.docker_calls))
        self.assertTrue(all("pull" not in call for call in self.docker_calls))

        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--import"), 0)
        index = self.docker_calls.index(transfer.volume_command(restore=True))
        self.assertEqual(self.docker_calls[index + 1:], [
            compose.command(step) for step in cli.plan("offline", import_definitions=True)
        ])
        self.assertIn("Open WebUI is stopped.", self.output.getvalue())

    def test_export_failure_preserves_previous_backup_and_import_failure_keeps_restored_data(self):
        self.assertEqual(self.invoke("backup"), 0)
        previous = self.archive.read_bytes()
        (self.project / "config/models.ini").write_text("[models.test]\nmodel = hf.co/example/model\n", encoding="utf-8")
        self.fail_compose_step = "export-modelfiles"
        self.docker_calls.clear()
        self.assertEqual(self.invoke("backup", "--export"), 23)
        self.assertEqual(self.archive.read_bytes(), previous)
        self.assertIn(["docker", "image", "save", *transfer.IMAGES], self.docker_calls)
        self.assertNotIn(transfer.volume_command(), self.docker_calls)

        self.fail_compose_step = "up"
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--import"), 23)
        self.assertIn(transfer.volume_command(restore=True), self.docker_calls)
        self.assertIn("Restored from", self.output.getvalue())
        self.assertNotIn("Modelfiles imported", self.output.getvalue())

    def test_ini_transfer_defaults_and_per_run_opt_out(self):
        ini = (
            "[launcher]\nmodels_file = selected.ini\n"
            "[transfer]\nbackup_dir = configured archives\nexport_models = true\n"
            "import_models = true\ncompression_level = 6\n"
        )
        (self.project / "config/config.ini").write_bytes(ini.encode("utf-8"))
        model_ini = b"[models.cached]\nmodel = hf.co/example/cached\n"
        (self.project / "config/selected.ini").write_bytes(model_ini)
        with patch("loaf.transfer.tarfile.open", wraps=tarfile.open) as open_tar:
            self.assertEqual(self.invoke("backup", custom=False), 0)
        self.assertIn(6, [call.kwargs.get("compresslevel") for call in open_tar.call_args_list])
        archive = self.project / "configured archives" / self.bundle.name / self.archive.name
        with tarfile.open(archive, "r:gz") as bundle:
            with tarfile.open(fileobj=io.BytesIO(bundle.extractfile("project.tar").read())) as project:
                self.assertEqual(project.extractfile("config/config.ini").read().decode(), ini)
                self.assertEqual(project.extractfile("config/selected.ini").read(), model_ini)
        self.assertTrue(any(call[-2:] == ["export-modelfiles", "hf.co/example/cached"] for call in self.docker_calls))

        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", custom=False), 0)
        self.assertIn(str(archive.resolve()), self.output.getvalue())
        self.assertIn("Modelfiles imported", self.output.getvalue())

        self.assertEqual(self.invoke("restore", "--no-import", "--dry-run", custom=False), 0)
        self.assertNotIn("import saved Modelfiles", self.output.getvalue())
        self.assertEqual(self.invoke("backup", "--no-export", "--dry-run", custom=False), 0)
        self.assertNotIn("Export the Modelfile", self.output.getvalue())

    def test_failed_backup_keeps_previous_archive_and_cleans_staging(self):
        root = self.project / "backup"
        archive = root / self.bundle.name / self.archive.name
        self.assertEqual(self.invoke("backup", custom=False), 0)
        previous = archive.read_bytes()
        self.fail_volume_backup = True
        self.assertEqual(self.invoke("backup", custom=False), 1)
        self.assertEqual(archive.read_bytes(), previous)
        self.assertEqual(list(root.iterdir()), [archive.parent])

    def test_backup_excludes_numbered_bundles_from_its_checkout_payload(self):
        self.assertEqual(self.invoke("backup"), 0, self.progress.getvalue())
        self.assertEqual(self.invoke("backup"), 0, self.progress.getvalue())
        backup = self.backup_dir / (self.bundle.name + "-2")
        self.assertTrue(backup.is_dir())
        # Include a previous bundle in Git's inventory despite the checkout ignore rules.
        self.git("add", "-f", str(backup.relative_to(self.project)))
        self.assertEqual(self.invoke("backup"), 0, self.progress.getvalue())
        newest = transfer.read_bundle(self.backup_dir / (self.bundle.name + "-3"))
        with tarfile.open(newest.archive, "r:gz") as outer:
            with tarfile.open(fileobj=outer.extractfile("project.tar"), mode="r|") as checkout:
                self.assertFalse(any(path.startswith("transfer disk/") for path in checkout.getnames()))

    def test_restore_prefers_newest_timestamp_and_accepts_explicit_archives(self):
        settings = self.project / "config/config.ini"
        settings.write_text("[webui]\nport = 8818\n")
        self.assertEqual(self.invoke("backup"), 0)
        self.clock.now.return_value = self.timestamp.replace(minute=1)
        self.assertEqual(self.invoke("backup"), 0)
        newer = self.backup_dir / f"20261120-1501_{transfer.BUNDLE_NAME}"
        self.assertEqual({path.name for path in self.backup_dir.iterdir()}, {self.bundle.name, newer.name})
        self.assertEqual(self.invoke("restore"), 0)
        self.assertIn(newer.name, self.output.getvalue())
        self.assertEqual(self.invoke("restore", "--archive", str(self.archive), custom=False), 0)
        self.assertIn(self.archive.name, self.output.getvalue())
        shutil.rmtree(newer)
        raw_archive = self.archive.rename(self.backup_dir / transfer.ARCHIVE_NAME)
        shutil.rmtree(self.bundle)
        self.assertEqual(self.invoke("restore"), 1)
        self.assertIn("No backup found", self.progress.getvalue())
        settings.write_text("[webui]\nport = 8820\n")
        self.assertEqual(self.invoke("restore", "--archive", str(raw_archive), custom=False), 0)
        self.assertIn(str(raw_archive.resolve()), self.output.getvalue())
        self.assertEqual(cli.config.load(self.project / "config").port, 8818)

    def test_versioned_backup_and_restore_use_only_selected_resources(self):
        namespace = "v1-2-local-inference"
        tags = [f"local/{name}:{namespace}" for name in ("ollama-online", "ollama-offline", "open-webui")]
        self.images = image_payload(tags)
        self.assertEqual(self.invoke("backup", "--version", "v1-2"), 0)
        versioned_archive, = self.backup_dir.iterdir()
        self.assertEqual(versioned_archive.name, f"20261120-1500_{namespace}_{transfer.BUNDLE_NAME}")
        self.assertIn(["docker", "image", "save", *tags], self.docker_calls)
        self.assertTrue(any("--project-name" in call and namespace in call for call in self.docker_calls))
        for volume in ("ollama", "open-webui"):
            self.assertIn(["docker", "ps", "--quiet", "--filter", f"volume={namespace}-{volume}"], self.docker_calls)
        (self.backup_dir / "20261120-1600_local-ai-ferry.tar.gz").touch()
        (self.backup_dir / "20261120-1600_v2-local-inference_local-ai-ferry.tar.gz").touch()

        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--version", "v1-2"), 0)
        self.assertIn(versioned_archive.name, self.output.getvalue())
        helper, = [call for call in self.docker_calls if call[1] == "run"]
        self.assertIn(tags[0], helper)
        self.assertIn(f"type=volume,source={namespace}-ollama,target=/transfer/ollama,volume-nocopy", helper)
        self.assertIn(f"type=volume,source={namespace}-open-webui,target=/transfer/webui,volume-nocopy", helper)

        renamed = self.backup_dir / "renamed.tar.gz"
        transfer.read_bundle(versioned_archive).archive.rename(renamed)
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--archive", str(renamed), "--version=", custom=False), 1)
        self.assertIn("Backup image tags do not match", self.progress.getvalue())
        self.assertTrue(all(call[1] == "ps" for call in self.docker_calls))

    def test_restore_keeps_docker_error_when_volume_command_rejects_input(self):
        self.assertEqual(self.invoke("backup"), 0)
        self.fail_volume_restore = True
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--import"), 23)
        self.assertTrue(all(command[1] != "compose" for command in self.docker_calls))
        self.assertIn("Restore requires empty volumes.", self.progress.getvalue())

    def test_standalone_setup_then_restore_discovers_a_versioned_backup(self):
        namespace = "v2-local-inference"
        self.images = image_payload(transfer.image_names(namespace))
        (self.project / "config/config.ini").write_text("[webui]\nport = 8080\n", encoding="utf-8")
        source_model = self.project / "config/models/blue.model"
        source_model.parent.mkdir()
        source_model.write_text("FROM model\n", encoding="utf-8")
        self.assertEqual(self.invoke("backup", "--version", "v2"), 0, self.progress.getvalue())
        bundle, = self.backup_dir.iterdir()
        self.docker_calls.clear()
        with patch.object(setup, "bundled_release", return_value=bundle / "runtime"):
            self.assertEqual(self.invoke("setup", custom=False, project=bundle), 0, self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(cli.config.load(bundle / "config").port, 3000)
        self.assertFalse((bundle / ".venv").exists())
        self.assertFalse((bundle / "python-package").exists())
        self.assertFalse((bundle / ".git").exists())

        self.fail_volume_restore = True
        self.assertEqual(self.invoke("restore", custom=False, project=bundle), 23, self.progress.getvalue())
        self.assertEqual(cli.config.load(bundle / "config").port, 3000)
        self.assertFalse((bundle / "config/models/blue.model").exists())
        original = transfer.read_bundle(bundle)
        self.assertFalse((bundle / "backup").exists())
        self.fail_volume_restore = False
        self.assertEqual(self.invoke("restore", custom=False, project=bundle), 0, self.progress.getvalue())
        settings = cli.config.load(bundle / "config")
        self.assertEqual((settings.port, settings.version), (8080, "v2"))
        self.assertEqual((bundle / "config/models/blue.model").read_bytes(), source_model.read_bytes())
        self.assertIn(transfer.volume_command(namespace=namespace, restore=True), self.docker_calls)
        self.assertFalse(original.archive.exists())
        self.assertFalse((bundle / "backup.json").exists())
        self.assertEqual(os.readlink(bundle / "loaf"), "dist/loaf")
        preserved = transfer.read_bundle(bundle / "backup")
        self.assertEqual(preserved.archive.name, original.archive.name)
        self.assertEqual((bundle / "dist/loaf").read_bytes(), preserved.executable.read_bytes())
        self.assertEqual(self.invoke("backup", "--verify", custom=False, project=bundle), 0, self.progress.getvalue())

        self.assertEqual(self.invoke("backup", custom=False, project=bundle), 0, self.progress.getvalue())
        backup, = (bundle / "backup").glob("*/*.tar.gz")
        self.assertEqual(
            self.invoke("backup", "--verify", "--backup-dir", str(bundle / "backup"), custom=False, project=bundle),
            0, self.progress.getvalue(),
        )
        self.assertIn(str(backup), self.output.getvalue())
        with tarfile.open(backup, "r:gz") as outer:
            with tarfile.open(fileobj=outer.extractfile("project.tar"), mode="r|") as inner:
                self.assertNotIn(".git/HEAD", inner.getnames())

    @unittest.skipUnless(hasattr(tarfile, "data_filter"), "Safe tar extraction filters are required.")
    def test_setup_all_recovers_source_files_and_keeps_the_backup(self):
        docs = self.project / "docs"
        docs.mkdir()
        (docs / "guide.md").write_bytes(b"Source documentation\n")
        (docs / "guide-link.md").symlink_to("guide.md")
        package_file = self.project / "python-package/pyproject.toml"
        package_file.write_bytes(b"# Exact Python package fixture\n")
        package_file.chmod(0o755)
        self.git("add", "docs", "python-package")
        bin_dir = self.project / ".venv/bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to("/usr/bin/python3")
        (bin_dir / "python3.13").symlink_to("python3")
        self.assertEqual(self.invoke("backup"), 0, self.progress.getvalue())
        original_bytes = self.archive.read_bytes()
        self.docker_calls.clear()

        self.assertEqual(self.invoke("setup", "--all", "--dry-run", custom=False, project=self.bundle), 0)
        self.assertFalse((self.bundle / "docs").exists())
        self.assertEqual(self.docker_calls, [])
        rename = Path.rename

        def fail_launcher_link(source, target):
            if source.name == "loaf-link":
                raise OSError("Cannot publish launcher link.")
            return rename(source, target)

        with patch.object(Path, "rename", fail_launcher_link):
            self.assertEqual(self.invoke("setup", "--all", custom=False, project=self.bundle), 1)
        self.assertIn("Cannot publish launcher link.", self.progress.getvalue())
        self.assertEqual(transfer.read_bundle(self.bundle).archive.read_bytes(), original_bytes)
        for path in ("backup", "dist", "docs", "python-package"):
            self.assertFalse((self.bundle / path).exists())
        self.docker_calls.clear()
        self.assertEqual(self.invoke("setup", "--all", custom=False, project=self.bundle), 0, self.progress.getvalue())

        recovered = self.bundle / package_file.relative_to(self.project)
        self.assertEqual(recovered.read_bytes(), package_file.read_bytes())
        self.assertEqual(recovered.stat().st_mode & 0o777, 0o755)
        self.assertEqual(os.readlink(self.bundle / "docs/guide-link.md"), "guide.md")
        self.assertEqual((self.bundle / "docs/guide-link.md").read_bytes(), b"Source documentation\n")
        self.assertEqual(os.readlink(self.bundle / ".venv/bin/python3"), "/usr/bin/python3")
        self.assertEqual(os.readlink(self.bundle / ".venv/bin/python3.13"), "python3")
        self.assertEqual((self.bundle / ".git/HEAD").read_bytes(), (self.project / ".git/HEAD").read_bytes())
        self.assertTrue((self.bundle / cli.config.TERMINAL_SECRET).is_file())
        self.assertFalse((self.bundle / ".env").exists())
        preserved = transfer.read_bundle(self.bundle / "backup")
        self.assertEqual(preserved.archive.read_bytes(), original_bytes)
        self.assertEqual(os.readlink(self.bundle / "loaf"), "dist/loaf")
        self.assertEqual((self.bundle / "loaf").read_bytes(), preserved.executable.read_bytes())
        self.assertIn(transfer.volume_command(restore=True), self.docker_calls)

    @unittest.skipUnless(hasattr(tarfile, "data_filter"), "Safe tar extraction filters are required.")
    def test_setup_all_rejects_external_source_links_before_docker(self):
        docs = self.project / "docs"
        docs.mkdir()
        (docs / "escape").symlink_to(self.project)
        self.git("add", "docs", "python-package")
        self.assertEqual(self.invoke("backup"), 0, self.progress.getvalue())
        self.docker_calls.clear()
        self.assertEqual(self.invoke("setup", "--all", custom=False, project=self.bundle), 1)
        self.assertIn("absolute path", self.progress.getvalue())
        self.assertTrue(self.archive.is_file())
        self.assertFalse((self.bundle / "docs").exists())
        self.assertEqual(self.docker_calls, [])

    def test_restore_checks_bundle_paths_and_executable_before_docker(self):
        self.assertEqual(self.invoke("backup"), 0)
        self.docker_calls.clear()
        for path in (self.bundle / transfer.BUNDLE_MANIFEST, self.bundle / "loaf", self.archive):
            with self.subTest(path=path.name):
                data = path.read_bytes()
                path.unlink()
                self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False), 1)
                self.assertIn(str(path), self.progress.getvalue())
                path.write_bytes(data)
        (self.bundle / "loaf").write_bytes(self.executable.read_bytes() + b"changed")
        self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False), 1)
        self.assertIn("SHA-256 verification failed", self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])

    def test_verify_and_restore_reject_a_retired_bundle_manifest(self):
        self.assertEqual(self.invoke("backup"), 0)
        (self.bundle / "backup.json").write_text(json.dumps({
            "archive": self.archive.name, "owo_sha256": setup.file_digest(self.executable), "version": "",
        }), encoding="utf-8")
        self.docker_calls.clear()
        for command, options in (("backup", ("--verify",)), ("restore", ())):
            with self.subTest(command=command):
                self.assertEqual(self.invoke(command, *options), 1)
                self.assertIn("Invalid backup manifest", self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])

    def test_model_archive_discovery_uses_current_dated_filenames(self):
        self.backup_dir.mkdir()
        (self.backup_dir / "owo-volumes.tar.gz").touch()
        (self.backup_dir / "20261120-1600_owo-volumes.tar.gz").touch()
        (self.backup_dir / "loaf-volumes.tar.gz").touch()
        self.assertIsNone(transfer.latest_backup(self.backup_dir, models_only=True))
        current = self.backup_dir / "20261120-1500_loaf-volumes.tar.gz"
        current.touch()
        self.assertEqual(transfer.latest_backup(self.backup_dir, models_only=True), current)

    def test_standalone_setup_recovers_cached_installers_on_request(self):
        self.assertEqual(self.invoke("backup"), 0)
        self.docker_calls.clear()
        with (
            patch.object(setup, "bundled_release", return_value=self.bundle / "runtime"),
            patch.object(setup, "prepare_docker") as docker,
            patch.object(setup, "install_toolkit") as toolkit,
        ):
            self.assertEqual(
                self.invoke("setup", "--with-docker-deps", custom=False, project=self.bundle), 0,
                self.progress.getvalue(),
            )
        docker.assert_called_once_with(self.bundle, allow_downloads=False)
        toolkit.assert_called_once_with(self.bundle, allow_downloads=False)
        recovered = self.bundle / self.docker_cache.relative_to(self.project)
        self.assertEqual((recovered / "Packages").read_bytes(), b"fixture package index")
        self.assertFalse((self.bundle / setup.PYTHON_WHEEL_DIR).exists())
        self.assertEqual(self.docker_calls, [])

    def test_key_setup_from_a_bundle_preserves_the_archived_credential(self):
        (self.project / "config/config.ini").write_text("[terminal_server]\ninclude_in_backup = true\n")
        key = self.project / cli.config.TERMINAL_SECRET
        key.write_bytes(b"archived-terminal-credential-0123456789")
        self.assertEqual(self.invoke("backup"), 0, self.progress.getvalue())
        self.docker_calls.clear()
        self.assertEqual(
            self.invoke("setup", "--terminal-server-key", custom=False, project=self.bundle),
            0, self.progress.getvalue(),
        )
        recovered_key = self.bundle / cli.config.TERMINAL_SECRET
        self.assertEqual(recovered_key.read_bytes(), key.read_bytes())
        self.assertFalse((self.bundle / "config/config.ini").exists())

        with patch.object(setup, "bundled_release", return_value=self.bundle / "runtime"):
            self.assertEqual(self.invoke("setup", custom=False, project=self.bundle), 0, self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(self.invoke("restore", custom=False, project=self.bundle), 0, self.progress.getvalue())
        self.assertEqual(recovered_key.read_bytes(), key.read_bytes())

    def test_backup_lists_the_selected_checkout_with_different_ownership(self):
        environment = {
            "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "LC_ALL": "C",
        }
        with patch.dict(os.environ, environment):
            command = ["git", "ls-files"]
            rejected = self.run(command, cwd=self.project, capture_output=True)
            if rejected.returncode == 0:
                self.skipTest("This Git does not simulate a different repository owner.")
            self.assertIn(b"dubious ownership", rejected.stderr)
            self.assertEqual(self.invoke("backup"), 0)
            rejected = self.run(command, cwd=self.project, capture_output=True)
            self.assertIn(b"dubious ownership", rejected.stderr)
        self.assertTrue(self.archive.is_file())

    def test_restore_rejects_corrupt_archive_before_image_load(self):
        self.assertEqual(self.invoke("backup"), 0)
        self.archive.write_bytes(self.archive.read_bytes()[:-4])
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore"), 1)
        self.assertEqual(self.docker_calls, [])

    def test_restore_rejects_an_archived_link_traversal_before_docker(self):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as volumes:
            link = tarfile.TarInfo("ollama/redirect")
            link.type = tarfile.SYMTYPE
            link.linkname = "../webui"
            volumes.addfile(link)
            entry = tarfile.TarInfo("ollama/redirect/config.json")
            entry.size = 1
            volumes.addfile(entry, io.BytesIO(b"x"))
        self.volumes = payload.getvalue()
        self.assertEqual(self.invoke("backup"), 0)
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore"), 1)
        self.assertIn("traverses an archived link", self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])
        self.assertFalse(list(self.project.glob(".loaf-restore-*")))

    def test_backup_excludes_configured_terminal_server_key(self):
        key = self.project / "private-key.txt"
        key.write_text("fixture key")
        (self.project / "config/config.ini").write_text(
            "[terminal_server]\nenabled = true\nsecret_file = private-key.txt\n"
        )
        self.assertEqual(self.invoke("backup"), 0)
        with tarfile.open(self.archive, "r:gz") as bundle:
            with tarfile.open(fileobj=io.BytesIO(bundle.extractfile("project.tar").read())) as project:
                self.assertNotIn(key.name, project.getnames())
            self.assertNotIn(transfer.TERMINAL_METADATA, bundle.getnames())

    def test_backup_includes_existing_terminal_image_without_workspace_data(self):
        namespace = "v2-local-inference"
        tags = transfer.image_names(namespace, include_terminal=True)
        self.terminal_image = tags[-1]
        self.images = image_payload(tags)
        self.assertEqual(self.invoke("backup", "--version", "v2"), 0, self.progress.getvalue())
        self.assertIn(["docker", "image", "ls", "--quiet", "--filter", f"reference={tags[-1]}"], self.docker_calls)
        self.assertIn(["docker", "image", "save", *tags], self.docker_calls)
        archive, = self.backup_dir.glob("*/*.tar.gz")
        with tarfile.open(archive, "r:gz") as bundle:
            self.assertNotIn(transfer.TERMINAL_METADATA, bundle.getnames())

        self.docker_calls.clear()
        self.assertEqual(self.invoke("backup", "--verify", "--version", "v2"), 0, self.progress.getvalue())
        self.assertIn(tags[-1], self.output.getvalue())
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(self.invoke("restore", "--version", "v2"), 0, self.progress.getvalue())
        self.assertIn(["docker", "image", "load"], self.docker_calls)

    def test_no_terminal_server_skips_image_and_preserves_explicit_workspace_backup(self):
        self.terminal_image = transfer.image_names("", include_terminal=True)[-1]
        key = self.project / "config/terminal-server.key"
        secret = b"x" * 40 + b"\n"
        key.write_bytes(secret)
        (self.project / "config/config.ini").write_text("[terminal_server]\ninclude_in_backup=true\n")
        workspace = transfer.workspace_prefix("") + "a" * 64
        self.workspace_volumes[workspace] = {
            "Name": workspace, "Labels": transfer.workspace_labels("", workspace),
            "Driver": "local", "Options": None,
        }
        self.volumes = tar_payload("ollama/model", b"base", **{workspace: None})

        self.assertEqual(self.invoke("backup", "--no-terminal-server"), 0, self.progress.getvalue())
        image_calls = [call for call in self.docker_calls if call[1] == "image"]
        self.assertEqual(image_calls, [["docker", "image", "save", *transfer.IMAGES]])
        self.assertIn(transfer.volume_command(workspaces=(workspace,)), self.docker_calls)
        self.assertEqual(self.invoke("backup", "--verify"), 0, self.progress.getvalue())
        self.assertNotIn(self.terminal_image, self.output.getvalue())

        key.unlink()
        self.assertEqual(self.invoke("restore"), 0, self.progress.getvalue())
        self.assertEqual(key.read_bytes(), secret)
        self.assertIn(transfer.volume_command(restore=True, workspaces=(workspace,)), self.docker_calls)

    def test_terminal_backup_restores_selected_workspaces_and_private_key(self):
        namespace = "v2-local-inference"
        workspace = transfer.workspace_prefix(namespace) + "a" * 64
        other = transfer.workspace_prefix("") + "b" * 64
        for name, deployment in ((workspace, namespace), (other, "")):
            self.workspace_volumes[name] = {
                "Name": name, "Labels": transfer.workspace_labels(deployment, name),
                "Driver": "local", "Options": None,
            }
        secret = b"x" * 40 + b"\r\n"
        source_key = self.project / "private-key.txt"
        source_key.write_bytes(secret)
        settings = self.project / "config/config.ini"
        settings.write_text("[terminal_server]\ninclude_in_backup=true\nsecret_file=private-key.txt\n")
        tags = transfer.image_names(namespace, include_terminal=True)
        self.terminal_image = tags[-1]
        self.images = image_payload(tags)
        self.volumes = tar_payload("ollama/model", b"base", **{
            workspace: None, f"{workspace}/code.c": b"workspace contents",
        })
        self.assertEqual(self.invoke("backup", "--version", "v2"), 0, self.progress.getvalue())
        archive, = self.backup_dir.glob("*/*.tar.gz")
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertIn(["docker", "image", "save", *tags], self.docker_calls)
        self.assertIn(transfer.volume_command(namespace=namespace, workspaces=(workspace,)), self.docker_calls)
        self.assertTrue(all(not any(other in part for part in command) for command in self.docker_calls))
        with tarfile.open(archive, "r:gz") as bundle:
            manifest = json.load(bundle.extractfile(transfer.CHECKSUMS))
            with bundle.extractfile(transfer.TERMINAL_METADATA) as metadata:
                self.assertEqual(manifest[transfer.TERMINAL_METADATA], sha256(metadata.read()).hexdigest())
            with tarfile.open(fileobj=io.BytesIO(bundle.extractfile("project.tar").read())) as project:
                self.assertNotIn("private-key.txt", project.getnames())
        self.assertEqual(self.invoke("backup", "--verify", "--version", "v2"), 0, self.progress.getvalue())
        self.assertIn(tags[-1], self.output.getvalue())
        self.assertNotIn(secret.strip().decode(), self.output.getvalue() + self.progress.getvalue())

        tampered = self.backup_dir / "tampered.tar.gz"
        with tarfile.open(archive, "r:gz") as bundle, tarfile.open(tampered, "w:gz") as output:
            for member in bundle:
                content = bundle.extractfile(member).read()
                if member.name == transfer.TERMINAL_METADATA:
                    content = content.replace(b"x" * 40, b"q" * 40)
                output.addfile(member, io.BytesIO(content))
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--archive", str(tampered), "--version", "v2", custom=False), 1)
        self.assertIn(f"SHA-256 verification failed for {transfer.TERMINAL_METADATA}", self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])

        settings.write_text("[terminal_server]\nsecret_file=private/restored.key\n")
        restored = source_key
        source_key.unlink()
        self.fail_volume_restore = True
        self.assertEqual(self.invoke("restore", "--version", "v2"), 23)
        self.assertFalse(restored.exists())
        self.fail_volume_restore = False
        self.assertEqual(self.invoke("restore", "--version", "v2"), 0, self.progress.getvalue())
        self.assertEqual(restored.read_bytes(), secret)
        self.assertEqual(restored.stat().st_mode & 0o777, 0o600)
        helper = transfer.volume_command(restore=True, namespace=namespace, workspaces=(workspace,))
        self.assertIn(helper, self.docker_calls)
        restored.write_bytes(b"z" * 32)
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--version", "v2"), 1)
        self.assertIn("differs from the backup", self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(restored.read_bytes(), b"z" * 32)

    def test_terminal_backup_requires_the_key_before_docker(self):
        (self.project / "config/config.ini").write_text("[terminal_server]\ninclude_in_backup=true\n")
        self.assertEqual(self.invoke("backup"), 1)
        self.assertIn("Terminal server key file is missing", self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])
        self.assertFalse(self.archive.exists())

    def test_verify_backup_reports_platforms_and_rejects_changed_payload_without_docker(self):
        self.assertEqual(self.invoke("backup"), 0)
        self.docker_calls.clear()
        self.assertEqual(self.invoke("backup", "--verify"), 0)
        for image in transfer.IMAGES:
            self.assertIn(f"{image}  linux/amd64", self.output.getvalue())
        self.assertEqual(self.docker_calls, [])

        changed = self.backup_dir / "changed.tar.gz"
        with tarfile.open(self.archive, "r:gz") as bundle, tarfile.open(changed, "w:gz") as output:
            for member in bundle:
                with bundle.extractfile(member) as content:
                    data = content.read()
                if member.name == "project.tar":
                    data = b"x" + data[1:]
                output.addfile(member, io.BytesIO(data))
        for mode, options in (("backup", ["--verify"]), ("restore", []), ("restore", ["images"])):
            self.assertEqual(self.invoke(mode, "--archive", str(changed), *options, custom=False), 1)
            self.assertIn("SHA-256 verification failed for project.tar", self.progress.getvalue())
            self.assertEqual(self.docker_calls, [])

    def test_archives_without_hashes_are_rejected_before_docker(self):
        self.assertEqual(self.invoke("backup"), 0)
        archive = self.backup_dir / transfer.ARCHIVE_NAME
        with tarfile.open(self.archive, "r:gz") as bundle, tarfile.open(archive, "w:gz") as output:
            for member in bundle:
                if member.name != transfer.CHECKSUMS:
                    with bundle.extractfile(member) as content:
                        output.addfile(member, content)
        self.docker_calls.clear()
        for command, options in (("restore", ()), ("backup", ("--verify",))):
            with self.subTest(command=command):
                self.assertEqual(self.invoke(command, "--archive", str(archive), *options, custom=False), 1)
                self.assertIn("has no SHA-256 manifest", self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])

    def test_cpu_modelfile_restore_uses_the_cpu_override(self):
        self.assertEqual(self.invoke("backup"), 0)
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--import", "--cpu"), 0, self.progress.getvalue())
        serving, = [call for call in self.docker_calls if "compose" in call and "up" in call]
        self.assertIn(str(DATA / "compose.cpu.yml"), serving)
        self.assertEqual(serving[-1], "ollama-offline")
        self.assertIn("CPU Ollama is ready", self.output.getvalue())

    def test_transfer_dry_run_performs_no_docker_calls_or_writes(self):
        for mode in ("backup", "restore"):
            self.assertEqual(self.invoke(mode, "--dry-run", custom=False), 0)
        (self.project / "config/models.ini").write_text("[models.test]\nmodel = hf.co/example/model\n", encoding="utf-8")
        self.assertEqual(self.invoke("backup", "--export", "--dry-run", custom=False), 0)
        output = self.output.getvalue()
        self.assertLess(output.index("Save images"), output.index("Export the Modelfile"))
        self.assertLess(output.index("Export the Modelfile"), output.index("Archive service volumes"))
        self.assertNotIn("+ ", output)
        self.assertEqual(self.invoke("backup", "-v", "--dry-run", custom=False), 0)
        self.assertTrue(any(line.startswith("+ ") and line.endswith(" stop") for line in self.output.getvalue().splitlines()))
        self.assertEqual(self.invoke("restore", "--import", "--dry-run", custom=False), 0)
        output = self.output.getvalue()
        self.assertLess(output.index("Load images"), output.index("import saved Modelfiles"))
        self.assertEqual(self.docker_calls, [])
        self.assertFalse((self.project / "backup").exists())
        self.assertFalse((self.project / ".run.lock").exists())
        self.assertFalse(self.launcher_wheel.exists())


class ModelTransferTests(unittest.TestCase):
    def test_model_archive_commands_use_separate_names_and_custom_paths(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "compose.yml").touch()
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["--project-dir", folder, "export", "--dry-run"]), 0)
            self.assertIn("_loaf-volumes.tar.gz", output.getvalue())
            archive = project / "custom.tar.gz"
            with patch.object(transfer, "import_models") as restore, redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["--project-dir", folder, "--version", "v2", "import",
                                           "--archive", str(archive), "--delete"]), 0)
            self.assertEqual(restore.call_args.args[:3], (project.resolve(), archive.resolve(), "v2-local-inference"))
            self.assertEqual(restore.call_args.kwargs, {"delete": True})

    def test_models_only_flags_route_to_the_model_store_operations(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "compose.yml").touch()
            archive = project / "models.tar.gz"
            options = ["--project-dir", folder, "--archive", str(archive), "--version", "v2"]
            with (
                patch.object(transfer, "export_models") as save,
                patch.object(transfer, "import_models") as restore,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(["backup", "volumes", "--models-only", *options]), 0)
                self.assertEqual(cli.main(["restore", "volumes", "--models-only", *options, "--delete"]), 0)
            self.assertEqual(save.call_args.args[:3], (project.resolve(), archive, "v2-local-inference"))
            self.assertEqual(restore.call_args.args[:3], (project.resolve(), archive, "v2-local-inference"))
            self.assertEqual(restore.call_args.kwargs, {"delete": True})
            for arguments in (
                ["backup", "images", "volumes", "--models-only"], ["restore", "volumes", "--delete"],
                ["backup", "project", "project"], ["restore", "config"], ["backup", "images", "--verify"],
            ):
                with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    cli.main(arguments)

    def test_wrong_model_archive_never_reaches_docker(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            path = project / "wrong.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                member = tarfile.TarInfo("webui/data.db")
                archive.addfile(member)
            with patch.object(transfer, "model_volume_command") as docker, redirect_stderr(io.StringIO()):
                stop = Mock()
                with self.assertRaisesRegex(transfer.TransferError, "Unexpected model archive member"):
                    transfer.import_models(project, path, "", stop)
            docker.assert_not_called()
            stop.assert_not_called()

    def test_v2_archive_validates_manifest_links_and_child_blobs(self):
        tag = "models/manifests-v2/ollama.com/library/Example/latest"
        layer = b"weights"
        layer_digest = sha256(layer).hexdigest()
        child = json.dumps({
            "config": {"digest": f"sha256:{layer_digest}", "size": len(layer)}, "layers": [],
        }).encode()
        child_digest = sha256(child).hexdigest()
        manifest = json.dumps({
            "mediaType": transfer.MANIFEST_LIST_TYPE, "manifests": [{"digest": f"sha256:{child_digest}"}],
        }).encode()
        manifest_digest = sha256(manifest).hexdigest()
        blob = f"models/blobs/sha256-{manifest_digest}"
        link = posixpath.relpath(blob, posixpath.dirname(tag))
        files = {blob: manifest, f"models/blobs/sha256-{child_digest}": child,
                 f"models/blobs/sha256-{layer_digest}": layer}
        with TemporaryDirectory() as folder:
            path = Path(folder) / "models.tar"

            def write_archive(target, contents, extra_member=None):
                with tarfile.open(path, "w:") as archive:
                    root = tarfile.TarInfo("models")
                    root.type = tarfile.DIRTYPE
                    archive.addfile(root)
                    registration = tarfile.TarInfo(tag)
                    registration.type = tarfile.SYMTYPE
                    registration.linkname = target
                    archive.addfile(registration)
                    for name, data in contents.items():
                        member = tarfile.TarInfo(name)
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))
                    if extra_member:
                        archive.addfile(extra_member)

            write_archive(link, files)
            transfer.validate_model_archive(path)
            wrong_link = "/" + blob
            missing_target = {name: data for name, data in files.items() if name != blob}
            missing_layer = {name: data for name, data in files.items() if data != layer}
            for target, contents, error in (
                (wrong_link, files, "Expected a relative link"),
                ("../" + link, files, "Expected a relative link"),
                ("../latest", files, "Expected a relative link"),
                (link, missing_target, "Missing or incomplete blob"),
                (link, {**files, blob: b"wrong manifest"}, "Manifest blob digest mismatch"),
                (link, missing_layer, "Missing or incomplete blob"),
            ):
                with self.subTest(error=error, target=target):
                    write_archive(target, contents)
                    with self.assertRaisesRegex(transfer.TransferError, error):
                        transfer.validate_model_archive(path)
            write_archive(link, files, tarfile.TarInfo(f"{tag}/child"))
            with self.assertRaisesRegex(transfer.TransferError, "non-directory parent"):
                transfer.validate_model_archive(path)

    def test_transfer_accepts_selected_live_backend_and_rejects_other_writers(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            with patch.object(transfer.subprocess, "check_output", side_effect=[
                b"[]", b"v2-local-inference/ollama-connected\n",
            ]):
                command = transfer.model_volume_command(project, "v2-local-inference")
            self.assertIn("type=volume,source=v2-local-inference-ollama,target=/ollama,volume-nocopy,readonly", command)
            self.assertIn(transfer.SNAPSHOT_MODELS, command)
            for consumer in (b"v2-local-inference/ollama-online", b"foreign/ollama-connected", b"/"):
                with self.subTest(consumer=consumer), patch.object(
                    transfer.subprocess, "check_output", side_effect=[b"[]", consumer],
                ), self.assertRaisesRegex(transfer.TransferError, "in use"):
                    transfer.model_volume_command(project, "v2-local-inference")

    @unittest.skipUnless(shutil.which("rsync") and shutil.which("sh"), "rsync and sh are required.")
    def test_matching_blob_name_and_size_can_hide_corrupt_destination_data(self):
        healthy = b"GGUF fixture"
        corrupt = b"---- fixture"
        digest = sha256(healthy).hexdigest()
        blob = f"models/blobs/sha256-{digest}"
        tag = "models/manifests/registry.ollama.ai/library/example/latest"
        manifest = json.dumps({"config": {"digest": f"sha256:{digest}", "size": len(healthy)}, "layers": []}).encode()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "models.tar"
            source = root / "source"
            (source / tag).parent.mkdir(parents=True)
            (source / tag).write_bytes(manifest)
            (source / blob).parent.mkdir(parents=True)
            for contents in (corrupt, healthy):
                (source / blob).write_bytes(contents)
                with tarfile.open(archive, "w") as payload:
                    payload.add(source / "models", arcname="models")
                transfer.validate_model_archive(archive)

            target, staging = root / "destination", root / "staging"
            staging.mkdir()
            (target / blob).parent.mkdir(parents=True)
            (target / blob).write_bytes(corrupt)
            script = transfer.IMPORT_MODELS.replace("/ollama", str(target)).replace("/staging", str(staging))
            with archive.open("rb") as payload:
                subprocess.run(["sh", "-c", script], stdin=payload, check=True)
            self.assertEqual((target / blob).read_bytes(), corrupt)
            self.assertEqual((target / tag).read_bytes(), manifest)

    @unittest.skipUnless(shutil.which("rsync") and shutil.which("sh"), "rsync and sh are required.")
    def test_snapshot_and_import_scripts_merge_by_default_and_delete_on_request(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            source = project / "source"
            target = project / "target"
            manifest = Path("models/manifests/registry.ollama.ai/library/example/latest")
            blob = Path("models/blobs") / ("sha256-" + "a" * 64)
            definition = json.dumps({"config": {"digest": "sha256:" + "a" * 64, "size": 4}, "layers": []})
            (source / manifest).parent.mkdir(parents=True)
            (source / manifest).write_text(definition)
            (source / blob).parent.mkdir()
            (source / blob).write_bytes(b"blob")
            v2_manifest = Path("models/manifests-v2/ollama.com/library/example/latest")
            manifest_blob = blob.with_name("sha256-" + sha256(definition.encode()).hexdigest())
            (source / manifest_blob).write_text(definition)
            (source / v2_manifest).parent.mkdir(parents=True)
            v2_link = posixpath.relpath(manifest_blob.as_posix(), v2_manifest.parent.as_posix())
            (source / v2_manifest).symlink_to(v2_link)
            (source / (str(blob) + "-partial")).touch()
            (source / "config").mkdir()
            (source / "config/example.model").write_text("FROM local\n")

            staging = project / "staging"
            staging.mkdir()
            archive = project / "models.tar"
            snapshot = transfer.SNAPSHOT_MODELS.replace("/ollama", str(source)).replace("/staging", str(staging))
            with archive.open("wb") as payload:
                subprocess.run(["sh", "-c", snapshot], stdout=payload, check=True)
            transfer.validate_model_archive(archive)
            with tarfile.open(archive) as snapshot_archive:
                self.assertNotIn(str(blob) + "-partial", snapshot_archive.getnames())

            (target / manifest).parent.mkdir(parents=True)
            (target / manifest).write_text("x" * len(definition))
            (target / manifest.with_name("obsolete")).touch()
            (target / v2_manifest).parent.mkdir(parents=True)
            (target / v2_manifest.with_name("obsolete")).symlink_to(v2_link)
            (target / blob).parent.mkdir()
            (target / "models/blobs/obsolete").touch()
            (target / "config").mkdir()
            (target / "config/obsolete.model").touch()
            (target / "outside-model-store").touch()

            staging = project / "import-staging"
            staging.mkdir()
            import_script = transfer.IMPORT_MODELS.replace("/ollama", str(target)).replace("/staging", str(staging))
            with archive.open("rb") as payload:
                subprocess.run(["sh", "-c", import_script], stdin=payload, check=True)
            self.assertEqual((target / manifest).read_text(), definition)
            self.assertEqual(os.readlink(target / v2_manifest), v2_link)
            self.assertEqual((target / v2_manifest).read_text(), definition)
            self.assertEqual((target / blob).read_bytes(), b"blob")
            self.assertEqual((target / "config/example.model").read_text(), "FROM local\n")
            self.assertTrue((target / manifest.with_name("obsolete")).is_file())
            self.assertTrue((target / v2_manifest.with_name("obsolete")).is_symlink())
            self.assertTrue((target / "models/blobs/obsolete").is_file())
            self.assertTrue((target / "config/obsolete.model").is_file())

            with archive.open("rb") as payload:
                subprocess.run(["sh", "-c", import_script, "loaf-model-import", "--delete-delay"],
                               stdin=payload, check=True)
            self.assertFalse((target / manifest.with_name("obsolete")).exists())
            self.assertFalse((target / v2_manifest.with_name("obsolete")).is_symlink())
            self.assertFalse((target / "models/blobs/obsolete").exists())
            self.assertFalse((target / "config/obsolete.model").exists())
            self.assertTrue((target / "outside-model-store").is_file())


class ProgressTests(unittest.TestCase):
    def test_image_export_reports_wait_and_throughput_before_the_pipe_closes(self):
        # The child waits for a status refresh, then for a small write to be
        # measured before it closes stdout. A full-buffer read would deadlock.
        script = """\
from pathlib import Path
import sys
from time import monotonic, sleep

def wait_for(marker):
    deadline = monotonic() + 5
    while not Path(marker).exists():
        if monotonic() >= deadline:
            sys.exit(f"No progress marker: {marker}")
        sleep(0.01)

wait_for("waiting")
sys.stdout.buffer.write(b"x" * 4096)
sys.stdout.buffer.flush()
wait_for("measured")
"""
        with TemporaryDirectory() as folder:
            root = Path(folder)
            waiting = root / "waiting"
            measured = root / "measured"

            class Output(io.StringIO):
                waits = 0

                def write(self, line):
                    if "0 B  0 B/s" in line and "waiting for Docker" in line:
                        self.waits += 1
                        if self.waits == 2:
                            waiting.touch()
                    if "4.0 KiB  " in line and "waiting for Docker" not in line and "done" not in line:
                        measured.touch()
                    return super().write(line)

            def fast_progress(*args, **kwargs):
                progress = Progress(*args, **kwargs)
                progress.interval = 0.01
                return progress

            archive = root / "images.tar"
            with redirect_stderr(Output()) as output, patch("loaf.transfer.Progress", side_effect=fast_progress):
                transfer.docker_archive(root, [sys.executable, "-c", script], archive, "Save images.tar")
            self.assertEqual(archive.read_bytes(), b"x" * 4096)
            self.assertTrue(measured.exists())
            self.assertTrue(output.getvalue().rstrip().endswith("done"))

    def test_terminal_redraws_bar_and_finishes_with_a_newline(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        with (
            redirect_stderr(Terminal()) as terminal,
            patch("loaf.progress.get_terminal_size", return_value=os.terminal_size((80, 24))),
            patch("loaf.progress.monotonic", side_effect=[0, 1, 2, 3]),
        ):
            with Progress("Load images.tar", 2048) as progress:
                progress.advance(1024)
                progress.advance(1024)
        output = terminal.getvalue()
        self.assertIn("\rLoad images.tar: [#####-----]  50%", output)
        self.assertIn("100% 2.0 KiB/2.0 KiB", output)
        self.assertTrue(output.rstrip().endswith("done"))
        self.assertTrue(output.endswith("\n"))


@unittest.skipUnless(os.name == "posix" and shutil.which("tar"), "Requires a POSIX shell and tar.")
class VolumeRestoreTests(unittest.TestCase):
    def test_populated_volume_is_preserved_and_partial_extract_is_cleaned(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ollama = root / "ollama"
            webui = root / "webui"
            ollama.mkdir()
            webui.mkdir()
            existing = webui / "account"
            existing.write_bytes(b"existing")
            script = transfer.RESTORE_VOLUMES.replace("/transfer", '"$TRANSFER_ROOT"')
            options = dict(env={**os.environ, "TRANSFER_ROOT": str(root)}, capture_output=True)
            result = subprocess.run(["sh", "-c", script], input=b"", **options)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"requires empty volumes", result.stderr)
            self.assertEqual(existing.read_bytes(), b"existing")

            existing.unlink()
            workspace = root / "workspace"
            workspace.mkdir()
            workspace_file = workspace / "source.c"
            workspace_file.write_bytes(b"personal files")
            result = subprocess.run(["sh", "-c", script, "restore", str(workspace)], input=b"", **options)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"requires empty volumes", result.stderr)
            self.assertEqual(workspace_file.read_bytes(), b"personal files")

            # A tar header followed by truncated content forces a partial file
            # to be extracted before tar exits with an error.
            data = b"x" * 1024
            payload = tar_payload("ollama/model", data)
            result = subprocess.run(["sh", "-c", script], input=payload[:600], **options)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(ollama.iterdir()), [])
            self.assertEqual(list(webui.iterdir()), [])
            result = subprocess.run(["sh", "-c", script], input=payload, **options)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((ollama / "model").read_bytes(), data)
