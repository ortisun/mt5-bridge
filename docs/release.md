# Branches and releases

The repository follows Git Flow. A release is a `release/x.y.z` branch cut
from `develop`, a version commit and a tag made by `make bump`, a gate that
runs every check, a merge into `main`, and a merge back into `develop`. The
tag is pushed last, and pushing it publishes the release notes.

Nothing is pushed, tagged on the remote or merged for you: you run each step.

## Branches

| Branch | Holds | Starts from | Merges into |
| --- | --- | --- | --- |
| `main` | released code only; every commit on it is a release | | |
| `develop` | the next release | | |
| `feature/<name>` | one change | `develop` | `develop`, squashed |
| `release/<x.y.z>` | a release being prepared; only fixes enter | `develop` | `main` and `develop`, merge commits |
| `hotfix/<x.y.z>` | a fix to released code | `main` | `main` and `develop`, merge commits |

A feature is squashed into `develop`, so the pull request title is the commit
message and must be a Conventional Commit. Release and hotfix branches are
merged with a merge commit, never squashed: the tag must stay in the history
of `main`.

## Versions

Two numbers, each following [SemVer](https://semver.org/):

| Number | Where | What raises it |
| --- | --- | --- |
| Release version | `pyproject.toml`, the tag `v<x.y.z>` | computed from the commits since the last tag: `fix` raises the patch number, `feat` the minor number, a breaking change the major number (the minor number before 1.0.0) |
| Contract version | `info.version` in `contracts/mt5_bridge/openapi.yaml`, and the version the server reports | raised by hand in the commit that changes the contract: major for a change that breaks a client, minor for an addition, patch for a correction of text |

They are separate because a release that only fixes the server must not tell
clients that the contract moved. They are tied by rules the gate enforces:

1. If any file in `contracts/mt5_bridge/` changed since the last release, the
   contract version must be higher than it was. If no file changed, it must
   be the same.
2. A release that changes the contract has at least one commit with the scope
   `contract`, so the changelog names the change.
3. A higher major contract version needs a commit marked as breaking (`!` or
   a `BREAKING CHANGE:` footer), which also raises the release version
   accordingly.
4. The version the server reports (`FastAPI(version=...)` in
   `src/mt5_bridge/api/app.py`) equals the contract version.

`make contract` checks these at any time.

## Release

1. **Cut the branch** from `develop`. The version is not known yet, so start
   with a temporary name:

   ```bash
   git switch develop
   git switch -c release/next
   ```

2. **Make the version.**

   ```bash
   make bump
   ```

   It refuses to run outside a `release/` or `hotfix/` branch or with
   uncommitted changes. It computes the version from the commits, writes it
   to `pyproject.toml` and `uv.lock`, adds the section of this release to
   `CHANGELOG.md`, and makes the commit `chore(version): v<x.y.z>` and the
   annotated tag `v<x.y.z>`, both local. `make bump INCREMENT=PATCH` (or
   `MINOR`, `MAJOR`) overrides the computed step. Then give the branch its
   name:

   ```bash
   git branch -m release/<x.y.z>
   ```

3. **Run the gate** on the tagged commit:

   ```bash
   make release-gate
   ```

   In order: the branch name, a clean working tree, the tag on `HEAD`,
   `uv sync --locked`, `make check` (lock file, Ruff, Pyright, import rule,
   tests, build), the commit messages since `main`, the contract rules, and
   the release notes, written to `dist/release-notes.md`. It stops at the
   first failure. `BASE=<branch>` changes where the commit messages are
   checked from.

   If it fails, drop the version commit and the tag, fix on the release
   branch and run `make bump` again:

   ```bash
   git tag -d v<x.y.z>
   git reset --hard HEAD~1
   ```

4. **Read `dist/release-notes.md`.** It is what the release will say. Its
   first section states whether the contract changed.

5. **Merge into `main`** with a merge commit, by a pull request titled
   `chore(repo): release <x.y.z>`.

6. **Push the tag**, only now:

   ```bash
   git push origin v<x.y.z>
   ```

   The `Release` workflow refuses a tag that is not on `main`, runs the
   checks again, and creates the GitHub Release with the notes, the wheel,
   the source distribution and `mt5-bridge-app.zip`, the application tree
   for the Windows guest.

7. **Merge `main` back into `develop`** with a merge commit.

8. **Deploy** the tagged commit to the guest
   ([install-utm.md](install-utm.md)), so the running bridge is an exact
   release.

## Hotfix

The same steps from `main`:

```bash
git switch main
git switch -c hotfix/next
```

Commit the fix with its test, then continue from step 2. A hotfix normally
raises the patch number.

## Release notes

Each release has notes in two places, made from the same commits:

- `CHANGELOG.md`, one section per release, written by `make bump`. Commits of
  type `feat`, `fix`, `refactor` and `perf` appear; `docs`, `test`, `build`,
  `ci` and `chore` do not.
- The GitHub Release, whose text is `dist/release-notes.md`: a **Contract**
  section, then the changelog section of that release. The Contract section
  says either that the contract is unchanged and clients need no change, or
  that it changed, from which version to which, with the commits that
  changed it.

`make release-notes` writes the notes for the current version at any time
after the bump.

## What has been tried

The whole procedure was rehearsed on 2026-11-15 in a throwaway local clone:
a first release, then a second one that changed the contract, including the
three failures of the contract rules and the refusal of `make bump` on
`develop`. Not tried: anything on GitHub. The `Release` workflow, the
`CI` workflow and the protection of `main` have never run. No release
exists yet; the first `make bump` computes `v0.2.0`.

Branch protection is not configured. To make these rules binding on GitHub,
protect `main` and `develop`: require a pull request and the `Quality` check,
and forbid direct pushes.
