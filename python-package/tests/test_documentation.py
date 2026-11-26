"""Check local links and parse shell examples without executing them."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

from tests import ROOT


def guides():
    return sorted((ROOT / "docs").rglob("*.md"))


def documents():
    return [ROOT / name for name in ("README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md")] + guides()


def markdown_parts(text):
    prose, blocks = [], []
    fence = ""
    for line in text.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)", line)
        if not fence:
            if match:
                fence, info = match.groups()
                language = (info.strip().split() or [""])[0]
                code = []
            else:
                prose.append(line)
        elif match and match[1].startswith(fence) and not match[2].strip():
            blocks.append((language, "".join(code)))
            fence = ""
        else:
            code.append(line)
    if fence:
        raise ValueError(f"Unclosed Markdown fence {fence!r}")
    return prose, blocks


def heading_anchors(text):
    seen = {}
    anchors = set()
    prose, _ = markdown_parts(text)
    for line in prose:
        match = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*$", line)
        if not match:
            continue
        title = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", match[1])
        title = re.sub(r"<[^>]*>", "", title).lower()
        slug = re.sub(r"[^\w -]", "", title).replace(" ", "-")
        occurrence = seen.get(slug, 0)
        seen[slug] = occurrence + 1
        anchors.add(f"{slug}-{occurrence}" if occurrence else slug)
    return anchors


def markdown_links(path):
    prose, _ = markdown_parts(path.read_text(encoding="utf-8"))
    for target in re.findall(r"\]\(([^)]+)\)", "".join(prose)):
        yield urlsplit(target.strip("<>"))


def local_link_problems(path):
    problems = []
    for link in markdown_links(path):
        if link.scheme or link.netloc:
            continue
        target = path.parent / unquote(link.path) if link.path else path
        if not target.exists():
            problems.append(f"Missing target: {link.geturl()}")
        elif link.fragment and target.suffix.lower() == ".md":
            if unquote(link.fragment) not in heading_anchors(target.read_text(encoding="utf-8")):
                problems.append(f"Missing heading: {link.geturl()}")
    return problems


class DocumentationTests(unittest.TestCase):
    def test_fences_and_local_links(self):
        for path in documents():
            with self.subTest(path=path):
                self.assertEqual(local_link_problems(path), [])

    def test_command_reference_covers_every_command_and_global_flag(self):
        from loaf import cli

        reference = (ROOT / "docs/cli-help.md").read_text(encoding="utf-8")
        for command in cli.COMMAND_HELP:
            with self.subTest(command=command):
                self.assertIn(f"\n### {command}\n", reference)
        for flag in cli.GLOBAL_OPTIONS:
            with self.subTest(flag=flag):
                self.assertIn(f"`{flag}", reference)

    def test_root_readme_links_every_guide(self):
        targets = {unquote(link.path) for link in markdown_links(ROOT / "README.md") if not (link.scheme or link.netloc)}
        self.assertTrue(guides())
        for path in guides():
            self.assertIn(path.relative_to(ROOT).as_posix(), targets)

    def test_link_validator_finds_broken_files_and_fragments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "my guide.md").write_text("# Real title\n", encoding="utf-8")
            guide = root / "source.md"
            guide.write_text(
                "# Start\n"
                "[good](my%20guide.md#real-title)\n"
                "[local](#start)\n"
                "[bad](missing.md)\n"
                "[bad heading](my%20guide.md#absent)\n"
                "[external](https://example.invalid/#not-checked)\n",
                encoding="utf-8",
            )
            self.assertEqual(local_link_problems(guide), [
                "Missing target: missing.md", "Missing heading: my%20guide.md#absent",
            ])

    def test_fences_hide_headings_and_links_and_require_matching_closures(self):
        text = (
            "# Title\n## Use `num_ctx`\n"
            "````markdown\n```bash\n## Ignored\n```\n````\n"
            "~~~bash\necho example\n~~~\n"
            "## Use `num_ctx`\n"
        )
        self.assertEqual(heading_anchors(text), {"title", "use-num_ctx", "use-num_ctx-1"})
        self.assertEqual(markdown_parts(text)[1], [
            ("markdown", "```bash\n## Ignored\n```\n"), ("bash", "echo example\n"),
        ])
        with self.assertRaisesRegex(ValueError, "Unclosed Markdown fence"):
            markdown_parts("```bash\necho example\n~~~\n")

    def check_examples(self, language, args, *, env=None):
        for path in documents():
            _, blocks = markdown_parts(path.read_text(encoding="utf-8"))
            for index, (kind, source) in enumerate(blocks, start=1):
                if kind == language:
                    with self.subTest(path=path, block=index):
                        result = subprocess.run(
                            args, input=source, encoding="utf-8", errors="replace",
                            capture_output=True, timeout=15, env=env,
                        )
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bash_blocks_parse_without_executing(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("Bash not installed.")
        env = dict(os.environ, BASH_ENV="")
        self.check_examples("bash", [bash, "--noprofile", "--norc", "-n"], env=env)
