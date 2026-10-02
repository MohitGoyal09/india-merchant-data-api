"""``scripts/scan_secrets.py``: high-signal secret patterns in git-tracked files.

Every fake key below is built at run time, so this file never contains a match itself.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"
FILLER = "Ab3_" * 10  # 40 characters of key-looking filler


@pytest.fixture(scope="module")
def scan_secrets() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import scan_secrets as module
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    return tmp_path


def track(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(root, "add", "-f", name)


# rule name -> a line that must trigger it
FAKES: dict[str, Callable[[], str]] = {
    "anthropic-key": lambda: "KEY=" + "sk-" + "ant-" + FILLER,
    "aws-access-key": lambda: "id: " + "AKI" + "A" + "ABCDEFGH23456789",
    "github-token": lambda: "t = " + "gh" + "p_" + "A1b2" * 10,
    "slack-token": lambda: "SLACK=" + "xo" + "xb-1234-abcd",
    "private-key": lambda: "-----BEGIN " + "OPENSSH " + "PRIVATE KEY-----",
    "generic-secret": lambda: 'password = "' + FILLER + '"',
}


@pytest.mark.parametrize("rule", sorted(FAKES))
def test_each_rule_flags_its_pattern_by_file_line_and_rule(
    scan_secrets: ModuleType, repo: Path, rule: str
) -> None:
    track(repo, "src/app.txt", "first line\n" + FAKES[rule]() + "\n")

    findings = scan_secrets.scan_repo(repo)

    assert [(f.path, f.line, f.rule) for f in findings] == [("src/app.txt", 2, rule)]


def test_a_clean_repo_exits_zero(
    scan_secrets: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    track(repo, "README.md", "nothing to see\npassword = os.environ['PW']\n")

    assert scan_secrets.main(["--root", str(repo)]) == 0
    assert "no secrets" in capsys.readouterr().out


def test_findings_exit_one_and_never_print_the_value(
    scan_secrets: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line = FAKES["anthropic-key"]()
    track(repo, "config/a.env", line + "\n")

    code = scan_secrets.main(["--root", str(repo)])

    out = capsys.readouterr()
    assert code == 1
    assert "config/a.env:1: anthropic-key" in out.out + out.err
    assert FILLER not in out.out + out.err
    assert "sk-" not in out.out + out.err


def test_short_or_unquoted_generic_values_are_not_flagged(
    scan_secrets: ModuleType, repo: Path
) -> None:
    track(
        repo,
        "a.py",
        'token = "short"\napi_key = os.environ["X"]\nsecret: "' + "x" * 23 + '"\n',
    )

    assert scan_secrets.scan_repo(repo) == []


def test_untracked_and_binary_files_are_skipped(scan_secrets: ModuleType, repo: Path) -> None:
    (repo / "untracked.txt").write_text(FAKES["aws-access-key"]() + "\n", encoding="utf-8")
    path = repo / "blob.bin"
    path.write_bytes(b"\x00\x01" + FAKES["aws-access-key"]().encode())
    _git(repo, "add", "-f", "blob.bin")

    assert scan_secrets.scan_repo(repo) == []


def test_allowlist_path_and_line_suppresses_only_that_line(
    scan_secrets: ModuleType, repo: Path
) -> None:
    fake = FAKES["github-token"]()
    track(repo, "tests/a.py", f"{fake}\n{fake}\n")
    track(repo, ".secrets-allowlist", "# deliberately fake\ntests/a.py:1\n")

    findings = scan_secrets.scan_repo(repo)

    assert [(f.path, f.line) for f in findings] == [("tests/a.py", 2)]


def test_allowlist_path_alone_suppresses_the_whole_file(
    scan_secrets: ModuleType, repo: Path
) -> None:
    track(repo, "tests/a.py", FAKES["github-token"]() + "\n")
    track(repo, ".secrets-allowlist", "tests/a.py\n")

    assert scan_secrets.scan_repo(repo) == []


def test_allowlist_regex_matches_the_offending_line(scan_secrets: ModuleType, repo: Path) -> None:
    track(repo, "a.txt", "FAKE_OK " + FAKES["aws-access-key"]() + "\n" + FAKES["aws-access-key"]())
    track(repo, ".secrets-allowlist", "re:^FAKE_OK \n")

    findings = scan_secrets.scan_repo(repo)

    assert [(f.path, f.line) for f in findings] == [("a.txt", 2)]


def test_the_allowlist_file_itself_is_not_scanned(scan_secrets: ModuleType, repo: Path) -> None:
    track(repo, ".secrets-allowlist", "re:" + FAKES["aws-access-key"]() + "\n")

    assert scan_secrets.scan_repo(repo) == []


def test_this_repository_has_no_secrets(scan_secrets: ModuleType) -> None:
    assert scan_secrets.scan_repo(REPO) == []


def test_tracked_files_without_git_skips_venv_data_and_env(
    scan_secrets: ModuleType, tmp_path: Path
) -> None:
    # An unpacked submission zip has no .git; the scan must still cover the source tree.
    for rel in ["src/a.py", ".venv/lib/x.py", "data/imda.sqlite3", ".env", "docs/b.md"]:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    assert scan_secrets.tracked_files(tmp_path) == ["docs/b.md", "src/a.py"]
