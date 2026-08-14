#!/usr/bin/env bash
# Decrypt the SOPS-encrypted secrets into a tmpfs file the containers read.
#
# /run is tmpfs on a systemd host, so the plaintext never touches persistent
# disk. Invoked by tradingagents-intraday.service as ExecStartPre; the matching
# ExecStopPost shreds the output.
#
# Requires: sops (age backend) on the host, and the age private key at
# $SOPS_AGE_KEY_FILE (default /etc/tradingagents/age.key, mode 0600).
set -euo pipefail

ENC_FILE="${ENC_FILE:-/opt/tradingagents/deploy/secrets/secrets.enc.env}"
SECRETS_DIR="${SECRETS_DIR:-/run/tradingagents}"
OUT_FILE="${OUT_FILE:-$SECRETS_DIR/secrets.env}"
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-/etc/tradingagents/age.key}"

if ! command -v sops >/dev/null 2>&1; then
  echo "decrypt-secrets: sops not found on PATH" >&2
  exit 1
fi
if [ ! -f "$ENC_FILE" ]; then
  echo "decrypt-secrets: encrypted secrets not found at $ENC_FILE" >&2
  exit 1
fi
if [ ! -f "$SOPS_AGE_KEY_FILE" ]; then
  echo "decrypt-secrets: age key not found at $SOPS_AGE_KEY_FILE" >&2
  exit 1
fi

install -d -m 0700 "$SECRETS_DIR"
umask 077
sops --decrypt --output "$OUT_FILE" "$ENC_FILE"
chmod 600 "$OUT_FILE"
echo "decrypt-secrets: wrote $OUT_FILE"
