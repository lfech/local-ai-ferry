"""Publish test results and refresh the README badges for the groups that ran."""

from collections import Counter
from datetime import datetime
from itertools import count
import os
from pathlib import Path
import platform
import re
import shutil
from tempfile import TemporaryDirectory
import unittest

from .config import TEST_FLAGS


GROUPS = {
    # Label and minimum widths of the two SVG cells.
    "cli": ("CLI baseline", 77, 109),
    "docker": ("Docker", 72, 87),
    "open-terminal": ("Open Terminal", 91, 87),
    "gpu": ("GPU inference", 94, 83),
    "catalog": ("Model catalog", 90, 80),
}
DOCKER_CASES = {"ComposeContractTests", "BuiltImageTests", "ShellHelperTests", "DockerTests"}
TERMINAL_CASES = {"TerminalServerDockerTests", "ProvisionerTests", "RunnerTests"}
BADGE_LINK = re.compile(
    r"(?m)^\[!\[[^\]\n]*\]\(docs/badges/([a-z-]+)\.svg\)\]"
    r"\(docs/verification-record\.md(?:#[^)\n]+)?\)"
)


def test_group(test: unittest.TestCase) -> str:
    module = type(test).__module__.rsplit(".", 1)[-1]
    case = type(test).__name__
    if module == "test_docker" and case in DOCKER_CASES:
        return "docker"
    if module == "test_terminal_server" and case in TERMINAL_CASES:
        return "open-terminal"
    return {"test_docker_gpu": "gpu", "test_catalog": "catalog"}.get(module, "cli")


class BadgeResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = set()
        self.passed = set()

    def startTest(self, test):
        super().startTest(test)
        self.started.add(test)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.add(test)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is None:
            self.passed.add(test)

    def badge_counts(self) -> dict[str, tuple[int, int]]:
        # unittest reports subtest outcomes separately. Count each test
        # method once and include it if any of its subtests ran.
        failed = {
            getattr(test, "test_case", test)
            for test, _ in (*self.failures, *self.errors, *self.expectedFailures)
        } | set(self.unexpectedSuccesses)
        skipped = {test for test, _ in self.skipped}
        skipped_subtests = {getattr(test, "test_case", test) for test, _ in self.skipped}
        unattempted = skipped | (skipped_subtests - self.passed)
        ran = self.started - (unattempted - failed)
        totals = Counter(test_group(test) for test in ran)
        passes = Counter(test_group(test) for test in (ran & self.passed) - failed)
        return {group: (passes[group], totals[group]) for group in GROUPS if totals[group]}


def badge_svg(label: str, timestamp: str, text: str, left: int, right: int, *, passed: bool) -> str:
    left = max(left, 6 * len(timestamp) + 12)
    right = max(right, 7 * len(text) + 10)
    width = left + right
    color = "#1a7f37" if passed else "#b42318"
    return f"""\
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="32" viewBox="0 0 {width} 32" role="img" aria-labelledby="title">
  <title id="title">{label} · {timestamp} — {text}</title>
  <clipPath id="badge"><rect width="{width}" height="32" rx="3"/></clipPath>
  <g clip-path="url(#badge)">
    <rect width="{width}" height="32" fill="#424a53"/>
    <rect x="{left}" width="{right}" height="32" fill="{color}"/>
  </g>
  <g fill="#fff" font-family="Verdana, Arial, sans-serif" font-size="11" text-anchor="middle">
    <text x="{left / 2:g}" y="13">{label}</text>
    <text x="{left / 2:g}" y="25" font-size="9.5" fill="#e5e7eb">{timestamp}</text>
    <text x="{left + right / 2:g}" y="20">{text}</text>
  </g>
</svg>
"""


def update_badges(project: Path, result: BadgeResult) -> None:
    counts = result.badge_counts()
    if not counts:
        print("No executed tests to record in the README badges.")
        return

    # Hold the existing checkout lock only while publishing the results.
    from .locks import PROJECT_LOCK, project_lock

    with project_lock(project / PROJECT_LOCK):
        now = datetime.now().astimezone()
        directory = project / "docs/badges"
        directory.mkdir(parents=True, exist_ok=True)
        record_path = directory.parent / "verification-record.md"
        record = record_path.read_text(encoding="utf-8") if record_path.exists() else "# Verification record\n"
        title = f"{now:%Y-%m-%d %H:%M:%S %z} — Test badge run"
        for number in count(1):
            heading = title if number == 1 else f"{title} {number}"
            if f"## {heading}\n" not in record:
                break
        timestamp = f"{now:%Y-%m-%d %H:%M:%S}"
        if number > 1:
            timestamp += f" #{number}"
        anchor = re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
        outcome = "passed" if result.wasSuccessful() else "failed"
        flags = ", ".join(f"`{name}=1`" for name in map(str.upper, TEST_FLAGS) if os.environ.get(name) == "1")
        entry = [
            f"## {heading}", "",
            f"`loaf test --badges` {outcome} on {platform.system()} with Python {platform.python_version()}.",
            f"Unittest reported {result.testsRun} cases and {len(result.skipped)} skips.",
            f"Enabled test options were {flags}." if flags else "All opt-in test options were disabled.", "",
            "| Group | Passed / executed |", "| --- | ---: |",
        ]
        contents = {}
        links = {}
        for group, (passed, total) in counts.items():
            label, left, right = GROUPS[group]
            text = f"{passed}/{total} passed"
            contents[directory / f"{group}.svg"] = badge_svg(label, timestamp, text, left, right, passed=passed == total)
            entry.append(f"| {label} | {passed}/{total} |")
            alt = f"{label} · {timestamp} — {text}"
            links[group] = f"[![{alt}](docs/badges/{group}.svg)](docs/verification-record.md#{anchor})"

        first_entry = re.search(r"(?m)^## ", record)
        at = first_entry.start() if first_entry else len(record)
        new_record = record[:at].rstrip() + "\n\n" + "\n".join(entry) + "\n"
        # Publish the record first so that a README link always has a target.
        contents = {record_path: new_record + ("\n" if record[at:] else "") + record[at:], **contents}
        readme_path = project / "README.md"
        if readme_path.is_file():
            readme = readme_path.read_bytes().decode("utf-8")
            updated = BADGE_LINK.sub(lambda match: links.get(match[1], match[0]), readme)
            if updated != readme:
                contents[readme_path] = updated
        with TemporaryDirectory(prefix=".loaf-badges-", dir=project) as folder:
            staged = {}
            for index, (path, text) in enumerate(contents.items()):
                temporary = Path(folder) / str(index)
                temporary.write_text(text, encoding="utf-8", newline="")
                if path.is_file():
                    shutil.copymode(path, temporary)
                staged[path] = temporary
            for path, temporary in staged.items():
                temporary.replace(path)
    print("Updated test badges for " + ", ".join(GROUPS[group][0] for group in counts) + ".")
