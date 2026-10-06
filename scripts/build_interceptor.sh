#!/usr/bin/env bash
# Vendor PyJWT[crypto] next to the interceptor so `cdk deploy` ships it.
# Run before deploy. Idempotent.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$HERE/src/interceptor"

echo "Vendoring PyJWT[crypto] into $TARGET ..."
python3 -m pip install "PyJWT[crypto]>=2.8,<3" \
  --platform manylinux2014_x86_64 \
  --target "$TARGET" \
  --implementation cp --python-version 3.13 \
  --only-binary=:all: --upgrade

# Drop dist-info / pycache to keep the asset small.
find "$TARGET" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$TARGET" -type d -name '*.dist-info' -prune -exec rm -rf {} + 2>/dev/null || true

echo "Done. Interceptor asset ready."
