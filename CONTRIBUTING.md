# Contributing

## Setup

Python 3.14 and [uv](https://docs.astral.sh/uv/). Once per clone:

```bash
make sync
```

```bash
make hooks
```

`make hooks` points Git at `.githooks/`, so every commit message is checked
when it is made.

## Checks

```bash
make check
```

It runs, in order:

| Step | Command | What it checks |
| --- | --- | --- |
| `make lint` | `uv lock --check`, `ruff check`, `ruff format --check` | the lock file matches `pyproject.toml`; lint rules; formatting |
| `make types` | `pyright` | strict type checking of `src` and `tests` |
| `make architecture` | `lint-imports` | only `mt5/real_client.py` imports `MetaTrader5`; the MCP adapter and the service do not import each other |
| `make test` | `pytest` | unit and contract tests against the fake terminal |
| `make build` | `uv build --no-sources` | the package builds |

`make format` applies the lint fixes and the formatter. CI runs `make check`
on every pull request and on `main` and `develop`, and a second job installs
the locked environment on Windows and runs the tests there.

No check exercises the real terminal. A change to `mt5/real_client.py`, to
the `MetaTrader5` pin or to `deploy/` needs a run in the Windows guest
([docs/install-utm.md](docs/install-utm.md)); say in the pull request
whether that run happened.

## Code rules

- `MetaTrader5` is imported only in `src/mt5_bridge/mt5/real_client.py`.
- Every test runs against `mt5/fake_client.py`. A test never needs Windows, a
  terminal, a network or a credential.
- Prices, volumes and money are `Decimal` inside the service and decimal
  strings in the contract. Conversion from the terminal's floats happens only
  in `real_client.py`.
- No credential, account number or certificate enters the repository.
  Secrets come from environment variables at run time.
- New behaviour comes with a test. A bug fix comes with the test that failed
  before it.
- Dependencies are pinned in `pyproject.toml` and locked in `uv.lock`; both
  change in the same commit.

## The contract

`contracts/mt5_bridge/` is the source of truth for the server and for every
client.

- An API change edits the contract files in the same commit as the server
  code. The tests in `tests/contracts/` must pass.
- `info.version` in `openapi.yaml` follows [SemVer](https://semver.org/):
  a major number for a change that breaks an existing client (a removed or
  renamed path or field, a changed type or meaning), a minor number for an
  addition an existing client can ignore, a patch number for a correction of
  text.
- The contract version and the release version are separate numbers, tied
  by rules the release gate enforces ([docs/release.md](docs/release.md)):
  changed contract files need a higher contract version, and the release
  notes state it. `make contract` checks them.
- `mt5_bridge.proto` is the gRPC form of the same API. After editing it, run
  `make proto` and commit the regenerated files under
  `src/mt5_bridge/api/grpc_api/generated/` in the same commit.
- The version the server reports in `src/mt5_bridge/api/app.py` equals the
  contract version.
- The MCP adapter's tools are generated from `openapi.yaml`. Run
  `make mcp-operations` after a contract change and commit
  `src/mt5_bridge/mcp_adapter/operations.json` with it; a test fails while
  it is stale ([docs/mcp.md](docs/mcp.md)).
- A commit that changes the contract uses the scope `contract`; a breaking
  one also carries `!` and a `BREAKING CHANGE:` footer.

## Commits

Every commit follows [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <summary>
```

- Types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`,
  `chore`, `revert`.
- Scopes are optional. The ones in use: `api`, `core`, `mt5`, `mcp`,
  `contract`, `deploy`, `docs`, `deps`, `ci`, `repo`. `version` belongs to the commit
  `make bump` makes.
- The summary is in the imperative, lower case, without a final period, and
  the whole first line has at most 100 characters.
- No `Co-authored-by` trailer, on any commit. This holds for commits made
  with an AI assistant too.

The `commit-msg` hook rejects a message that breaks the format or carries the
trailer. `make commits` checks the same for a range of commits, and CI runs
it on every pull request.

## Branches and releases

Git Flow: `main` holds released code only, `develop` collects the next
release, and work happens on `feature/<name>` branches cut from `develop`.
A release is made on a `release/<x.y.z>` branch and a fix to released code
on a `hotfix/<x.y.z>` branch; both merge into `main` and back into
`develop`. Branch and pull request names say what changes, in plain words,
with no internal task codes.

The release version comes from the commits (`make bump`), every release
passes `make release-gate`, and every release has notes that state whether
the contract changed. The full procedure is in
[docs/release.md](docs/release.md).

## Licence

The repository has no licence file. Until the owner chooses one, the code is
not licensed for use by others.
