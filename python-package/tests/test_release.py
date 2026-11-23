"""Exercise release tagging with temporary Git repositories."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import ROOT


@unittest.skipUnless(sys.version_info >= (3, 11), "The release helper requires Python 3.11 or newer.")
class ReleaseTests(unittest.TestCase):
    tag = "v1.2.3"
    ref = f"refs/tags/{tag}"

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("Git is required for release tests.")
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.project = self.root / "project"
        (self.project / "scripts").mkdir(parents=True)
        self.script = self.project / "scripts/release.py"
        shutil.copyfile(ROOT / "scripts/release.py", self.script)
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
        self.metadata = self.project / "python-package/pyproject.toml"
        self.metadata.parent.mkdir()
        self.write_version("1.2.3")
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "release@example.invalid")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "Prepare release")

    def write_version(self, version):
        self.metadata.write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.project, text=True, env=self.env).strip()

    def release(self, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(self.script), *args], cwd=self.root, env=self.env, capture_output=True, text=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def remote(self, name, *, create=True):
        path = self.root / f"{name}.git"
        if create:
            self.git("init", "--quiet", "--bare", str(path))
        self.git("remote", "add", name, str(path))
        return path

    def test_dry_run_reports_commit_and_remotes_without_creating_refs(self):
        origin = self.remote("origin", create=False)
        mirror = self.remote("mirror", create=False)
        result = self.release("--dry-run", "origin", "mirror")
        self.assertIn(f"Would create annotated tag {self.tag} at {self.git('rev-parse', 'HEAD')[:12]}", result.stdout)
        self.assertIn(f"Would push {self.tag} to origin", result.stdout)
        self.assertIn(f"Would push {self.tag} to mirror", result.stdout)
        self.assertEqual(self.git("tag", "--list"), "")
        self.assertFalse(origin.exists())
        self.assertFalse(mirror.exists())

    def test_creates_local_tag_from_a_notes_file(self):
        origin = self.remote("origin")
        notes = "## Release summary\n\n- A change"
        (self.root / "notes.md").write_text(notes + "\n", encoding="utf-8")
        self.release("-F", "notes.md")
        self.assertEqual(self.git("cat-file", "-t", self.ref), "tag")
        self.assertEqual(self.git("rev-parse", f"{self.ref}^{{}}"), self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("for-each-ref", "--format=%(contents)", self.ref), notes)
        self.assertEqual(self.git("for-each-ref", cwd=origin), "")

    def test_retries_a_failed_push_with_the_same_tag_and_no_other_refs(self):
        origin = self.remote("origin")
        mirror = self.remote("mirror", create=False)
        self.git("config", "push.followTags", "true")
        self.git("tag", "-a", "other", "-m", "Unrelated tag")
        self.release("-m", "Original notes", "origin", "mirror", success=False)
        tag_object = self.git("rev-parse", self.ref)
        self.assertEqual(self.git("rev-parse", self.ref, cwd=origin), tag_object)

        self.git("init", "--quiet", "--bare", str(mirror))
        result = self.release("origin", "mirror")
        self.assertIn("with its original notes", result.stdout)
        for remote in (origin, mirror):
            self.assertEqual(self.git("for-each-ref", "--format=%(refname) %(objectname)", cwd=remote),
                             f"{self.ref} {tag_object}")

    def test_rejects_an_uncommitted_version_change(self):
        self.write_version("1.2.4")
        with self.subTest("Working tree"):
            result = self.release("-m", "New version", success=False)
            self.assertIn("Commit the version change", result.stderr)
            self.assertEqual(self.git("tag", "--list"), "")
        with self.subTest("Index"):
            self.git("add", "--", "python-package/pyproject.toml")
            self.write_version("1.2.3")
            result = self.release("-m", "New version", success=False)
            self.assertIn("Commit the version change", result.stderr)
            self.assertEqual(self.git("tag", "--list"), "")

    def test_validates_all_remote_names_before_tagging(self):
        origin = self.remote("origin")
        self.release("-m", "New version", "origin", "missing", success=False)
        self.assertEqual(self.git("tag", "--list"), "")
        self.assertEqual(self.git("for-each-ref", cwd=origin), "")

    def test_refuses_to_replace_a_tag_for_another_commit(self):
        self.git("tag", "-a", self.tag, "-m", "Earlier release")
        previous = self.git("rev-parse", self.ref)
        self.git("commit", "--quiet", "--allow-empty", "-m", "A newer commit")
        result = self.release("-m", "New release", success=False)
        self.assertIn("Use a new release version", result.stderr)
        self.assertEqual(self.git("rev-parse", self.ref), previous)

    def test_refuses_to_reuse_a_lightweight_tag(self):
        self.git("tag", self.tag)
        result = self.release("-m", "New release", success=False)
        self.assertIn("already exists as a lightweight tag", result.stderr)
        self.assertEqual(self.git("cat-file", "-t", self.ref), "commit")
