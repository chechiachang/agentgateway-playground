#!/bin/sh
set -eu

AGCTL_VERSION=v1.4.0-alpha.2
MODEL_CATALOG_PROVIDERS=${MODEL_CATALOG_PROVIDERS:-anthropic,azure,openai,amazon-bedrock}
SCRIPT_DIR=$(CDPATH='' cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH='' cd "$SCRIPT_DIR/.." && pwd)
OUTPUT_DIR="$REPO_ROOT/data/model-catalog"
OUTPUT_FILE="$OUTPUT_DIR/catalog.json"

case "$(uname -s)/$(uname -m)" in
  Darwin/arm64|Darwin/aarch64)
    AGCTL_ASSET=agctl-darwin-arm64
    AGCTL_SHA256=47940edefd0ed1205ae9e88602d3020c9fc37f8117ad3e3e025109d980f44183
    ;;
  Linux/x86_64|Linux/amd64)
    AGCTL_ASSET=agctl-linux-amd64
    AGCTL_SHA256=032f50c6066dff5688c5e363b9ba2b19ffb9507803b83eaf5c4b44592d42f5d5
    ;;
  Linux/aarch64|Linux/arm64)
    AGCTL_ASSET=agctl-linux-arm64
    AGCTL_SHA256=bd4886ab98b101ff43b00972c5a0b1b5a40ee177a8145b851165607bdc850b87
    ;;
  *)
    echo "Unsupported agctl platform: $(uname -s)/$(uname -m)" >&2
    exit 1
    ;;
esac

command -v curl >/dev/null 2>&1 || { echo 'curl is required' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo 'python3 is required' >&2; exit 1; }

mkdir -p "$OUTPUT_DIR"
chmod 0755 "$OUTPUT_DIR"
DOWNLOAD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/agctl-costs.XXXXXX")
STAGE_DIR=$(mktemp -d "$OUTPUT_DIR/.refresh.XXXXXX")
cleanup() {
  rm -rf "$DOWNLOAD_DIR" "$STAGE_DIR"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

AGCTL="$DOWNLOAD_DIR/agctl"
CATALOG_TMP="$STAGE_DIR/catalog.json"
BASE_URL="https://github.com/agentgateway/agentgateway/releases/download/$AGCTL_VERSION"

echo "Downloading agctl $AGCTL_VERSION ($AGCTL_ASSET)"
curl --fail --location --silent --show-error --retry 2 \
  "$BASE_URL/$AGCTL_ASSET" -o "$AGCTL"
ACTUAL_SHA256=$(python3 - "$AGCTL" <<'PY'
import hashlib
import sys

h = hashlib.sha256()
with open(sys.argv[1], "rb") as binary:
    for chunk in iter(lambda: binary.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
)
if [ "$ACTUAL_SHA256" != "$AGCTL_SHA256" ]; then
  echo "agctl checksum mismatch: expected $AGCTL_SHA256, got $ACTUAL_SHA256" >&2
  exit 1
fi
chmod 0755 "$AGCTL"

echo "Importing models.dev prices ($MODEL_CATALOG_PROVIDERS)"
"$AGCTL" costs import \
  --source models.dev \
  --providers "$MODEL_CATALOG_PROVIDERS" \
  --pretty \
  -o "$CATALOG_TMP"

python3 - "$CATALOG_TMP" <<'PY'
import json
import sys
from decimal import Decimal, InvalidOperation

def reject_constant(value):
    raise ValueError(f"invalid number {value}")

with open(sys.argv[1], encoding="utf-8") as source:
    catalog = json.load(source, parse_constant=reject_constant)

if not isinstance(catalog, dict) or not isinstance(catalog.get("providers"), dict):
    raise SystemExit("Invalid catalog: expected a providers object")

providers = catalog["providers"]
if not providers:
    raise SystemExit("Invalid catalog: no providers")

price_fields = {"input", "output", "cacheRead", "cacheWrite"}
model_count = 0
priced_count = 0
for provider_name, provider in providers.items():
    models = provider.get("models") if isinstance(provider, dict) else None
    if not isinstance(models, dict) or not models:
        raise SystemExit(f"Invalid catalog: provider {provider_name!r} has no models")
    for model_name, model in models.items():
        model_count += 1
        rates = model.get("rates") if isinstance(model, dict) else None
        if not isinstance(rates, dict):
            continue
        for field, value in rates.items():
            if field not in price_fields:
                continue
            try:
                amount = Decimal(str(value))
            except (InvalidOperation, ValueError):
                raise SystemExit(f"Invalid catalog: non-numeric {field} rate for {provider_name}/{model_name}")
            if not amount.is_finite() or amount < 0:
                raise SystemExit(f"Invalid catalog: invalid {field} rate for {provider_name}/{model_name}")
        if any(field in rates for field in ("input", "output")):
            priced_count += 1

if not model_count or not priced_count:
    raise SystemExit("Invalid catalog: no model pricing rates found")
print(f"Validated catalog: {len(providers)} providers, {model_count} models, {priced_count} with pricing")
PY

chmod 0644 "$CATALOG_TMP"
mv -f "$CATALOG_TMP" "$OUTPUT_FILE"
echo "Updated $OUTPUT_FILE"
