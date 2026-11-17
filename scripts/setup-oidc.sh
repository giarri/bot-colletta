#!/usr/bin/env bash
# One-time: let GitHub Actions deploy to Azure without stored credentials (OIDC federation).
# Usage: scripts/setup-oidc.sh [owner/repo] [resource-group]
# Needs `az login` as someone who can create app registrations and assign roles on the resource group.
set -euo pipefail
repo="${1:-giarri/bot-colletta}"
rg="${2:-rg-bot-colletta}"
name="gh-${repo//\//-}"

sub=$(az account show --query id -o tsv)
tenant=$(az account show --query tenantId -o tsv)
app=$(az ad app list --display-name "$name" --query '[0].appId' -o tsv)
[ -n "$app" ] || app=$(az ad app create --display-name "$name" --query appId -o tsv)
az ad sp show --id "$app" >/dev/null 2>&1 || az ad sp create --id "$app" >/dev/null
sp=$(az ad sp show --id "$app" --query id -o tsv)

fed() { # name subject
  az ad app federated-credential list --id "$app" --query "[?name=='$1'].name" -o tsv | grep -q . ||
    az ad app federated-credential create --id "$app" --parameters \
      "{\"name\":\"$1\",\"issuer\":\"https://token.actions.githubusercontent.com\",\"subject\":\"$2\",\"audiences\":[\"api://AzureADTokenExchange\"]}" >/dev/null
}
fed github-production "repo:$repo:environment:production"
fed github-pr "repo:$repo:pull_request"

scope="/subscriptions/$sub/resourceGroups/$rg"
# Contributor deploys the template and the app; RBAC Administrator lets the template create role assignments.
for role in Contributor "Role Based Access Control Administrator"; do
  az role assignment create --assignee-object-id "$sp" --assignee-principal-type ServicePrincipal \
    --role "$role" --scope "$scope" >/dev/null
done

cat <<OUT
Set these repository variables (Settings > Secrets and variables > Actions > Variables):
  AZURE_CLIENT_ID=$app
  AZURE_TENANT_ID=$tenant
  AZURE_SUBSCRIPTION_ID=$sub
Service principal object id (pass as deployerPrincipalId in infra/main.bicepparam): $sp
OUT
