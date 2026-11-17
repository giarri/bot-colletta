#!/usr/bin/env bash
# One-time: create the storage account that holds the Terraform state, and let the CI principal use it.
# Usage: scripts/bootstrap-tfstate.sh [resource-group] [location]   (location defaults to the resource group's)
# Needs `az login` as someone who can create storage accounts and assign roles on the resource group.
# Prints the storage account name to put in the TF_STATE_STORAGE repository variable.
set -euo pipefail
rg="${1:-rg-bot-colletta}"
location="${2:-}"

sub=$(az account show --query id -o tsv)
me=$(az ad signed-in-user show --query id -o tsv)
sa="sttfstate$(printf %s "$sub" | sha256sum | cut -c1-8)"

if az group show -n "$rg" >/dev/null 2>&1; then
  location="${location:-$(az group show -n "$rg" --query location -o tsv)}"
else
  location="${location:-italynorth}"
  az group create -n "$rg" -l "$location" --output none
fi
az storage account show -g "$rg" -n "$sa" --query id -o tsv >/dev/null 2>&1 ||
  az storage account create -g "$rg" -n "$sa" -l "$location" --sku Standard_LRS --min-tls-version TLS1_2 \
    --allow-blob-public-access false --allow-shared-key-access false --output none
id=$(az storage account show -g "$rg" -n "$sa" --query id -o tsv)

# Blob data access for the person bootstrapping and for the CI principal (app id from scripts/setup-oidc.sh).
role() { az role assignment create --assignee-object-id "$1" --assignee-principal-type "$2" \
  --role "Storage Blob Data Contributor" --scope "$id" --output none; }
role "$me" User
if [ -n "${CI_PRINCIPAL_ID:-}" ]; then role "$CI_PRINCIPAL_ID" ServicePrincipal; fi

# Role assignments can take a minute to propagate before the container can be created.
for _ in 1 2 3 4 5 6; do
  az storage container create --account-name "$sa" -n tfstate --auth-mode login --output none 2>/dev/null && break
  sleep 15
done
az storage container show --account-name "$sa" -n tfstate --auth-mode login --query name -o tsv >/dev/null

cat <<OUT
State container ready. Set this repository variable:
  TF_STATE_STORAGE=$sa
Local use: terraform -chdir=infra init -backend-config="storage_account_name=$sa"
(Re-run with CI_PRINCIPAL_ID=<service principal object id> to grant the CI principal access.)
OUT
