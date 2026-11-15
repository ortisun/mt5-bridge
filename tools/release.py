"""Release helpers: the branch rule, the contract-version rule and the release notes.

Standard library only, so it runs before any environment is synced. The procedure that
uses it is in docs/release.md.

    python3 tools/release.py bump [--increment MAJOR|MINOR|PATCH]
    python3 tools/release.py contract-check [--base REF]
    python3 tools/release.py notes [--base REF] [--output FILE]
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = "contracts/mt5_bridge"
OPENAPI = f"{CONTRACT_DIR}/openapi.yaml"
APP = "src/mt5_bridge/api/app.py"
RELEASE_BRANCH = re.compile(r"^(release|hotfix)/.+")
INFO_VERSION = re.compile(
    r'^info:\n(?:[ \t]+.*\n)*?[ \t]+version:[ \t]*"?([0-9]+\.[0-9]+\.[0-9]+)"?', re.M
)
APP_VERSION = re.compile(r'FastAPI\([^)]*version="([^"]+)"')
CONTRACT_SUBJECT = re.compile(r"^[a-z]+\(contract\)!?: ")
BREAKING_SUBJECT = re.compile(r"^[a-z]+(\([^)]*\))?!: ")


class ReleaseError(Exception):
    """A rule of the release procedure does not hold."""


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def current_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD")


def require_release_branch() -> None:
    branch = current_branch()
    if not RELEASE_BRANCH.match(branch):
        raise ReleaseError(
            f"a version is made only on a release/ or hotfix/ branch; this is {branch!r}"
        )


def bump(increment: str | None) -> int:
    """Make the version commit and the tag with commitizen, on a release branch only.

    The branch and the working tree are checked first: commitizen writes the version
    files before it runs any hook of its own, so a late refusal would leave them changed.
    """
    require_release_branch()
    if git("status", "--porcelain"):
        raise ReleaseError("uncommitted changes; commit or discard them before the bump")
    command = ["uv", "run", "--locked", "cz", "bump", "--yes"]
    if increment:
        command += ["--increment", increment]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def parse_version(text: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in text.split("."))
    return major, minor, patch


def contract_version(text: str, where: str) -> str:
    found = INFO_VERSION.search(text)
    if found is None:
        raise ReleaseError(f"no info.version in {OPENAPI} at {where}")
    return found.group(1)


def previous_tag() -> str | None:
    """The newest release tag in the history of HEAD that is not on HEAD itself."""
    on_head = set(git("tag", "--points-at", "HEAD").split())
    merged = git("tag", "--merged", "HEAD", "--list", "v*", "--sort=-version:refname").split()
    for tag in merged:
        if tag not in on_head:
            return tag
    return None


def commits(base: str | None) -> list[tuple[str, str]]:
    """(subject, body) of every commit after ``base`` up to HEAD."""
    rev = f"{base}..HEAD" if base else "HEAD"
    raw = git("log", "--format=%s%x1f%b%x1e", rev)
    result: list[tuple[str, str]] = []
    for record in raw.split("\x1e"):
        if record.strip():
            subject, _, body = record.strip().partition("\x1f")
            result.append((subject, body))
    return result


def contract_state(base: str | None) -> tuple[str, str | None, bool]:
    """The contract version now, at ``base``, and whether the contract files changed."""
    now = contract_version((ROOT / OPENAPI).read_text(encoding="utf-8"), "HEAD")
    if base is None:
        return now, None, False
    before = contract_version(git("show", f"{base}:{OPENAPI}"), base)
    changed = bool(git("diff", "--name-only", base, "HEAD", "--", CONTRACT_DIR))
    return now, before, changed


def contract_check(base: str | None) -> str:
    """Fail when the contract and its version disagree; return one line describing them."""
    now, before, changed = contract_state(base)

    app = APP_VERSION.search((ROOT / APP).read_text(encoding="utf-8"))
    if app is None or app.group(1) != now:
        served = app.group(1) if app else "none"
        raise ReleaseError(
            f"{APP} serves version {served} but the contract is {now}; they must be equal"
        )

    if before is None:
        return f"contract {now} (no earlier release to compare with)"
    if not changed:
        if now != before:
            raise ReleaseError(
                f"info.version went from {before} to {now} but no file in {CONTRACT_DIR} changed"
            )
        return f"contract {now}, unchanged since {base}"

    if parse_version(now) <= parse_version(before):
        raise ReleaseError(
            f"{CONTRACT_DIR} changed since {base} but info.version is still {now} "
            f"(was {before}); raise it"
        )
    history = commits(base)
    if not any(CONTRACT_SUBJECT.match(subject) for subject, _ in history):
        raise ReleaseError(
            f"{CONTRACT_DIR} changed since {base} but no commit has the scope 'contract', "
            "so the changelog would not say it"
        )
    if parse_version(now)[0] > parse_version(before)[0] and not any(
        BREAKING_SUBJECT.match(subject) or "BREAKING CHANGE" in body for subject, body in history
    ):
        raise ReleaseError(
            f"the contract went from {before} to {now}, a breaking change, but no commit is "
            "marked with '!' or a 'BREAKING CHANGE:' footer"
        )
    return f"contract {now}, changed from {before} since {base}"


def changelog_section(version: str) -> str:
    """The section of CHANGELOG.md for ``version``, without its heading."""
    lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    heading = re.compile(rf"^## v?{re.escape(version)}\b")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise ReleaseError(f"CHANGELOG.md has no section for {version}; run cz bump first")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1 : end]).strip()


def package_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r'^version = "([^"]+)"', text, re.M)
    if found is None:
        raise ReleaseError("no version in pyproject.toml")
    return found.group(1)


def notes(base: str | None) -> str:
    version = package_version()
    now, before, changed = contract_state(base)
    contract_check(base)
    if before is None:
        contract = (
            f"REST and WebSocket contract version **{now}**. This is the first release, "
            "so there is no earlier contract to compare with."
        )
    elif changed:
        subjects = list(dict.fromkeys(s for s, _ in commits(base) if CONTRACT_SUBJECT.match(s)))
        listed = "\n".join(f"- {subject}" for subject in subjects)
        contract = (
            f"**The contract changed**: version {before} to **{now}**. "
            f"Clients that keep a copy of `{CONTRACT_DIR}/` need the new files.\n\n{listed}"
        )
    else:
        contract = (
            f"Contract version **{now}**, unchanged since {base}. "
            "Clients need no change to their copy of the contract."
        )
    return f"# v{version}\n\n## Contract\n\n{contract}\n\n## Changes\n\n{changelog_section(version)}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Release helpers (docs/release.md).")
    commands = parser.add_subparsers(dest="command", required=True)
    bump_parser = commands.add_parser("bump")
    bump_parser.add_argument("--increment", choices=["MAJOR", "MINOR", "PATCH"])
    for name in ("contract-check", "notes"):
        sub = commands.add_parser(name)
        sub.add_argument("--base", help="release tag to compare with (default: the previous one)")
        if name == "notes":
            sub.add_argument("--output", type=Path, help="write the notes here, not to stdout")
    args = parser.parse_args()

    try:
        if args.command == "bump":
            return bump(args.increment)
        base: str | None = args.base or previous_tag()
        if args.command == "contract-check":
            print(contract_check(base))
            return 0
        text = notes(base)
        if args.output is None:
            print(text, end="")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
            print(f"wrote {args.output}")
        return 0
    except ReleaseError as error:
        print(f"release: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
