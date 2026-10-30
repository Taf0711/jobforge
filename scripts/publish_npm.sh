#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/installer"

VERSION="$(node -p "require('./package.json').version")"
TAG="v${VERSION}"

if ! git -C "$ROOT" rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Tag $TAG is missing. Push the release tag first." >&2
  exit 2
fi
if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
  echo "Working tree is dirty. Commit the release before publishing." >&2
  exit 2
fi
if [ "$(npm view jobforge version 2>/dev/null || true)" = "$VERSION" ]; then
  echo "jobforge@$VERSION is already published." >&2
  exit 0
fi

echo "Publishing jobforge@$VERSION from $TAG"
npm ci
npm pack --dry-run
npm publish --access public "$@"
