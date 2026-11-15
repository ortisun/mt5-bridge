#!/bin/sh
# The release gate (docs/release.md). Run it on a release/ or hotfix/ branch, on the commit
# that `cz bump` made and tagged. It stops at the first failure and changes nothing.
#
#   tools/release_gate.sh            commits are checked from main
#   BASE=develop tools/release_gate.sh
set -eu

cd "$(dirname "$0")/.."
BASE="${BASE:-main}"

step() { printf '\n== %s\n' "$1"; }

step "release or hotfix branch"
case "$(git rev-parse --abbrev-ref HEAD)" in
  release/*|hotfix/*) ;;
  *) echo "release gate: not a release/ or hotfix/ branch" >&2; exit 1 ;;
esac

step "clean working tree"
if [ -n "$(git status --porcelain)" ]; then
  echo "release gate: uncommitted changes" >&2
  exit 1
fi

step "version commit is tagged"
version="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)"
if ! git tag --points-at HEAD | grep -qx "v$version"; then
  echo "release gate: HEAD has no tag v$version; run 'make bump' first" >&2
  exit 1
fi
echo "v$version"

step "locked environment"
uv sync --locked

step "lock file, style, types, import rule, tests, build"
make check

step "commit messages since $BASE"
make commits COMMIT_RANGE="$BASE..HEAD"

step "contract version"
python3 tools/release.py contract-check

step "release notes"
python3 tools/release.py notes --output dist/release-notes.md

printf '\nrelease gate passed for v%s\n' "$version"
