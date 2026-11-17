#!/usr/bin/env bash
# Seed the Key Vault secrets the app needs. Usage: scripts/seed-secrets.sh <vault-name>
# Existing secrets are left alone, so it is safe to re-run. Needs "Key Vault Secrets Officer" on the vault.
set -euo pipefail
vault="${1:?usage: $0 <vault-name>}"

exists() { az keyvault secret show --vault-name "$vault" -n "$1" --query id -o tsv >/dev/null 2>&1; }
put() { az keyvault secret set --vault-name "$vault" -n "$1" --file /dev/stdin --output none; echo "set $1"; }

if exists telegram-token; then echo "telegram-token already set"; else
  read -rsp "Telegram bot token: " token; echo
  printf %s "$token" | put telegram-token
fi
if exists cred-key; then echo "cred-key already set"; else
  # Fernet key: 32 url-safe base64 bytes
  python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode(),end="")' | put cred-key
fi
if exists webhook-secret; then echo "webhook-secret already set"; else
  python3 -c 'import secrets;print(secrets.token_urlsafe(32),end="")' | put webhook-secret
fi
echo "Next: register the webhook (setWebhook with secret_token = the webhook-secret value)."
