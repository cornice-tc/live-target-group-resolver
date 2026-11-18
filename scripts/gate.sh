#!/usr/bin/env bash
# The checks CI runs; run it locally before pushing.
set -euo pipefail

cd "$(dirname "$0")/.."

uv run ruff format --check .
uv run ruff check .
uv run mypy live_target_group_resolver.py --config-file=pyproject.toml
# cfnresponse.py is vendored AWS code, exempt like ruff is.
uv run bandit -q -r live_target_group_resolver.py
uv run pytest tests/ -v
