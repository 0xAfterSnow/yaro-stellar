#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${SOURCE_ACCOUNT:?Set SOURCE_ACCOUNT to your funded Stellar CLI identity}"
: "${SPONSOR_ADDRESS:?Set SPONSOR_ADDRESS to the sponsor public address}"
: "${ELDER_ADDRESS:?Set ELDER_ADDRESS to the elder public address}"
: "${OFFRAMP_RAIL:?Set OFFRAMP_RAIL to the settlement public address}"
: "${DAILY_LIMIT:?Set DAILY_LIMIT in seven-decimal USDC base units}"
USDC_TOKEN="${USDC_TOKEN:-CBIELTK6YBZJU5UP2WWQEUCYKLPU6AUNZ2BQ4WWFEIE3USCIHMXQDAMA}"

if ! [[ "$DAILY_LIMIT" =~ ^[1-9][0-9]*$ ]]; then
  echo 'DAILY_LIMIT must be a positive integer in USDC base units.' >&2
  exit 1
fi
command -v stellar >/dev/null || { echo 'Install stellar-cli first.' >&2; exit 1; }
cargo test --workspace --locked
stellar contract build --locked
stellar contract deploy \
  --wasm target/wasm32v1-none/release/yaro_vault.wasm \
  --source-account "$SOURCE_ACCOUNT" \
  --network testnet \
  --alias "${VAULT_ALIAS:-yaro-vault}" \
  -- \
  --sponsor "$SPONSOR_ADDRESS" \
  --elder "$ELDER_ADDRESS" \
  --usdc_token "$USDC_TOKEN" \
  --offramp_rail "$OFFRAMP_RAIL" \
  --daily_limit "$DAILY_LIMIT"
