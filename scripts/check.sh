#!/usr/bin/env bash
# Everything CI checks, locally: ./scripts/check.sh [--no-blender]
set -euo pipefail
cd "$(dirname "$0")/.."

cargo fmt --all -- --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked --quiet
python3 scripts/check_version.py
if command -v ruff >/dev/null; then ruff check blender tests scripts
elif command -v uvx >/dev/null; then uvx ruff check blender tests scripts
else echo "ruff not found; skipping Python lint" >&2
fi
python3 scripts/build_addon.py
python3 tests/test_native_reload.py
if [[ "${1:-}" != "--no-blender" ]]; then
    python3 scripts/run_blender_tests.py
fi
echo "All checks passed"
