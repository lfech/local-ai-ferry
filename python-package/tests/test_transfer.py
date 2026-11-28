"""Exercise the typed archive workflow with a real checkout and simulated Docker."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import grp
import gzip
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import posixpath
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from time import sleep
import unittest
from unittest.mock import patch

from loaf import assets, cli, config, docs, setup, transfer
from loaf.progress import Progress, wait_status
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


def read_members(path):
    with tarfile.open(path, "r:gz") as archive:
        return {member.name: archive.extractfile(member).read() for member in archive}


def write_archive(path, members):
    """Write a gzip-compressed tar from (name, bytes) pairs in order."""
    with tarfile.open(path, "w:gz") as archive:
        for name, data in members:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))


KEY = b"archived-terminal-credential-0123456789\n"
OWNER = "a" * 64
ACCOUNT = transfer.workspace_prefix("") + OWNER


@unittest.skipUnless(shutil.which("git"), "Git is required for checkout backup.")
class TransferTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="loaf transfer ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "source project"
        self.project.mkdir()
        self.destination = self.root / "destination project"
        (self.project / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        (self.project / "docs").mkdir()
        (self.project / "docs/guide.md").write_bytes(b"Source documentation\n")
        (self.project / "docs/guide-link.md").symlink_to("guide.md")
        shutil.copyfile(ROOT / ".gitignore", self.project / ".gitignore")
        self.git("init", "--quiet")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "Initial commit")
        (self.project / "compose.yml").write_text("name: edited\nservices: {}\n", encoding="utf-8")
        (self.project / ".env").write_text("PRIVATE=value\n", encoding="utf-8")
        (self.project / "config").mkdir()
        self.settings = self.project / "config/config.ini"
        self.settings.write_text("[webui]\nport = 8818\n", encoding="utf-8")
        (self.project / "config/models").mkdir()
        (self.project / "config/models/blue.model").write_text("FROM model\n", encoding="utf-8")
        self.key = self.project / config.TERMINAL_SECRET
        self.key.write_bytes(KEY)
        self.wheels = self.project / setup.PYTHON_WHEEL_DIR
        self.wheels.mkdir(parents=True)
        package = self.project / setup.PYTHON_PACKAGE_DIR
        package.mkdir()
        (package / "pyproject.toml").write_bytes(b"# Exact Python package fixture\n")
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
        self.backup_dir = self.root / "transfer disk"
        self.executable = self.project / "dist/loaf"
        self.executable.parent.mkdir()
        self.executable.write_bytes(transfer.EXECUTABLE_HEADER + b"\n__LOAF_PAYLOAD__\n\x1f\x8bfixture")
        executable_patch = patch.dict(os.environ, LOAF_RELEASE_EXECUTABLE=str(self.executable))
        executable_patch.start()
        self.addCleanup(executable_patch.stop)
        clock_patch = patch("loaf.transfer.datetime")
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.clock.now.return_value = datetime(2026, 11, 20, 15, 0)
        self.bundle = self.backup_dir / f"20261120-1500_{transfer.BUNDLE_NAME}"
        self.archive = self.bundle / f"{self.bundle.name}.tar.gz"
        self.run = subprocess.run
        self.popen = subprocess.Popen
        self.docker_calls = []
        self.received = []
        self.failing = set()
        self.images = image_payload(transfer.image_names(config.DEPLOYMENT))
        self.terminal_image = None
        self.models = tar_payload("models", None, **{"models/blobs": None})
        self.webui = tar_payload("webui", None, **{"webui/webui.db": b"accounts and chats"})
        self.definitions = b""
        self.volumes = {}
        # Volumes whose emptiness check finds data.
        self.populated = set()

    def git(self, *arguments):
        return subprocess.check_output(["git", *arguments], cwd=self.project)

    def add_terminal_volume(self):
        self.settings.write_text("[terminal_server]\nenabled = true\n", encoding="utf-8")
        name = transfer.terminal_volume(config.DEPLOYMENT)
        self.volumes[name] = {"Name": name, "Labels": transfer.terminal_volume_labels(config.DEPLOYMENT), "Driver": "local",
                              "Options": None}

    def docker(self, command, **kwargs):
        if command[0] != "docker":
            return self.run(command, **kwargs)
        self.docker_calls.append(command)
        if any(failure in command for failure in self.failing if not failure.startswith(("save ", "restore "))):
            raise subprocess.CalledProcessError(23, command)
        output = b""
        if command[1:3] == ["image", "ls"]:
            if self.terminal_image and command[-1] == f"reference={self.terminal_image}":
                output = b"terminal-image-id\n"
        elif command[1:3] == ["volume", "ls"]:
            # Docker matches a name filter anywhere in the name.
            value = command[-1].removeprefix("name=")
            output = "\n".join(name for name in self.volumes if value in name).encode()
        elif command[1:3] == ["volume", "inspect"]:
            output = json.dumps([self.volumes[name] for name in command[3:] if name in self.volumes]).encode()
            # Like Docker, fail when a named volume is missing.
            if not set(command[3:]) <= set(self.volumes):
                return subprocess.CompletedProcess(command, 1, stdout=output)
        elif command[1:3] == ["run", "--rm"] and "find" in command:
            mount = next(part for part in command if part.startswith("type=volume,source="))
            output = b"/volume/data\n" if mount.split(",")[1].removeprefix("source=") in self.populated else b""
        elif command[1:3] == ["volume", "create"]:
            labels = dict(command[index + 1].split("=", 1) for index, part in enumerate(command) if part == "--label")
            driver = command[command.index("--driver") + 1] if "--driver" in command else "local"
            self.volumes.setdefault(command[-1], {"Name": command[-1], "Labels": labels, "Driver": driver, "Options": None})
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def docker_stream(self, command, **kwargs):
        if command[0] != "docker":
            return self.popen(command, **kwargs)
        self.docker_calls.append(command)
        targets = [part.split("target=/transfer/", 1)[1].split(",")[0] for part in command if "target=/transfer/" in part]
        payload = b""
        if command[1:3] == ["image", "save"]:
            label, payload = "save images", self.images
        elif command[1:3] == ["image", "load"]:
            label = "load"
        elif transfer.SNAPSHOT_MODELS in command:
            label, payload = "save models", self.models
        elif transfer.IMPORT_MODELS in command:
            label = "restore models"
        elif "loaf-check-modelfiles" in command:
            label, payload = "check modelfiles", self.definitions
        elif transfer.RESTORE_VOLUMES in command:
            label = "restore " + ",".join(targets)
        else:
            label = "save " + ",".join(targets)
            payload = self.webui if targets == ["webui"] else tar_payload(
                targets[0], None, **{name: None for name in targets[1:]}, **{f"{targets[0]}/notes.txt": b"files"})
        if label in self.failing:
            code = 'import sys; print("Docker refused the payload.", file=sys.stderr); sys.exit(23)'
            return self.popen([sys.executable, "-c", code], **kwargs)
        if command[1:3] == ["image", "load"] or "-i" in command:
            received = self.root / f"received-{len(self.received)}"
            self.received.append((label, received))
            code = "import sys; open(sys.argv[1], 'wb').write(sys.stdin.buffer.read())"
            return self.popen([sys.executable, "-c", code, str(received)], **kwargs)
        source = self.root / f"payload-{len(self.docker_calls)}"
        source.write_bytes(payload)
        code = "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())"
        return self.popen([sys.executable, "-c", code, str(source)], **kwargs)

    def received_payloads(self):
        return [(label, path.read_bytes()) for label, path in self.received]

    def invoke(self, mode, *arguments, custom=True, project="source"):
        options = ["--backup-dir", str(self.backup_dir)] if custom else []
        project = self.project if project == "source" else project
        location = ["--project-dir", str(project)] if project is not None else []
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
            return cli.main([*location, *([mode] if mode else []), *options, *arguments])

    def export_wheels(self, project):
        self.assertEqual(project, self.project.resolve())
        if self.fail_wheel_export:
            raise setup.SetupError("Cannot export Python wheels from the local cache.")
        self.launcher_wheel.write_bytes(b"fresh launcher wheel")
        (self.wheels / setup.WHEEL_CACHE_RECORD).write_text("{}", encoding="utf-8")
        return self.wheels

    def manifest(self, archive=None):
        with tarfile.open(archive or self.archive, "r:gz") as members:
            first = members.next()
            self.assertEqual(first.name, transfer.MANIFEST)
            return json.load(members.extractfile(first))

    def test_backup_writes_a_typed_archive_and_restore_applies_every_type(self):
        self.assertEqual(self.invoke("backup"), 0, self.progress.getvalue())
        # loaf.json records the launcher's hash; SHA256SUMS belongs to release downloads.
        self.assertEqual(sorted(path.name for path in self.bundle.iterdir()),
                         sorted([transfer.LAUNCHER, self.archive.name]))
        self.assertEqual((self.bundle / "loaf").read_bytes(), self.executable.read_bytes())
        self.assertTrue(os.access(self.bundle / "loaf", os.X_OK))
        self.assertEqual(self.manifest()["launcher"], {
            "size": self.executable.stat().st_size, "sha256": setup.file_digest(self.executable)})

        members = read_members(self.archive)
        self.assertEqual(list(members), [transfer.MANIFEST, "source.tar", "config.tar", "installers.tar",
                                         "images.tar", "models.tar", "webui.tar"])
        manifest = self.manifest()
        self.assertEqual((manifest["format"], manifest["version"], manifest["deployment"]),
                         (transfer.ARCHIVE_FORMAT, transfer.ARCHIVE_VERSION, ""))
        self.assertEqual([item["type"] for item in manifest["items"]],
                         ["source", "config", "installers", "images", "models", "webui"])
        for item in manifest["items"]:
            for member in item["members"]:
                self.assertEqual((member["size"], member["sha256"]),
                                 (len(members[member["path"]]), sha256(members[member["path"]]).hexdigest()))
        self.assertEqual(manifest["items"][3]["images"], list(transfer.image_names(config.DEPLOYMENT)))
        self.assertEqual((members["images.tar"], members["models.tar"], members["webui.tar"]),
                         (self.images, self.models, self.webui))
        with tarfile.open(fileobj=io.BytesIO(members["source.tar"])) as source:
            names = source.getnames()
            self.assertIn(".git/HEAD", names)
            self.assertEqual(source.extractfile("compose.yml").read(), b"name: edited\nservices: {}\n")
            self.assertFalse([name for name in names if name.split("/")[0] in ("install-cache", ".venv", "config")])
            self.assertNotIn(".env", names)
        with tarfile.open(fileobj=io.BytesIO(members["config.tar"])) as settings:
            self.assertEqual(sorted(settings.getnames()), ["config/config.ini", "config/models/blue.model"])
        with tarfile.open(fileobj=io.BytesIO(members["installers.tar"])) as installers:
            names = installers.getnames()
            self.assertIn(f"{setup.PYTHON_WHEEL_DIR}/{setup.WHEEL_CACHE_RECORD}", names)
            self.assertEqual(installers.extractfile(self.launcher_wheel.relative_to(self.project).as_posix()).read(),
                             b"fresh launcher wheel")
            self.assertEqual(installers.extractfile(self.toolkit.relative_to(self.project).as_posix()).read(),
                             b"fixture deb")
            self.assertIn(f"{self.docker_cache.relative_to(self.project)}/Packages", names)
            self.assertNotIn(f"{setup.INSTALL_CACHE}/scratch.txt", names)
        stop = cli.Compose(self.project).command(cli.STOP_WITH_TERMINAL)
        self.assertLess(self.docker_calls.index(stop), next(
            index for index, call in enumerate(self.docker_calls) if transfer.SNAPSHOT_MODELS in call))
        progress = self.progress.getvalue()
        for phase in ("Archive source.tar", "Save images.tar", "Save models.tar", "Save webui.tar",
                      "Hash webui.tar", "Compress webui.tar"):
            self.assertIn(phase, progress)

        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False, project=self.destination),
                         0, self.progress.getvalue())
        self.assertEqual(self.received_payloads(), [
            ("load", self.images), ("restore models", self.models), ("restore webui", self.webui),
        ])
        self.assertEqual((self.destination / "compose.yml").read_bytes(), b"name: edited\nservices: {}\n")
        self.assertEqual(os.readlink(self.destination / "docs/guide-link.md"), "guide.md")
        self.assertEqual((self.destination / ".git/HEAD").read_bytes(), (self.project / ".git/HEAD").read_bytes())
        self.assertEqual(config.load(self.destination / "config").port, 8818)
        self.assertEqual((self.destination / "config/models/blue.model").read_text(), "FROM model\n")
        self.assertEqual((self.destination / self.toolkit.relative_to(self.project)).read_bytes(), b"fixture deb")
        # Outside the system deployment, restored settings stay owner-only.
        self.assertEqual(stat.S_IMODE((self.destination / "config/config.ini").stat().st_mode), 0o600)
        self.assertFalse((self.destination / ".venv").exists())
        self.assertFalse((self.destination / config.TERMINAL_SECRET).exists())
        self.assertFalse(list(self.destination.glob(".loaf-restore-*")))
        self.assertIn("Restored source, config, installers, images, models, webui", self.output.getvalue())

    def test_types_and_aliases_select_members_and_terminal_adds_webui(self):
        for arguments, members, docker in (
            (("project",), ["config.tar", "installers.tar"], False),
            (("volumes",), ["models.tar", "webui.tar"], True),
            # Without a user volume yet, only the key is saved.
            (("terminal",), ["webui.tar", "terminal/terminal-server.key"], True),
            (("config", "images"), ["config.tar", "images.tar"], True),
        ):
            with self.subTest(arguments=arguments):
                shutil.rmtree(self.backup_dir, ignore_errors=True)
                self.docker_calls.clear()
                self.assertEqual(self.invoke("backup", *arguments), 0, self.progress.getvalue())
                self.assertEqual(list(read_members(self.archive)), [transfer.MANIFEST, *members])
                self.assertEqual(any(call[:2] != ["docker", "compose"] for call in self.docker_calls), docker)
        self.add_terminal_volume()
        shutil.rmtree(self.backup_dir)
        self.assertEqual(self.invoke("backup", "volumes"), 0, self.progress.getvalue())
        self.assertEqual([item["type"] for item in self.manifest()["items"]], ["models", "webui", "terminal"])

        for arguments in (("unknown",), ("config", "config"), ("--verify", "config")):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.invoke("backup", *arguments)
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "images", "--archive", str(self.bundle), custom=False,
                                     project=self.destination), 1)
        self.assertIn("does not contain images", self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke("restore", "config", "--delete", "--archive", str(self.bundle), custom=False)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke("restore", "config", "--import", "--archive", str(self.bundle), custom=False)

    def test_terminal_round_trip_restores_the_user_volume_and_key(self):
        self.add_terminal_volume()
        self.assertEqual(self.invoke("backup", "terminal"), 0, self.progress.getvalue())
        manifest = self.manifest()
        terminal = manifest["items"][1]
        self.assertEqual([item["type"] for item in manifest["items"]], ["webui", "terminal"])
        self.assertEqual([member["path"] for member in terminal["members"]],
                         [transfer.TERMINAL_VOLUME, transfer.TERMINAL_KEY])
        members = read_members(self.archive)
        self.assertEqual(members[transfer.TERMINAL_KEY], KEY)
        users = transfer.terminal_volume(config.DEPLOYMENT)
        saved = [call for call in self.docker_calls if "tar" in call and transfer.RESTORE_VOLUMES not in call]
        self.assertIn(f"type=volume,source={users},target=/transfer/users,volume-nocopy,readonly", saved[-1])

        self.volumes.clear()
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False, project=self.destination),
                         0, self.progress.getvalue())
        # Restore creates the WebUI volume with Compose's labels, so Compose adopts it without a warning.
        self.assertEqual({name: volume["Labels"] for name, volume in self.volumes.items()}, {
            "loaf-open-webui": {"com.docker.compose.project": "loaf", "com.docker.compose.volume": "open-webui"},
            users: transfer.terminal_volume_labels(config.DEPLOYMENT),
        })
        self.assertEqual([label for label, _ in self.received], ["restore webui", "restore users"])
        key = self.destination / config.TERMINAL_SECRET
        self.assertEqual(key.read_bytes(), KEY)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertNotIn(KEY.strip().decode(), self.output.getvalue() + self.progress.getvalue())

        # A different key at the destination stops the restore before any Docker change.
        key.write_bytes(b"another-terminal-credential-0123456789\n")
        self.volumes.clear()
        self.received.clear()
        self.assertEqual(self.invoke("restore", "terminal", "--archive", str(self.bundle), custom=False,
                                     project=self.destination), 1)
        self.assertIn("differs from the backup", self.progress.getvalue())
        self.assertEqual((self.volumes, self.received), ({}, []))

        # A failed user volume restore leaves no new key behind.
        key.unlink()
        self.failing = {"restore users"}
        self.assertEqual(self.invoke("restore", "terminal", "--archive", str(self.bundle), custom=False,
                                     project=self.root / "third project"), 23)
        self.assertFalse((self.root / "third project" / config.TERMINAL_SECRET).exists())

    def test_terminal_backup_without_a_user_volume_saves_only_the_key(self):
        self.settings.write_text("[terminal_server]\nenabled = true\n", encoding="utf-8")
        self.assertEqual(self.invoke("backup", "terminal"), 0, self.progress.getvalue())
        self.assertEqual([member["path"] for member in self.manifest()["items"][1]["members"]], [transfer.TERMINAL_KEY])
        self.volumes.clear()
        self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False, project=self.destination),
                         0, self.progress.getvalue())
        self.assertEqual(list(self.volumes), ["loaf-open-webui"])
        self.assertEqual((self.destination / config.TERMINAL_SECRET).read_bytes(), KEY)

    def test_versioned_backups_restore_only_with_the_matching_version(self):
        namespace = "loaf-v2"
        self.images = image_payload(transfer.image_names(namespace))
        self.assertEqual(self.invoke("backup", "config", "images", "--namespace", "v2"), 0, self.progress.getvalue())
        bundle, = self.backup_dir.iterdir()
        self.assertEqual(bundle.name, f"20261120-1500_v2_{transfer.BUNDLE_NAME}")
        self.assertEqual(self.manifest(next(bundle.glob("*.tar.gz")))["deployment"], "v2")
        self.assertEqual(self.invoke("restore", "--namespace", "v3", project=self.destination), 1)
        self.assertIn(f"No backup found in {self.backup_dir}. It holds backups of other namespaces. "
                      f"Select one with --archive, for example --archive {shlex.quote(str(bundle))}.",
                      self.progress.getvalue())
        self.assertEqual(self.invoke("restore", "--archive", str(bundle), "--namespace", "v3", custom=False,
                                     project=self.destination), 1)
        self.assertIn("The backup is of namespace loaf-v2", self.progress.getvalue())
        self.assertEqual(self.invoke("restore", "--namespace", "v2", project=self.destination), 0, self.progress.getvalue())
        self.assertEqual(config.load(self.destination / "config").namespace_name, "v2")

    def test_config_links_stay_links_and_verify_runs_the_restore_checks(self):
        examples = self.project / "config/model-examples"
        examples.mkdir()
        (examples / "models.8gb.ini").write_text("[models.chat]\nmodel = hf.co/a/b:Q4_K_M\n", encoding="utf-8")
        (self.project / "config/models.ini").symlink_to("model-examples/models.8gb.ini")
        self.assertEqual(self.invoke("backup", "config"), 0, self.progress.getvalue())
        with tarfile.open(fileobj=io.BytesIO(read_members(self.archive)["config.tar"])) as settings:
            link = settings.getmember("config/models.ini")
            self.assertEqual((link.issym(), link.linkname), (True, "model-examples/models.8gb.ini"))
        # Verify streams every member through restore's checks without a temporary copy.
        with patch("loaf.transfer.TemporaryDirectory", side_effect=AssertionError("verify staged a member")):
            self.assertEqual(self.invoke("backup", "--verify", "--archive", str(self.bundle), custom=False), 0,
                             self.progress.getvalue())
        self.assertIn("Verified SHA-256 hashes and contents", self.output.getvalue())
        (self.destination / "config").mkdir(parents=True)
        (self.destination / "config/models.ini").symlink_to("previous.ini")
        self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False, project=self.destination),
                         0, self.progress.getvalue())
        restored = self.destination / "config/models.ini"
        self.assertEqual(os.readlink(restored), "model-examples/models.8gb.ini")
        self.assertEqual(restored.read_text(encoding="utf-8"), "[models.chat]\nmodel = hf.co/a/b:Q4_K_M\n")
        # The restored settings make the directory a project that uses this loaf's Docker project, without a link.
        self.assertFalse((self.destination / "docker-project").exists())
        self.assertTrue((self.destination / "config/config.ini").is_file())

    def test_links_that_leave_their_backup_scope_are_refused(self):
        outside = self.root / "outside.ini"
        outside.write_text("[webui]\n", encoding="utf-8")
        for name, target in (("absolute.ini", outside), ("parent.ini", Path("../../outside.ini")),
                             ("unarchived.ini", Path("notes.txt"))):
            with self.subTest(name=name):
                (self.project / "config/notes.txt").write_text("not a setting\n", encoding="utf-8")
                link = self.project / "config" / name
                link.symlink_to(target)
                self.assertEqual(self.invoke("backup", "config"), 1)
                self.assertIn(f"{link} links to {target}", self.progress.getvalue())
                link.unlink()
        self.assertFalse(self.backup_dir.exists() and list(self.backup_dir.iterdir()))
        (self.project / "docs/escape.md").symlink_to("../../outside.ini")
        self.git("add", "docs/escape.md")
        self.assertEqual(self.invoke("backup", "source"), 1)
        self.assertIn("outside the checkout", self.progress.getvalue())
        # Restore refuses the same links in an archive written elsewhere.
        for linkname in ("../outside.ini", "/etc/hostname", "missing.ini"):
            with self.subTest(linkname=linkname):
                payload = io.BytesIO()
                with tarfile.open(fileobj=payload, mode="w") as archive:
                    member = tarfile.TarInfo("config/models.ini")
                    member.type, member.linkname = tarfile.SYMTYPE, linkname
                    archive.addfile(member)
                with self.assertRaisesRegex(transfer.TransferError, "Invalid link in config.tar"):
                    transfer.check_files(io.BytesIO(payload.getvalue()), "config", "config.tar")

    def test_restore_checks_target_volumes_before_the_first_change(self):
        self.add_terminal_volume()
        self.assertEqual(self.invoke("backup", "config", "terminal"), 0, self.progress.getvalue())
        self.received.clear()
        self.volumes = {"loaf-open-webui": {"Name": "loaf-open-webui", "Labels": {}, "Driver": "local", "Options": None}}
        self.populated = {"loaf-open-webui"}
        self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False, project=self.destination), 1)
        self.assertIn("Restore needs a new or empty loaf-open-webui volume", self.progress.getvalue())
        self.assertFalse((self.destination / "config/config.ini").exists())
        self.assertEqual(self.received, [])
        # The user volume's ownership labels are checked before any change, too.
        users = transfer.terminal_volume(config.DEPLOYMENT)
        self.populated = set()
        self.volumes[users] = {"Name": users, "Labels": {"other": "owner"}, "Driver": "local", "Options": None}
        self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False, project=self.destination), 1)
        self.assertIn(f"Terminal volume {users} does not match", self.progress.getvalue())
        self.assertFalse((self.destination / "config/config.ini").exists())
        self.assertEqual(self.received, [])

    def test_no_terminal_server_backup_restores_without_the_terminal(self):
        self.add_terminal_volume()
        self.settings.write_text("# Local settings\n[terminal_server]\n# The terminal is on here.\nenabled = true\n",
                                 encoding="utf-8")
        self.terminal_image = transfer.image_names(config.DEPLOYMENT, include_terminal=True)[-1]
        # By default the terminal type, with the key, follows an enabled terminal server.
        self.assertEqual(self.invoke("backup", "config", "volumes"), 0, self.progress.getvalue())
        self.assertIn("terminal", [item["type"] for item in self.manifest()["items"]])
        shutil.rmtree(self.backup_dir)
        self.assertEqual(self.invoke("backup", "--no-terminal-server"), 0, self.progress.getvalue())
        manifest = self.manifest()
        self.assertIs(manifest["skip_terminal_server"], True)
        self.assertNotIn("terminal", [item["type"] for item in manifest["items"]])
        images = next(item for item in manifest["items"] if item["type"] == "images")["images"]
        self.assertNotIn(self.terminal_image, images)
        self.assertNotIn(transfer.TERMINAL_KEY, read_members(self.archive))

        self.volumes.clear()
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False, project=self.destination),
                         0, self.progress.getvalue())
        restored = (self.destination / "config/config.ini").read_text(encoding="utf-8")
        self.assertEqual(restored, "# Local settings\n[terminal_server]\n# The terminal is on here.\nenabled = false\n")
        drop = [call for call in self.docker_calls if transfer.DROP_TERMINAL_CONNECTION in call]
        self.assertEqual(len(drop), 1)
        self.assertIn("type=volume,source=loaf-open-webui,target=/app/backend/data,volume-nocopy", drop[0])
        self.assertIn("--network", drop[0])
        self.assertIn("The restored settings disable it.", self.output.getvalue())
        self.assertIn("Next: ", self.output.getvalue())
        self.assertTrue(self.output.getvalue().rstrip().endswith("offline"))

    def test_restore_edits_single_settings_lines_and_migrates_renamed_ones(self):
        self.settings.write_text("# Keep this comment.\n[webui]\n# Port comment\nport = 8818\n", encoding="utf-8")
        self.images = image_payload(transfer.image_names("loaf-v2"))
        self.assertEqual(self.invoke("backup", "config", "--namespace", "v2"), 0, self.progress.getvalue())
        self.assertEqual(self.invoke("restore", "--namespace", "v2", project=self.destination), 0, self.progress.getvalue())
        self.assertEqual((self.destination / "config/config.ini").read_text(encoding="utf-8"),
                         "# Keep this comment.\n[webui]\n# Port comment\nport = 8818\n\n[launcher]\nnamespace = v2\n")
        # Backups from before the rename carry [launcher] version.
        recovered = self.root / "recovered"
        recovered.mkdir()
        (recovered / "config.ini").write_text("[launcher]\n# Name\nversion = v2\n", encoding="utf-8")
        settings = transfer.stage_configuration(recovered, (), self.destination / "config", "v2")
        self.assertEqual(settings.namespace_name, "v2")
        self.assertEqual((recovered / "config.ini").read_text(encoding="utf-8"), "[launcher]\n# Name\nnamespace = v2\n")

    def test_dry_runs_report_the_errors_of_a_real_run(self):
        self.assertEqual(self.invoke("backup", "config"), 0, self.progress.getvalue())
        self.assertEqual(self.invoke("restore", "terminal", "--dry-run", "--archive", str(self.bundle), custom=False,
                                     project=self.destination), 1)
        self.assertIn("does not contain webui, terminal", self.progress.getvalue())
        self.destination.mkdir()
        self.assertEqual(self.invoke("backup", "source", "--dry-run", project=self.destination), 1)
        self.assertIn("The source type needs a Git checkout", self.progress.getvalue())
        self.assertEqual(self.invoke("restore", "--dry-run", custom=False, project=self.root / "empty"), 1)
        self.assertIn("No backup found", self.progress.getvalue())
        # A mistyped project directory is reported, not created.
        self.assertEqual(self.invoke("restore", custom=False, project=self.root / "typo"), 1)
        self.assertIn("No backup found", self.progress.getvalue())
        self.assertFalse((self.root / "typo").exists())
        # With a backup, restore creates the missing project directory.
        new_project = self.root / "new project"
        self.assertEqual(self.invoke("restore", "config", "--archive", str(self.bundle), custom=False,
                                     project=new_project), 0, self.progress.getvalue())
        self.assertTrue((new_project / "config/config.ini").is_file())

    def test_restore_without_the_terminal_key_suggests_setup(self):
        self.settings.write_text("[terminal_server]\nenabled = true\n", encoding="utf-8")
        self.assertEqual(self.invoke("backup", "config"), 0, self.progress.getvalue())
        self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False, project=self.destination),
                         0, self.progress.getvalue())
        next_line, = [line for line in self.output.getvalue().splitlines() if line.startswith("Next: ")]
        self.assertTrue(next_line.endswith(" setup terminal-server-key"), next_line)

    def test_a_release_download_folder_is_no_backup_folder(self):
        # A release download holds loaf and the release's SHA256SUMS, but no archive that begins with loaf.json.
        download = self.root / "release download"
        project = download / "loaf-project"
        project.mkdir(parents=True)
        shutil.copyfile(self.executable, download / "loaf")
        source = download / "loaf-v0.5.0-source.tar.gz"
        with tarfile.open(source, "w:gz") as archive:
            entry = tarfile.TarInfo("loaf-v0.5.0")
            entry.type = tarfile.DIRTYPE
            archive.addfile(entry)
        (download / transfer.CHECKSUMS).write_text("".join(
            f"{'0' * 64}  {name}\n" for name in ("loaf", "LICENSE.txt", "loaf_0.5.0_all.deb", source.name)))
        self.assertFalse(transfer.is_bundle(download))
        with patch.dict(os.environ, LOAF_RELEASE_EXECUTABLE=str(download / "loaf")):
            self.assertIsNone(transfer.find_bundle(project, None))
            with (patch.object(setup, "bundled_release", return_value=download / "runtime"),
                  patch.object(setup, "prepare_configuration") as prepared):
                self.assertEqual(self.invoke("setup", custom=False, project=project), 0, self.progress.getvalue())
        prepared.assert_called_once()
        self.assertNotIn("restore", self.output.getvalue())

    def test_a_backup_folder_from_before_0_5_0_moves_with_its_checksums(self):
        self.assertEqual(self.invoke("backup", "config"), 0, self.progress.getvalue())
        folder, before = self.copied_bundle("older backup")
        (folder / transfer.CHECKSUMS).write_text(f"{'0' * 64}  loaf\n{'0' * 64}  {self.archive.name}\n")
        self.assertTrue(transfer.is_bundle(folder))
        moved = transfer.adopt_bundle(folder)
        self.assertEqual(sorted(path.name for path in moved.iterdir()),
                         sorted([transfer.LAUNCHER, transfer.CHECKSUMS, self.archive.name]))
        self.assertEqual((folder / "loaf").read_bytes(), before["loaf"])

    def test_restore_aliases_select_the_types_that_the_archive_contains(self):
        self.assertEqual(self.invoke("backup", "config", "webui"), 0, self.progress.getvalue())
        archive = transfer.open_archive(self.bundle)
        self.assertEqual(transfer.requested_types(archive, ("volumes",)), ("webui",))
        self.assertEqual(transfer.requested_types(archive, ("project",)), ("config",))
        for types, message in ((("terminal",), "does not contain terminal"), (("models",), "does not contain models"),
                               (("installers",), "does not contain installers")):
            with self.subTest(types=types), self.assertRaisesRegex(transfer.TransferError, message):
                transfer.requested_types(archive, types)
        self.assertEqual(self.invoke("restore", "volumes", "--archive", str(self.bundle), custom=False,
                                     project=self.destination), 0, self.progress.getvalue())
        self.assertIn("Restored webui from", self.output.getvalue())

    def test_restore_checks_docker_before_it_reads_the_archive(self):
        self.assertEqual(self.invoke("backup", "config", "webui"), 0, self.progress.getvalue())
        webui = next(name for name, role in transfer.volume_names(config.DEPLOYMENT).items() if role == "webui")
        self.volumes[webui] = {"Name": webui, "Labels": {}, "Driver": "local", "Options": None}
        self.populated.add(webui)
        with patch.object(transfer, "unpack", side_effect=AssertionError("read the archive")) as unpack:
            self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False,
                                         project=self.destination), 1)
        unpack.assert_not_called()
        self.assertIn(f"Restore needs a new or empty {webui} volume, but it holds data", self.progress.getvalue())
        self.assertNotIn("--namespace", self.progress.getvalue())
        self.assertFalse((self.destination / "config").exists())

    def test_missing_toolkit_packages_only_warn(self):
        with patch("loaf.transfer.toolkit_files", side_effect=setup.SetupError("packages are missing")):
            options = ["--project-dir", str(self.project), "backup", "installers", "--backup-dir", str(self.backup_dir)]
            with (patch("loaf.transfer.prepare_python_wheels", side_effect=self.export_wheels),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as progress):
                self.assertEqual(cli.main(options), 0, progress.getvalue())
        warning = progress.getvalue()
        self.assertIn("holds no NVIDIA Container Toolkit packages: packages are missing", warning)
        for command in ("loaf setup\n", "loaf setup toolkit\n", "apt-get download nvidia-container-toolkit"):
            self.assertIn(command, warning)
        with tarfile.open(fileobj=io.BytesIO(read_members(self.archive)["installers.tar"])) as installers:
            self.assertFalse([name for name in installers.getnames() if "/deb/nvidia/" in name])

    def test_backup_names_the_command_that_resumes_serving(self):
        self.assertEqual(self.invoke("backup", "config", "webui"), 0, self.progress.getvalue())
        self.assertIn("The backup stopped the deployment's services. Resume serving with: ", self.output.getvalue())
        self.assertTrue(self.output.getvalue().rstrip().endswith(" offline"))
        self.assertEqual(self.invoke("backup", "config"), 0, self.progress.getvalue())
        self.assertNotIn("Resume serving", self.output.getvalue())

    def test_installers_carry_the_cached_python_runtime(self):
        runtime = self.project / setup.PYTHON_RUNTIME_DIR / "cpython-runtime.tar.gz"
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b"checked runtime download")
        (runtime.parent / ".loaf-download-partial").write_bytes(b"partial")
        self.assertEqual(self.invoke("backup", "installers"), 0, self.progress.getvalue())
        with tarfile.open(fileobj=io.BytesIO(read_members(self.archive)["installers.tar"])) as installers:
            names = installers.getnames()
        self.assertIn(runtime.relative_to(self.project).as_posix(), names)
        self.assertFalse([name for name in names if ".loaf-download-" in name])

    def test_changed_old_or_newer_archives_fail_before_any_change(self):
        self.assertEqual(self.invoke("backup", "project", "images"), 0, self.progress.getvalue())
        members = read_members(self.archive)
        original = self.archive.read_bytes()

        def attempt(message):
            self.docker_calls.clear()
            self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False,
                                         project=self.destination), 1)
            self.assertIn(message, self.progress.getvalue())
            self.assertEqual([call for call in self.docker_calls if call[:2] != ["docker", "compose"]], [])
            self.assertFalse((self.destination / "config").exists())

        write_archive(self.archive, [(name, data + b"x" if name == "images.tar" else data)
                                     for name, data in members.items()])
        attempt("Size mismatch for images.tar")
        manifest = json.loads(members[transfer.MANIFEST])
        write_archive(self.archive, [(name, b"0" * len(data) if name == "images.tar" else data)
                                     for name, data in members.items()])
        attempt("SHA-256 verification failed for images.tar")
        # loaf.json's member hashes decide: the same members compressed anew still verify.
        write_archive(self.archive, members.items())
        self.assertEqual(self.invoke("backup", "--verify", "--archive", str(self.bundle), custom=False), 0,
                         self.progress.getvalue())
        self.archive.write_bytes(original[:len(original) // 2])
        attempt(f"{self.archive.name} is truncated or damaged")
        self.archive.write_bytes(original)
        # Another launcher only gets a warning: the running loaf restores the archive.
        (self.bundle / "loaf").write_bytes(self.executable.read_bytes() + b"changed")
        self.assertEqual(self.invoke("backup", "--verify", "--archive", str(self.bundle), custom=False), 0)
        self.assertIn(f"differs from the launcher that {transfer.MANIFEST} records", self.progress.getvalue())
        shutil.copyfile(self.executable, self.bundle / "loaf")
        self.assertEqual(self.invoke("backup", "--verify", "--archive", str(self.bundle), custom=False), 0)
        self.assertIn(f"matches the hash that {transfer.MANIFEST} records", self.output.getvalue())

        loose = self.root / "loose.tar.gz"
        for name, data, message in (
            ("old format", [("project.tar", b""), ("sha256.json", b"{}")], "does not begin with loaf.json"),
            ("newer format", [(transfer.MANIFEST, json.dumps({**manifest, "version": 4}).encode())],
             "uses archive format 4"),
            ("unknown type", [(transfer.MANIFEST, json.dumps({**manifest, "items": [
                {"type": "notes", "members": [{"path": "notes.tar", "size": 0, "sha256": "0" * 64}]}]}).encode())],
             "unexpected item 'notes'"),
            ("terminal without webui", [(transfer.MANIFEST, json.dumps({**manifest, "items": [
                {"type": "terminal", "members": [{"path": transfer.TERMINAL_KEY, "size": 0, "sha256": "0" * 64}]}]}).encode())],
             "terminal without the WebUI data"),
        ):
            with self.subTest(name):
                write_archive(loose, data)
                self.assertEqual(self.invoke("restore", "--archive", str(loose), custom=False,
                                             project=self.destination), 1)
                self.assertIn(message, self.progress.getvalue())
        self.settings.write_text("[transfer]\nbackup_version_min = 4\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.invoke("restore", "--archive", str(self.bundle), custom=False)
        self.assertIn("expected archive format 3", self.progress.getvalue())

    def copied_bundle(self, name):
        """Copy the backup folder under another name, as transfer media often renames it."""
        folder = (self.root / name).resolve()
        shutil.copytree(self.bundle, folder)
        return folder, {path.name: path.read_bytes() for path in folder.iterdir()}

    def test_setup_and_restore_inside_a_backup_folder_make_it_the_project(self):
        self.assertEqual(self.invoke("backup", "project"), 0, self.progress.getvalue())
        folder, before = self.copied_bundle("copied backup")
        moved = folder / "backup" / self.bundle.name
        with (
            patch.object(Path, "cwd", return_value=folder),
            patch.object(setup, "bundled_release", return_value=folder / "runtime"),
            patch.object(setup, "prepare_configuration") as prepared,
            patch.object(setup, "prepare_docker") as docker,
            patch.object(setup, "install_toolkit") as toolkit,
            patch("loaf.cli.assets.system_deployment_installed", return_value=False),
        ):
            for arguments in (("status",), (None,)):
                with self.subTest(arguments=arguments):
                    self.assertEqual(self.invoke(*arguments, custom=False, project=None), 2)
                    hints = self.progress.getvalue()
                    self.assertIn(f"{folder} is a backup folder. The typical next steps in it are:", hints)
                    for step in ("./loaf setup ", "./loaf restore ", "./loaf run offline ", "./loaf docs transfer"):
                        self.assertIn(step, hints)
                    self.assertIn(f"cd ~/loaf && {shlex.quote(str(folder / 'loaf'))} setup", hints)
                    self.assertNotIn("--system", hints)
            self.assertEqual(self.invoke("setup", "--dry-run", custom=False, project=None), 0, self.progress.getvalue())
            self.assertIn(f"Move the backup to {folder / 'backup'} and make {folder} the project.", self.output.getvalue())
            self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, before)

            self.assertEqual(self.invoke("setup", "all", "docker-deps", "toolkit", custom=False, project=None), 0,
                             self.progress.getvalue())
            self.assertIn(f"{folder} is the project now", self.progress.getvalue())
            # The project finds the moved backup as its newest one, so the hint needs no --archive.
            self.assertIn("Next: ./loaf restore\nRead the guides with ./loaf docs, for example ./loaf docs transfer.\n",
                          self.output.getvalue())
            self.assertEqual(self.invoke("restore", "config", custom=False, project=None), 0, self.progress.getvalue())
            self.assertIn("Next: ./loaf run offline\n", self.output.getvalue())
        self.assertEqual({path.name: path.read_bytes() for path in moved.iterdir()}, before)
        self.assertTrue(transfer.is_bundle(moved))
        self.assertFalse(transfer.is_bundle(folder))
        self.assertEqual(prepared.call_args.args[0], folder / "config")
        # The backup's installers supply the host packages without downloads.
        docker.assert_called_once_with(folder, allow_downloads=False)
        toolkit.assert_called_once_with(folder, allow_downloads=False)
        self.assertEqual((folder / self.docker_cache.relative_to(self.project) / "Packages").read_bytes(),
                         b"fixture package index")
        self.assertEqual((folder / "loaf").read_bytes(), self.executable.read_bytes())
        self.assertTrue(os.access(folder / "loaf", os.X_OK))
        self.assertEqual(config.load(folder / "config").port, 8818)
        # Only setup terminal-server-key creates a key, and this backup has none.
        self.assertFalse((folder / config.TERMINAL_SECRET).exists())
        self.assertFalse(list(folder.glob(".loaf-*")))
        self.assertEqual([call for call in self.docker_calls if call[:2] != ["docker", "compose"]], [])

    def test_system_restore_shares_files_with_the_group_and_keeps_the_terminal_key(self):
        self.add_terminal_volume()
        self.settings.write_text("[webui]\nport = 8818\n[terminal_server]\nenabled = true\n", encoding="utf-8")
        self.assertEqual(self.invoke("backup", "config", "installers", "terminal"), 0, self.progress.getvalue())
        system, state = self.root / "etc loaf", self.root / "var lib loaf"
        system.mkdir()
        state.mkdir()
        key = system / "terminal-server.key"
        (system / "config.ini").write_text(f"[terminal_server]\nsecret_file = {key}\n", encoding="utf-8")
        self.volumes.clear()
        with (
            patch.dict(os.environ, LOAF_SYSTEM_CONFIG_DIR=str(system)),
            patch.object(assets, "SYSTEM_PROJECT_DIR", state),
            patch("loaf.transfer.SYSTEM_GROUP", grp.getgrgid(os.getgid()).gr_name),
        ):
            self.assertEqual(self.invoke("restore", "--system", "--archive", str(self.bundle), custom=False,
                                         project=None), 0, self.progress.getvalue())

        def access(path):
            return stat.S_IMODE(path.stat().st_mode), path.stat().st_gid

        # Settings and installers are readable by the deployment directories' group.
        self.assertEqual(access(system / "config.ini"), (0o640, system.stat().st_gid))
        self.assertEqual(access(system / "models"), (0o750, system.stat().st_gid))
        self.assertEqual(access(system / "models/blue.model"), (0o640, system.stat().st_gid))
        wheel = state / self.launcher_wheel.relative_to(self.project)
        self.assertEqual(access(wheel), (0o640, state.stat().st_gid))
        self.assertEqual(access(state / setup.INSTALL_CACHE), (0o750, state.stat().st_gid))
        # The portable settings keep the system key path, and the key follows its own policy.
        settings = config.load(system)
        self.assertEqual((settings.port, settings.terminal_server_secret_file), (8818, key))
        self.assertEqual(key.read_bytes(), KEY)
        self.assertEqual(access(key), (0o640, os.getgid()))
        self.assertFalse((state / config.TERMINAL_SECRET).exists())
        self.assertIn("Next: ", self.output.getvalue())
        self.assertIn("--system run offline", self.output.getvalue())

    def test_a_restored_settings_file_never_replaces_the_terminal_key(self):
        self.settings.unlink()
        self.assertEqual(self.invoke("backup", "config"), 0, self.progress.getvalue())
        key = self.destination / "config/models/blue.model"
        key.parent.mkdir(parents=True)
        key.write_bytes(KEY)
        (self.destination / "config/config.ini").write_text(
            "[terminal_server]\nsecret_file = config/models/blue.model\n", encoding="utf-8")
        self.assertEqual(self.invoke("restore", "--archive", str(self.bundle), custom=False, project=self.destination),
                         1)
        self.assertIn("The backup uses the terminal key path for a configuration file.", self.progress.getvalue())
        self.assertEqual(key.read_bytes(), KEY)

    def test_setup_in_a_versioned_backup_folder_names_the_archive_to_restore(self):
        self.assertEqual(self.invoke("backup", "config", "--namespace", "v2"), 0, self.progress.getvalue())
        self.bundle, = self.backup_dir.iterdir()
        folder, _ = self.copied_bundle("versioned backup")
        moved = folder / "backup" / self.bundle.name
        with (
            patch.object(Path, "cwd", return_value=folder),
            patch.object(setup, "bundled_release", return_value=folder / "runtime"),
            patch.object(setup, "prepare_configuration"),
            patch("loaf.cli.assets.system_deployment_installed", return_value=False),
        ):
            self.assertEqual(self.invoke("setup", custom=False, project=None), 0, self.progress.getvalue())
            # The default settings select unversioned backups, so a plain restore would not find this one.
            self.assertIn(f"Next: ./loaf restore --archive {shlex.quote(str(moved))}\n", self.output.getvalue())
            self.assertEqual(self.invoke("restore", "--archive", str(moved), custom=False, project=None), 0,
                             self.progress.getvalue())
        self.assertIn("Next: ./loaf run offline\n", self.output.getvalue())
        self.assertEqual(config.load(folder / "config").namespace_name, "v2")

    def test_restore_inside_a_backup_folder_restores_into_it_or_changes_nothing(self):
        self.assertEqual(self.invoke("backup", "project"), 0, self.progress.getvalue())
        blocked, before = self.copied_bundle("blocked backup")
        (blocked / "backup" / self.bundle.name).mkdir(parents=True)
        with patch.object(Path, "cwd", return_value=blocked):
            self.assertEqual(self.invoke("restore", custom=False, project=None), 1)
        self.assertIn("which already exists", self.progress.getvalue())
        self.assertEqual({path.name for path in blocked.iterdir()} - {".run.lock"}, {*before, "backup"})
        self.assertEqual({path.name: path.read_bytes() for path in blocked.iterdir() if path.is_file()
                          and path.name != ".run.lock"}, before)

        if os.geteuid():
            # Read-only transfer media cannot become the project.
            media, before = self.copied_bundle("read-only backup")
            media.chmod(0o555)
            self.addCleanup(media.chmod, 0o755)
            self.assertEqual(self.invoke("restore", custom=False, project=media), 1)
            self.assertIn("Copy the folder to a writable location, or run its loaf from the project directory",
                          self.progress.getvalue())
            self.assertEqual({path.name: path.read_bytes() for path in media.iterdir()}, before)

        folder, before = self.copied_bundle("restored backup")
        moved = folder / "backup" / self.bundle.name
        with patch("loaf.cli.assets.system_deployment_installed", return_value=True):
            self.assertEqual(self.invoke("restore", "--dry-run", custom=False, project=folder), 0)
            self.assertIn(f"--system restore --archive {shlex.quote(str(folder))}", self.output.getvalue())
            self.assertEqual(self.invoke("restore", custom=False, project=folder), 0, self.progress.getvalue())
        output = self.output.getvalue()
        # Run from elsewhere, the follow-up names the project; the system hint names the moved backup.
        self.assertIn(f"Next: {shlex.join(cli.project_command(folder, None, None))} run offline\n", output)
        self.assertIn("The loaf Debian package is installed. To restore into its system deployment instead, run:\n"
                      f"  sudo {shlex.join(cli.project_command(folder, None, None, system=True))} restore "
                      f"--archive {shlex.quote(str(moved))}\n", output)
        self.assertEqual({path.name: path.read_bytes() for path in moved.iterdir()}, before)
        self.assertEqual(config.load(folder / "config").port, 8818)
        self.assertEqual((folder / self.toolkit.relative_to(self.project)).read_bytes(), b"fixture deb")
        self.assertIn(f"Restored config, installers from {folder / 'backup' / self.bundle.name / self.archive.name}",
                      self.output.getvalue())

    def test_source_restore_replaces_archived_files_and_keeps_other_files(self):
        self.assertEqual(self.invoke("backup", "source"), 0, self.progress.getvalue())
        self.destination.mkdir()
        (self.destination / "compose.yml").write_text("old\n", encoding="utf-8")
        (self.destination / "notes.txt").write_text("local notes\n", encoding="utf-8")
        (self.destination / "docs").mkdir()
        (self.destination / "docs/guide.md").write_text("old guide\n", encoding="utf-8")
        (self.destination / "docs/guide-link.md").symlink_to("notes.txt")
        restore = ("restore", "--archive", str(self.bundle))
        self.assertEqual(self.invoke(*restore, custom=False, project=self.destination), 0, self.progress.getvalue())
        self.assertEqual((self.destination / "compose.yml").read_bytes(), b"name: edited\nservices: {}\n")
        self.assertEqual((self.destination / "docs/guide.md").read_bytes(), b"Source documentation\n")
        self.assertEqual(os.readlink(self.destination / "docs/guide-link.md"), "guide.md")
        self.assertEqual((self.destination / "notes.txt").read_text(encoding="utf-8"), "local notes\n")
        self.assertEqual((self.destination / ".git/HEAD").read_bytes(), (self.project / ".git/HEAD").read_bytes())
        self.assertFalse(list(self.destination.glob(".loaf-restore-*")))

        # The checkout's guides replace those that standalone setup exported as a whole.
        shutil.rmtree(self.destination / "docs")
        (self.destination / "docs").mkdir()
        for name in (docs.STAMP, "README.md", "loaf.1", "guide.md"):
            (self.destination / "docs" / name).write_text("exported\n", encoding="utf-8")
        self.assertEqual(self.invoke(*restore, custom=False, project=self.destination), 0, self.progress.getvalue())
        self.assertEqual(sorted(path.name for path in (self.destination / "docs").iterdir()), ["guide-link.md", "guide.md"])
        self.assertEqual((self.destination / "docs/guide.md").read_bytes(), b"Source documentation\n")

        # A file where the checkout has a directory stops the restore before any change.
        (self.destination / "compose.yml").write_text("old\n", encoding="utf-8")
        shutil.rmtree(self.destination / "docs")
        (self.destination / "docs").write_text("a file\n", encoding="utf-8")
        self.assertEqual(self.invoke(*restore, custom=False, project=self.destination), 1)
        self.assertIn("over a file or link", self.progress.getvalue())
        self.assertEqual((self.destination / "compose.yml").read_text(encoding="utf-8"), "old\n")

    def test_failed_source_publish_puts_back_moved_and_replaced_files(self):
        checkout, project = self.root / "checkout", self.root / "checkout project"
        (checkout / "docs").mkdir(parents=True)
        (project / "docs").mkdir(parents=True)
        (checkout / "compose.yml").write_text("new\n", encoding="utf-8")
        (checkout / "docs/guide.md").write_text("new guide\n", encoding="utf-8")
        (project / "compose.yml").write_text("old\n", encoding="utf-8")
        entries = transfer.checkout_targets(checkout, project)
        self.assertEqual(sorted(target for _, target in entries), [project / "compose.yml", project / "docs/guide.md"])
        with self.assertRaises(FileNotFoundError):
            transfer.publish_checkout([*entries, (checkout / "missing", project / "missing")], self.root / "replaced")
        self.assertEqual((project / "compose.yml").read_text(encoding="utf-8"), "old\n")
        self.assertFalse((project / "docs/guide.md").exists())
        self.assertEqual((checkout / "compose.yml").read_text(encoding="utf-8"), "new\n")
        self.assertEqual((checkout / "docs/guide.md").read_text(encoding="utf-8"), "new guide\n")

    def test_setup_reuses_the_archived_terminal_key(self):
        self.add_terminal_volume()
        self.assertEqual(self.invoke("backup", "terminal"), 0, self.progress.getvalue())
        with patch.object(Path, "cwd", return_value=self.bundle):
            self.assertEqual(self.invoke("setup", "terminal-server-key", custom=False, project=self.destination),
                             0, self.progress.getvalue())
        self.assertEqual((self.destination / config.TERMINAL_SECRET).read_bytes(), KEY)

    def test_verify_lists_types_and_dry_runs_change_nothing(self):
        self.assertEqual(self.invoke("backup", "--dry-run"), 0)
        self.assertIn("Save source, config, installers, images, models, webui to", self.output.getvalue())
        self.assertFalse(self.backup_dir.exists())
        self.assertEqual(self.invoke("backup", "project", "images"), 0, self.progress.getvalue())
        created = self.output.getvalue()
        self.docker_calls.clear()
        self.assertEqual(self.invoke("backup", "--verify", "--archive", str(self.bundle), custom=False), 0)
        output = self.output.getvalue()
        for line in ("Archive format 3", "config", "installers", "images", "loaf/open-webui:latest  linux/amd64"):
            self.assertIn(line, output)
        # Backup and verify print the hashes of the archive and the launcher to compare on another channel.
        hashes = [f"{setup.file_digest(path)}  {path}" for path in (self.archive, self.bundle / "loaf")]
        for line in hashes:
            self.assertIn(f"\n{line}\n", created)
            self.assertIn(f"\n{line}\n", output)
        self.assertIn("Record them apart from the transfer media", created)
        self.assertIn("Compare them with the values recorded when the backup was made", output)
        self.assertEqual(self.invoke("restore", "--dry-run", "--archive", str(self.bundle), custom=False,
                                     project=self.destination), 0)
        self.assertIn(f"Restore config, installers, images from {self.archive}", self.output.getvalue())
        self.assertEqual(self.docker_calls, [])
        self.assertFalse(self.destination.exists())

    def test_models_restore_merges_or_mirrors_and_can_replay_modelfiles(self):
        self.assertEqual(self.invoke("backup", "models"), 0, self.progress.getvalue())
        stop = cli.Compose(self.project).command(cli.STOP_DOWNLOADER)
        snapshot = next(index for index, call in enumerate(self.docker_calls) if transfer.SNAPSHOT_MODELS in call)
        self.assertLess(self.docker_calls.index(stop), snapshot)
        self.assertFalse(any("stop" in call and "ollama-offline" in call for call in self.docker_calls))
        self.docker_calls.clear()
        self.assertEqual(self.invoke("restore", "--delete", "--import", project=self.destination), 0,
                         self.progress.getvalue())
        importer, = [call for call in self.docker_calls if transfer.IMPORT_MODELS in call]
        self.assertEqual(importer[-2:], ["loaf-model-import", "--delete-delay"])
        started = self.docker_calls[self.docker_calls.index(importer) + 1:]
        self.assertTrue(any("up" in call and call[-1] == "ollama-offline" for call in started))
        self.assertIn("Open WebUI is stopped.", self.output.getvalue())
        self.assertTrue(self.output.getvalue().rstrip().endswith(" offline"), self.output.getvalue())
        # A CPU replay suggests CPU serving next.
        self.assertEqual(self.invoke("restore", "models", "--import", "--cpu", project=self.destination), 0,
                         self.progress.getvalue())
        self.assertTrue(self.output.getvalue().rstrip().endswith(" offline --cpu"), self.output.getvalue())

    def test_backup_warns_about_modelfiles_and_keeps_the_snapshot(self):
        blob = "models/blobs/sha256-fixture"
        recipe = b"FROM /ollama/" + blob.encode() + b"\nPARAMETER temperature 0.2\n"
        self.models = tar_payload("models", None, **{"models/blobs": None, blob: b"weights",
                                                     "config/current:latest.model": recipe,
                                                     "config/stale:latest.model": recipe,
                                                     "config/missing:latest.model": recipe.replace(b"fixture", b"missing")})
        self.definitions = tar_payload("current:latest.model", b"# Generated\n" + recipe,
                                       **{"stale:latest.model": recipe.replace(b"0.2", b"0.8"),
                                          "missing:latest.model": recipe.replace(b"fixture", b"missing")})
        self.assertEqual(self.invoke("backup", "models"), 0, self.progress.getvalue())
        warnings = [line for line in self.progress.getvalue().splitlines() if line.startswith("Warning:")]
        self.assertCountEqual(warnings, [
            "Warning: config/stale:latest.model: differs from the current Ollama definition.",
            "Warning: config/missing:latest.model: missing model blob sha256-missing.",
        ])
        self.assertEqual(read_members(self.archive)["models.tar"], self.models)

    def test_failed_backups_keep_the_previous_backup_and_clean_staging(self):
        self.assertEqual(self.invoke("backup", "project", "webui"), 0, self.progress.getvalue())
        previous = self.archive.read_bytes()
        self.failing = {"save webui"}
        self.assertEqual(self.invoke("backup", "project", "webui"), 23)
        self.fail_wheel_export, self.failing = True, set()
        self.docker_calls.clear()
        self.assertEqual(self.invoke("backup"), 1)
        self.assertIn("Cannot export Python wheels", self.progress.getvalue())
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(list(self.backup_dir.iterdir()), [self.bundle])
        self.assertEqual(self.archive.read_bytes(), previous)

    def test_backups_are_private_unless_the_directory_shares_them_with_its_group(self):
        def mode(path):
            return stat.S_IMODE(path.stat().st_mode) & 0o777

        for shared in (False, True):
            with self.subTest(shared=shared):
                shutil.rmtree(self.backup_dir, ignore_errors=True)
                self.backup_dir.mkdir()
                self.backup_dir.chmod(0o2770 if shared else 0o700)
                self.assertEqual(self.invoke("backup", "project"), 0, self.progress.getvalue())
                private, runnable = (0o640, 0o750) if shared else (0o600, 0o700)
                self.assertEqual((mode(self.bundle), mode(self.bundle / "loaf")), (runnable, runnable))
                self.assertEqual(mode(self.archive), private)

    def test_backup_excludes_backup_folders_inside_the_checkout(self):
        self.backup_dir = self.project / "transfer disk"
        self.bundle = self.backup_dir / self.bundle.name
        self.archive = self.bundle / self.archive.name
        self.assertEqual(self.invoke("backup", "source"), 0, self.progress.getvalue())
        self.assertEqual(self.invoke("backup", "source"), 0, self.progress.getvalue())
        with tarfile.open(fileobj=io.BytesIO(read_members(self.bundle.with_name(self.bundle.name + "-2") /
                                                          f"{self.bundle.name}-2.tar.gz")["source.tar"])) as source:
            self.assertFalse([name for name in source.getnames() if name.startswith("transfer disk")])

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


class ModelTransferTests(unittest.TestCase):
    def test_transfer_accepts_selected_live_backend_and_rejects_other_writers(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            with patch.object(transfer.subprocess, "check_output", side_effect=[
                b"[]", b"loaf-v2/ollama-connected\n",
            ]):
                command = transfer.model_volume_command(project, "loaf-v2")
            self.assertIn("type=volume,source=loaf-v2-ollama,target=/ollama,volume-nocopy,readonly", command)
            self.assertIn(transfer.SNAPSHOT_MODELS, command)
            for consumer in (b"loaf-v2/ollama-online", b"foreign/ollama-connected", b"/"):
                with self.subTest(consumer=consumer), patch.object(
                    transfer.subprocess, "check_output", side_effect=[b"[]", consumer],
                ), self.assertRaisesRegex(transfer.TransferError, "in use"):
                    transfer.model_volume_command(project, "loaf-v2")
            # A restore can create the volume, so it only checks the consumers.
            with patch.object(transfer.subprocess, "check_output", side_effect=[b""]):
                command = transfer.model_volume_command(project, "", restore=True, delete=True)
            self.assertEqual(command[-2:], ["loaf-model-import", "--delete-delay"])

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

            target = root / "destination"
            (target / blob).parent.mkdir(parents=True)
            (target / blob).write_bytes(corrupt)
            script = transfer.IMPORT_MODELS.replace("/ollama", str(target))
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

            archive = project / "models.tar"
            snapshot = transfer.SNAPSHOT_MODELS.replace("/ollama", str(source))
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

            import_script = transfer.IMPORT_MODELS.replace("/ollama", str(target))
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
            # The import unpacks inside the store and moves blobs into place, without a second copy elsewhere.
            self.assertEqual(list(target.glob(".loaf-transfer.*")), [])
            self.assertNotIn("/staging", transfer.IMPORT_MODELS + transfer.SNAPSHOT_MODELS)

            # A store without models/ still yields the directory that restore expects.
            empty = project / "empty"
            empty.mkdir()
            script = transfer.SNAPSHOT_MODELS.replace("/ollama", str(empty)).replace("/tmp/empty", str(project / "e"))
            with archive.open("wb") as payload:
                subprocess.run(["sh", "-c", script], stdout=payload, check=True)
            transfer.validate_model_archive(archive)


def write_members(path, *members):
    """Write (name, kind, value) entries: kind is dir, file, symlink, hardlink or device."""
    with tarfile.open(path, "w") as archive:
        for name, kind, value in members:
            member = tarfile.TarInfo(name)
            data = None
            if kind == "dir":
                member.type = tarfile.DIRTYPE
            elif kind == "file":
                member.size = len(value)
                data = io.BytesIO(value)
            elif kind == "symlink":
                member.type, member.linkname = tarfile.SYMTYPE, value
            elif kind == "hardlink":
                member.type, member.linkname = tarfile.LNKTYPE, value
            else:
                member.type = tarfile.CHRTYPE
            archive.addfile(member, data)


class ArchiveValidationTests(unittest.TestCase):
    """Restore refuses unsafe archive contents before Docker or the project changes."""

    def setUp(self):
        folder = TemporaryDirectory(prefix="loaf-archive-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "payload.tar"

    def test_volume_payload_accepts_links_inside_one_volume(self):
        write_members(
            self.path, ("webui", "dir", None), ("webui/models", "file", b"data"),
            ("webui/copy", "hardlink", "webui/models"), ("webui/latest", "symlink", "/elsewhere"),
            (ACCOUNT, "dir", None),
        )
        transfer.validate_volume_payload(self.path, {"webui", ACCOUNT})

    def test_volume_payload_rejects_paths_types_and_links_outside_the_volumes(self):
        base = (("webui", "dir", None), (ACCOUNT, "dir", None))
        volumes = {"webui", ACCOUNT}
        for label, members, message in (
            ("unknown volume", [("other", "dir", None)], "Unexpected path"),
            ("parent segment", [("webui/../x", "file", b"")], "Unexpected path"),
            ("duplicate entry", [("webui/a", "file", b""), ("webui/a", "file", b"")], "Unexpected path"),
            ("device file", [("webui/console", "device", None)], "Unexpected file type"),
            ("hard link to another volume", [("webui/key", "hardlink", f"{ACCOUNT}/data")], "Invalid hard link"),
            ("hard link with parent segment", [("webui/key", "hardlink", "webui/../x")], "Invalid hard link"),
            ("link traversal", [("webui/link", "symlink", "/"), ("webui/link/file", "file", b"")], "traverses"),
        ):
            with self.subTest(label):
                write_members(self.path, *base, *members)
                with self.assertRaisesRegex(transfer.TransferError, message):
                    transfer.validate_volume_payload(self.path, volumes)
        write_members(self.path, ("webui", "file", b""), (ACCOUNT, "dir", None))
        with self.assertRaisesRegex(transfer.TransferError, "Unexpected file type"):
            transfer.validate_volume_payload(self.path, volumes)
        write_members(self.path, ("webui", "dir", None))
        with self.assertRaisesRegex(transfer.TransferError, "missing data"):
            transfer.validate_volume_payload(self.path, volumes)

    def test_archive_members_must_match_the_manifest(self):
        settings = io.BytesIO()
        with tarfile.open(fileobj=settings, mode="w") as config_tar:
            entry = tarfile.TarInfo("config/config.ini")
            entry.size = len(b"[webui]\n")
            config_tar.addfile(entry, io.BytesIO(b"[webui]\n"))
        payload = settings.getvalue()
        member = {"path": "config.tar", "size": len(payload), "sha256": sha256(payload).hexdigest()}
        manifest = {"format": transfer.ARCHIVE_FORMAT, "version": transfer.ARCHIVE_VERSION, "deployment": "",
                    "items": [{"type": "config", "members": [member]}]}
        encoded = json.dumps(manifest).encode()
        archive = self.root / "archive.tar.gz"
        for label, members, message in (
            ("unknown member", [(transfer.MANIFEST, encoded), ("config.tar", payload), ("notes.tar", b"")],
             "Unexpected member"),
            ("duplicate member", [(transfer.MANIFEST, encoded), ("config.tar", payload), ("config.tar", payload)],
             "Unexpected member"),
            ("missing member", [(transfer.MANIFEST, encoded)], "is missing config.tar"),
            ("changed member", [(transfer.MANIFEST, encoded), ("config.tar", payload[:-1] + b"!")], "verification failed"),
        ):
            with self.subTest(label):
                write_archive(archive, members)
                opened = transfer.open_archive(archive)
                with self.assertRaisesRegex(transfer.TransferError, message), redirect_stderr(io.StringIO()):
                    transfer.unpack(opened, self.root, set())
        for label, change, message in (
            ("not loaf", {"format": "other"}, "not a loaf archive"),
            ("older format", {"version": 1}, "uses archive format 1"),
            ("member outside its type", {"items": [{"type": "config", "members": [{**member, "path": "images.tar"}]}]},
             "unexpected member"),
            ("negative size", {"items": [{"type": "config", "members": [{**member, "size": -1}]}]}, "unexpected member"),
            ("invalid deployment", {"deployment": "../x"}, "Invalid loaf.json"),
        ):
            with self.subTest(label):
                write_archive(archive, [(transfer.MANIFEST, json.dumps({**manifest, **change}).encode())])
                with self.assertRaisesRegex(transfer.TransferError, message):
                    transfer.open_archive(archive)

    def test_terminal_volume_name_and_labels_follow_the_deployment(self):
        self.assertEqual(transfer.terminal_volume("loaf-v2"), "loaf-v2-terminal-users")
        self.assertEqual(transfer.terminal_volume_labels("loaf"),
                         {"loaf.terminal.project": "loaf", "loaf.terminal.users": "true"})

    def model_members(self, manifest_value, blob=b"weights", *, size=None, digest=None):
        digest = digest or f"sha256:{sha256(blob).hexdigest()}"
        manifest = manifest_value if isinstance(manifest_value, bytes) else json.dumps(manifest_value or {
            "config": {"digest": digest, "size": len(blob) if size is None else size}, "layers": [],
        }).encode()
        return [
            ("models", "dir", None), ("models/blobs", "dir", None),
            (f"models/blobs/{digest.replace(':', '-')}", "file", blob),
            ("models/manifests/registry.ollama.ai/library/example/latest", "file", manifest),
        ]

    def test_plain_model_manifest_requires_complete_blobs(self):
        write_members(self.path, *self.model_members(None), ("config", "dir", None),
                      ("config/example.model", "file", b"FROM /ollama/models/blobs/x\n"))
        transfer.validate_model_archive(self.path)

        missing = self.model_members(None)
        del missing[2]
        for label, members, message in (
            ("missing blob", missing, "Missing or incomplete blob"),
            ("size mismatch", self.model_members(None, size=1), "Missing or incomplete blob"),
            ("invalid digest", self.model_members(None, digest="md5:abc"), "Invalid blob digest"),
            ("oversized manifest", self.model_members(b" " * (transfer.COPY_BUFFER + 1)), "exceeds 1 MiB"),
            ("manifest type", self.model_members(b"[]"), "Expected a model manifest object"),
            ("missing models", [("config", "dir", None)], "missing the models directory"),
            ("top-level file", [("models", "file", b"")], "Expected a directory"),
            ("link outside v2 tags", [("models", "dir", None), ("models/x", "symlink", "/etc")],
             "Unexpected model archive member"),
            ("file below a file", [("models", "dir", None), ("models/a", "file", b""), ("models/a/b", "file", b"")],
             "non-directory parent"),
        ):
            with self.subTest(label):
                write_members(self.path, *members)
                with self.assertRaisesRegex(transfer.TransferError, message):
                    transfer.validate_model_archive(self.path)

    @unittest.skipUnless(hasattr(tarfile, "data_filter"), "Requires Python tar extraction filters.")
    def test_checkout_refuses_reserved_absolute_and_duplicate_paths(self):
        for name in ("/etc/passwd", "docs/../../outside", "dist/loaf", "backup/x", ".loaf-restore-x/y",
                     "loaf", "docs"):
            with self.subTest(name):
                members = [("docs", "dir", None), (name, "dir" if name == "docs" else "file", None if name == "docs" else b"")]
                write_members(self.path, *members)
                checkout = self.root / "checkout"
                shutil.rmtree(checkout, ignore_errors=True)
                with self.assertRaisesRegex(transfer.TransferError, "Invalid checkout path"):
                    transfer.stage_checkout(self.path, checkout)


class ProgressTests(unittest.TestCase):
    def test_readiness_wait_reports_elapsed_time_only_outside_a_terminal(self):
        for terminal, expected in ((False, True), (True, False)):
            with self.subTest(terminal=terminal), redirect_stderr(io.StringIO()) as errors, \
                    patch.object(sys.stderr, "isatty", return_value=terminal, create=True):
                with wait_status("Waiting for services", interval=0.01):
                    sleep(0.1)
            # Compose redraws its terminal display; an extra line there would duplicate it.
            self.assertEqual("Waiting for services (0s elapsed)." in errors.getvalue(), expected, errors.getvalue())

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
            webui = root / "webui"
            workspace = root / "workspace"
            webui.mkdir()
            workspace.mkdir()
            existing = webui / "account"
            existing.write_bytes(b"existing")
            script = transfer.RESTORE_VOLUMES.replace("/transfer", '"$TRANSFER_ROOT"')
            options = dict(env={**os.environ, "TRANSFER_ROOT": str(root)}, capture_output=True)
            arguments = ["sh", "-c", script, "restore", str(webui), str(workspace)]
            result = subprocess.run(arguments, input=b"", **options)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"requires empty volumes", result.stderr)
            self.assertEqual(existing.read_bytes(), b"existing")

            existing.unlink()
            # A tar header followed by truncated content forces a partial file
            # to be extracted before tar exits with an error.
            data = b"x" * 1024
            payload = tar_payload("webui/model", data)
            result = subprocess.run(arguments, input=payload[:600], **options)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((list(webui.iterdir()), list(workspace.iterdir())), ([], []))
            result = subprocess.run(arguments, input=payload, **options)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((webui / "model").read_bytes(), data)
