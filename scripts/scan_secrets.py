"""Scan git-tracked files for high-signal secret patterns. Offline; exits 1 on a finding.

Usage: ``uv run python scripts/scan_secrets.py [--root DIR]``. Output is ``path:line: rule`` only;
the matched text is never printed.

Deliberately fake values (test fixtures) go in ``.secrets-allowlist`` at the repository root, one
entry per line (``#`` starts a comment):

- ``path/to/file:12``   skip that line of that file
- ``path/to/file``      skip the whole file
- ``re:PATTERN``        skip any line where ``PATTERN`` matches (``re.search``)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_NAME = ".secrets-allowlist"
MAX_BYTES = 5_000_000
BINARY_SNIFF_BYTES = 8000

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("slack-token", re.compile(r"xox[baprs]-")),
    ("private-key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    (
        "generic-secret",
        re.compile(
            r"""(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['"][A-Za-z0-9_\-]{24,}['"]"""
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


@dataclass(frozen=True, slots=True)
class Allowlist:
    lines: frozenset[tuple[str, int]] = frozenset()
    files: frozenset[str] = frozenset()
    patterns: tuple[re.Pattern[str], ...] = ()

    def allows(self, path: str, number: int, text: str) -> bool:
        return (
            path in self.files
            or (path, number) in self.lines
            or any(p.search(text) for p in self.patterns)
        )


def load_allowlist(root: Path) -> Allowlist:
    path = root / ALLOWLIST_NAME
    if not path.is_file():
        return Allowlist()
    lines: set[tuple[str, int]] = set()
    files: set[str] = set()
    patterns: list[re.Pattern[str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("re:"):
            patterns.append(re.compile(entry[3:]))
            continue
        name, _, number = entry.rpartition(":")
        if name and number.isdigit():
            lines.add((name, int(number)))
        else:
            files.add(entry)
    return Allowlist(frozenset(lines), frozenset(files), tuple(patterns))


def tracked_files(root: Path) -> list[str]:
    done = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [name for name in done.stdout.decode("utf-8").split("\0") if name]


def scan_text(path: str, text: str, allow: Allowlist) -> list[Finding]:
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if allow.allows(path, number, line):
            continue
        found.extend(Finding(path, number, rule) for rule, pattern in RULES if pattern.search(line))
    return found


def _readable_text(path: Path) -> str | None:
    """The file's text, or None for a missing, oversized or binary file."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_repo(root: Path) -> list[Finding]:
    allow = load_allowlist(root)
    findings: list[Finding] = []
    for name in tracked_files(root):
        if name == ALLOWLIST_NAME:
            continue
        text = _readable_text(root / name)
        if text is not None:
            findings.extend(scan_text(name, text, allow))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    args = parser.parse_args(argv)
    findings = scan_repo(args.root)
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} possible secret(s); values are not shown. Rotate real ones.")
        print(f"Deliberately fake values go in {ALLOWLIST_NAME}.")
        return 1
    print("no secrets found in tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
