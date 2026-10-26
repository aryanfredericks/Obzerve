#!/usr/bin/env bash
# Test a built `obzerve` wheel the way users get it: installed into a fresh virtual
# environment (not the workspace), against either the oldest dependency versions the
# package declares or the newest available ones.
#
#   tools/test_sdk_wheel.sh <python-version> <minimum|latest> [path/to/obzerve-*.whl]
#
# Without a wheel argument it builds one into dist/ first.
set -euo pipefail

PYTHON_VERSION=${1:?python version, e.g. 3.10}
DEPS=${2:?minimum or latest}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WHEEL=${3:-}

if [[ -z "$WHEEL" ]]; then
  rm -rf "$ROOT/dist"
  uv build --package obzerve -o "$ROOT/dist" >/dev/null
  WHEEL=$(ls "$ROOT"/dist/obzerve-*.whl)
fi

VENV=$(mktemp -d)/venv
trap 'rm -rf "$(dirname "$VENV")"' EXIT
uv venv -q --python "$PYTHON_VERSION" "$VENV"

# Keep these minimums in sync with sdk/python/pyproject.toml.
MINIMUMS=(opentelemetry-api==1.27.0 opentelemetry-sdk==1.27.0
          opentelemetry-exporter-otlp-proto-http==1.27.0 requests==2.28.0)
case "$DEPS" in
  minimum) PINS=("${MINIMUMS[@]}") ;;
  latest) PINS=() ;;
  *) echo "deps must be 'minimum' or 'latest'" >&2; exit 2 ;;
esac

uv pip install -q --python "$VENV/bin/python" "$WHEEL[openai,anthropic,huggingface]" \
  ${PINS[@]+"${PINS[@]}"} "pytest>=8"
"$VENV/bin/python" - <<'PY'
import importlib.metadata as m, platform
names = ("obzerve", "opentelemetry-sdk", "requests", "openai", "anthropic", "huggingface_hub")
print(f"Python {platform.python_version()}: " + ", ".join(f"{n} {m.version(n)}" for n in names))
PY

# Run from outside the repository so `obzerve` resolves to the installed wheel.
cd "$(dirname "$VENV")"
"$VENV/bin/python" -m pytest -q -p no:cacheprovider --import-mode=importlib \
  -c "$ROOT/sdk/python/pyproject.toml" --rootdir "$ROOT/sdk/python" "$ROOT/sdk/python/tests"
