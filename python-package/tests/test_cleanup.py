"""Exercise cleanup with an isolated checkout and XDG data directory."""

from contextlib import redirect_stderr, redirect_stdout
import gzip
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from loaf import cli, locks, transfer
from tests import ROOT


class CleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="loaf clean ")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.project = base / "project"
        self.project.mkdir()
        self.home = base / "data"
        self.runtime = self.home / "loaf"
        data = self.runtime / "runtime/hash/python/lib/python3.13/site-packages/loaf/data"
        data.mkdir(parents=True)
        self.put(data / "compose.yml")
        link = self.project / "docker-project"
        link.symlink_to(data, target_is_directory=True)
        env = patch.dict(os.environ, XDG_DATA_HOME=str(self.home), LOAF_RUNTIME_LOCK_FD="")
        env.start()
        self.addCleanup(env.stop)
        shutil.copyfile(ROOT / ".gitignore", self.project / ".gitignore")
        self.put(self.project / "python-package/src/loaf/__init__.py")
        self.put(self.project / "config/config.ini", b"[webui]\nport = 8818\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "Initial checkout")

    def git(self, *arguments):
        return subprocess.check_output(["git", *arguments], cwd=self.project)

    def put(self, path, data=b"fixture"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def invoke(self, *arguments):
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            result = cli.main(["--project-dir", str(self.project), "clean", *arguments])
        return result, stdout.getvalue() + stderr.getvalue()

    def test_clean_removes_runtime_and_temporaries(self):
        staged = self.put(self.project / "backup/.loaf-backup-dead/users.tar")
        bytecode = self.put(self.project / "python-package/src/loaf/__pycache__/cli.pyc")
        download = self.put(self.project / "install-cache/wheels/.loaf-wheels-dead/package.whl")
        self.put(self.runtime / ".build-interrupted/python.tar.gz")
        self.put(self.runtime / "runtime/resource.dat.gz", gzip.compress(b"data, not an archive"))
        editable = self.put(self.project / ".venv/pyvenv.cfg")
        cache = self.put(self.project / "install-cache/wheels/setuptools.whl")
        self.assertEqual(self.invoke("--dry-run")[0], 0)
        self.assertTrue(self.runtime.is_dir())
        self.assertTrue(staged.is_file())
        self.assertFalse((self.project / locks.PROJECT_LOCK).exists())
        self.assertEqual(self.invoke()[0], 0)
        for path in (self.runtime, staged, bytecode, download):
            self.assertFalse(path.exists(), path)
        self.assertTrue(editable.is_file())
        self.assertTrue(cache.is_file())
        self.assertTrue((self.project / "docker-project").is_symlink())

    def test_full_keeps_git_config_credentials_and_backups(self):
        overrides = b"[webui]\nport = 8819\n[terminal_server]\nsecret_file = python-package/dist/private\n"
        self.put(self.project / "config/config.ini", overrides)
        key = self.put(self.project / "config/terminal-server.key")
        custom_key = self.put(self.project / "python-package/dist/private")
        source = self.put(self.project / "working-notes")
        self.put(self.project / ".venv/pyvenv.cfg")
        self.put(self.project / "install-cache/wheels/setuptools.whl")
        self.put(self.project / "install-cache/deb/docker/example.deb")
        self.put(self.project / "python-package/build/launcher")
        backup = self.put(self.project / "backup/saved/backup.json")
        dist_backup = self.put(self.project / "dist/another-models.data")
        with tarfile.open(dist_backup, "w:gz") as payload:
            entry = tarfile.TarInfo("models")
            entry.type = tarfile.DIRTYPE
            payload.addfile(entry)
        custom_archive = dist_backup.read_bytes()
        temp_backup = self.put(self.project / ".loaf-backup-kept.tar.gz")
        tracked = self.put(self.project / "python-package/src/local_ai_ferry.egg-info/note")
        self.git("add", "-f", str(tracked.relative_to(self.project)))
        before = self.git("status", "--porcelain")
        self.assertEqual(self.invoke("--full", "--force")[0], 0)
        for path in (key, custom_key, source, backup, temp_backup, tracked):
            self.assertEqual(path.read_bytes(), b"fixture")
        self.assertEqual(dist_backup.read_bytes(), custom_archive)
        self.assertEqual((self.project / "config/config.ini").read_bytes(), overrides)
        self.assertEqual(self.git("status", "--porcelain"), before)
        for path in (".venv", "install-cache", "python-package/build", "docker-project"):
            self.assertFalse(os.path.lexists(self.project / path), path)

    def test_full_asks_before_removing_installers_an_offline_host_cannot_download(self):
        cached = self.put(self.project / "install-cache/wheels/setuptools.whl")
        self.put(self.project / ".venv/pyvenv.cfg")
        # Without a terminal and without --force, the installers stay.
        with patch("sys.stdin.isatty", return_value=False):
            status, output = self.invoke("--full")
        self.assertEqual(status, 0, output)
        self.assertIn("Add --force to remove it", output)
        self.assertTrue(cached.is_file())
        self.assertFalse((self.project / ".venv").exists())
        status, output = self.invoke("--full", "--dry-run")
        self.assertIn("Would ask before removing", output)
        for answer, kept in (("n", True), ("y", False)):
            with self.subTest(answer=answer), patch("sys.stdin.isatty", return_value=True), \
                    patch("builtins.input", return_value=answer) as asked:
                self.assertEqual(self.invoke("--full")[0], 0)
            self.assertIn("cannot download its installers again", asked.call_args.args[0])
            self.assertEqual(cached.is_file(), kept)

    def test_full_removes_the_cached_runtime_downloads_with_the_installers(self):
        # The executable build's checked downloads are archives, but no backups.
        runtime = self.put(self.project / "install-cache/python/cpython-3.13.18-install_only_stripped.tar.gz")
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="y"):
            status, output = self.invoke("--full")
        self.assertEqual(status, 0, output)
        self.assertNotIn("Keep", output)
        self.assertFalse(runtime.exists())
        # Another archive in the cache still protects it.
        backup = self.put(self.project / "install-cache/deb/saved_local-ai-ferry.tar.gz")
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="y"):
            status, output = self.invoke("--full")
        self.assertIn("Keep", output)
        self.assertTrue(backup.exists())

    def test_full_keeps_unignored_files_in_build_directory(self):
        self.put(self.project / "python-package/.gitignore", b"!/build/\n!/build/notes.txt\n")
        notes = self.put(self.project / "python-package/build/notes.txt")
        status, output = self.invoke("--full")
        self.assertEqual(status, 0, output)
        self.assertIn(f"Keep {notes.parent}", output)
        self.assertEqual(notes.read_bytes(), b"fixture")

    def test_full_refuses_to_remove_caches_with_invalid_configuration(self):
        key = self.put(self.project / "install-cache/private", b"k" * 32)
        self.put(self.project / "config/config.ini",
                 b"[terminal_server]\nsecret_file = install-cache/private\n[webui]\nport = invalid\n")
        for options in (("--dry-run",), ()):
            with self.subTest(options=options):
                status, output = self.invoke("--full", *options)
                self.assertEqual(status, 1, output)
                self.assertIn("[webui] port", output)
                self.assertNotIn("Would remove", output)
                self.assertEqual(key.read_bytes(), b"k" * 32)
                self.assertTrue(self.runtime.is_dir())

    def test_busy_project_or_runtime_prevents_cleanup(self):
        candidate = self.put(self.project / ".loaf-restore-active/images.tar")
        for lock in (
            locks.project_lock(self.project / locks.PROJECT_LOCK),
            locks.runtime_cleanup_lock(self.home),
        ):
            with lock:
                status, output = self.invoke()
            self.assertEqual(status, 1)
            self.assertIn("Wait for it to finish.", output)
            self.assertTrue(candidate.is_file())
            self.assertTrue(self.runtime.is_dir())

    def test_cleanup_never_follows_runtime_symlinks(self):
        shutil.rmtree(self.runtime)
        other = self.put(self.home / "user-data/important")
        self.runtime.symlink_to(other.parent, target_is_directory=True)
        status, output = self.invoke("--full")
        self.assertEqual(status, 1)
        self.assertIn("local loaf runtime cache", output)
        self.assertTrue(other.is_file())

    def test_full_does_not_remove_paths_under_a_symlinked_parent(self):
        package = self.project / "python-package"
        destination = self.home / "alternate-source"
        package.rename(destination)
        package.symlink_to(destination, target_is_directory=True)
        build = self.put(destination / "build/keep")
        self.assertEqual(self.invoke("--full")[0], 0)
        self.assertTrue(build.is_file())

    def test_standalone_ignores_an_empty_git_placeholder_in_an_ancestor(self):
        shutil.rmtree(self.project / ".git")
        (self.project.parent / ".git").mkdir()
        self.assertEqual(self.invoke("--full")[0], 0)
        self.assertTrue((self.project.parent / ".git").is_dir())
        self.assertFalse(self.runtime.exists())

    def test_project_lock_rejects_links_without_writing_targets(self):
        target = self.put(self.home / "elsewhere", b"")
        lock = self.project / locks.PROJECT_LOCK
        for make_link in (lambda: lock.symlink_to(target), lambda: os.link(target, lock)):
            make_link()
            with self.assertRaisesRegex(OSError, "Lock file must be a regular file"), locks.project_lock(lock):
                self.fail("Lock followed a link")
            self.assertEqual(target.read_bytes(), b"")
            lock.unlink()

    def test_bundle_layout_cannot_be_cleaned_as_a_cache(self):
        bundle = self.runtime / f"20261123-1500_{transfer.BUNDLE_NAME}"
        archive = self.put(bundle / "custom.tar.gz")
        self.put(bundle / transfer.CHECKSUMS)
        self.put(bundle / transfer.LAUNCHER)
        status, output = self.invoke("--full")
        self.assertEqual(status, 0)
        self.assertIn("protected Git work, backup, credentials or mount", output)
        self.assertTrue(archive.is_file())
