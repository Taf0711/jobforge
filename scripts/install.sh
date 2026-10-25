#!/usr/bin/env bash
set -euo pipefail

# Install JobForge from this checkout (or from GitHub if run outside one).
# Usage: ./scripts/install.sh
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ -n "$ROOT" ] && [ -f "$ROOT/pyproject.toml" ]; then
  SOURCE="$ROOT"
else
  SOURCE="git+https://github.com/Taf0711/jobforge"
fi

echo "Installing JobForge from $SOURCE"
uv tool install --force --from "$SOURCE" jobforge

if ! command -v typst >/dev/null 2>&1; then
  echo
  echo "Typst is not installed. Tailoring needs it:"
  echo "  macOS:   brew install typst"
  echo "  Windows: winget install Typst.Typst"
  echo "  Linux:   https://typst.app/docs/guides/installation/"
  echo
fi

if command -v jobforge >/dev/null 2>&1; then
  jobforge setup
  echo
  echo "Done. Launch with: jobforge"
else
  echo "JobForge was installed but is not on PATH."
  echo "Add '$HOME/.local/bin' to PATH, then run: jobforge setup"
fi
