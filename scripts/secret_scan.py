"""Secret-leak scan: fail if a credential is committed to the repository or shows up in a log.

Scans every git-tracked text file (never ``.env`` files, which are not tracked and are not read)
or the files given on the command line, for:

* well-known credential shapes: cloud access keys, private keys, provider API tokens, Slack and
  GitHub tokens, JWTs, and URLs with an embedded password;
* with ``--value NAME`` (repeatable), the value of that environment variable - how CI checks
  that a secret it configured for a test run did not reach the captured logs.

A line that must hold such a shape (a documentation placeholder) is exempted by ending it with
the marker ``secret-scan: allow``. Findings print as ``path:line: kind`` - never the matched
text - and the exit status is 1 when there is any.

Usage:  python scripts/secret_scan.py [FILE ...] [--value ENV_NAME ...]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess  # nosec B404 - fixed argument list, no shell
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOW_MARKER = "secret-scan: allow"
MAX_BYTES = 2_000_000  # larger tracked files are data (lock files, fixtures), not source

PATTERNS: dict[str, re.Pattern[str]] = {
    "aws-access-key-id": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "anthropic-api-key": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),
    "openai-api-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
    "stripe-secret-key": re.compile(r"\b[sr]k_live_[A-Za-z0-9]{20,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "url-with-password": re.compile(
        r"\b[a-z][a-z0-9+.\-]*://[^\s:/@'\"]+:(?!\*\*\*|<|\$\{|%|\{)([^\s@/'\"]{6,})@[^\s'\"]+"
    ),
}
# Passwords in documentation URLs that are placeholders, not credentials.
PLACEHOLDER_PASSWORDS = frozenset({"password", "change-me", "changeme", "secret", "example"})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


def tracked_files(root: Path = ROOT) -> list[Path]:
    """Git-tracked files under ``root``, minus ``.env`` files (never read)."""
    out = subprocess.run(  # nosec B603 B607 - fixed git invocation
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode("utf-8")
    files = []
    for rel in filter(None, out.split("\0")):
        name = Path(rel).name
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            continue
        files.append(root / rel)
    return files


def _lines(path: Path) -> Iterable[tuple[int, str]]:
    try:
        if path.stat().st_size > MAX_BYTES:
            return
        with path.open(encoding="utf-8", errors="strict") as fh:
            yield from enumerate(fh, start=1)
    except (UnicodeDecodeError, OSError):
        return  # binary or unreadable: not text a secret could be pasted into


def scan_paths(paths: Iterable[Path], *, values: Iterable[str] = ()) -> list[Finding]:
    """Findings in ``paths``; ``values`` are literal secret values to look for as well."""
    literal = [v for v in values if v and len(v) >= 3]
    findings: list[Finding] = []
    for path in paths:
        try:
            shown = path.relative_to(ROOT).as_posix()
        except ValueError:
            shown = str(path)
        for number, line in _lines(path):
            if ALLOW_MARKER in line:
                continue
            for kind, pattern in PATTERNS.items():
                match = pattern.search(line)
                if match and not (match.groups() and match.group(1) in PLACEHOLDER_PASSWORDS):
                    findings.append(Finding(shown, number, kind))
            if any(v in line for v in literal):
                findings.append(Finding(shown, number, "configured-secret-value"))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--value", action="append", default=[], metavar="ENV_NAME",
                        help="also look for the value of this environment variable")
    args = parser.parse_args(argv)
    values = [os.environ.get(name, "") for name in args.value]
    paths = args.files or tracked_files()
    findings = scan_paths(paths, values=values)
    for finding in findings:
        print(finding)
    print(f"secret scan: {len(findings)} finding(s) in {len(paths)} file(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
