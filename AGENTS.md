# MT5 bridge

Rules for this repository. The contract is in `contracts/mt5_bridge/`, the
runbook is [docs/runbook.md](docs/runbook.md).

- The bridge is a standalone uv project (`mt5_bridge`, its own `.venv` and `uv.lock`). It imports no
  client project and no client project imports it; clients use the contract files only. Run its
  commands from the repository root: `make check` (lint, format, types, import rule, tests, build).
- Commits follow Conventional Commits and never carry a `Co-authored-by` trailer; branches follow
  Git Flow. The rules are in [CONTRIBUTING.md](CONTRIBUTING.md); releases, tags and release notes
  follow [docs/release.md](docs/release.md) and are the owner's to make.
- `MetaTrader5` is imported only in `src/mt5_bridge/mt5/real_client.py` (import-linter enforces it);
  every test runs against the deterministic fake terminal in `mt5/fake_client.py`. The real client
  cannot be tested here: no Windows host or terminal is available outside the guest.
- An API change edits `contracts/mt5_bridge/` in the same commit as the server code; the tests in
  `tests/contracts/` must keep passing.
- Do not change UTM, start a terminal or place live credentials in code or tests.
- Credentials come from environment variables set on the machine (`MT5_BRIDGE_LOGIN`,
  `MT5_BRIDGE_PASSWORD`, `MT5_BRIDGE_SERVER`), never from a file in this repository. The API has
  no client authentication: access control is the network's job (bind address, guest firewall).
  Limits, the kill switch and the dead-man switch stay in
  `core/`, outside any client.
- Comments cite the design rules by number (`plan §N`, `ADR-NNN`, `rule N`); see
  [docs/design-rules.md](docs/design-rules.md) for what each one says.
