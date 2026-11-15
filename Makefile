.PHONY: sync hooks proto format lint types architecture test build commits check package bump contract release-notes release-gate

# The range of commits `make commits` checks. The first commit of the repository
# predates the rule, so the range starts after it.
COMMIT_RANGE ?= main..HEAD

sync:
	uv sync --locked

hooks:
	git config core.hooksPath .githooks

# Regenerate src/mt5_bridge/api/grpc_api/generated/ from the gRPC contract.
proto:
	uv run --locked python tools/generate_proto.py

format:
	uv run --locked ruff check --fix .
	uv run --locked ruff format .

lint:
	uv lock --check
	uv run --locked ruff check .
	uv run --locked ruff format --check .

types:
	uv run --locked pyright

architecture:
	uv run --locked lint-imports --no-cache

test:
	uv run --locked pytest

build:
	uv build --no-sources

commits:
	uv run --locked cz check --rev-range $(COMMIT_RANGE)
	@if git log --format=%B $(COMMIT_RANGE) | grep -qi '^co-authored-by:'; then \
		echo "a commit in $(COMMIT_RANGE) has a Co-authored-by trailer"; exit 1; fi

check: lint types architecture test build

# The application tree for the Windows guest: the committed files of HEAD under app/.
package:
	mkdir -p dist
	git archive --format=zip --prefix=app/ --output=dist/mt5-bridge-app.zip HEAD
	@echo "dist/mt5-bridge-app.zip holds commit `git rev-parse --short HEAD`"

# The release steps (docs/release.md). INCREMENT=PATCH|MINOR|MAJOR overrides the computed bump.
bump:
	python3 tools/release.py bump $(if $(INCREMENT),--increment $(INCREMENT))

contract:
	python3 tools/release.py contract-check

release-notes:
	python3 tools/release.py notes --output dist/release-notes.md

release-gate:
	tools/release_gate.sh
