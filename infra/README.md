# Infrastructure (Terraform)

Everything the bot runs on, as code: Function App (Flex Consumption, Python 3.11, 512 MB), storage account
(Functions host, deployment package, table `botusers`), Key Vault (RBAC), Log Analytics + Application Insights, and
the role assignments that tie them together via managed identity. No shared keys, no secrets in the code.

The resource group `rg-bot-colletta` is not managed here; it must exist (`scripts/bootstrap-tfstate.sh` creates it if missing).
The default `location` is `westeurope`, where the existing resources live. If Azure rejects new resources there (as it
does on the owner subscription), pass another region to the bootstrap script for the state account, e.g.
`scripts/bootstrap-tfstate.sh rg-bot-colletta italynorth`; the app resources stay put because they are imported.
State lives in an Azure Storage container (`tfstate`), accessed with Entra ID only.

## First-time setup

```bash
scripts/bootstrap-tfstate.sh                          # state storage account; prints TF_STATE_STORAGE
terraform -chdir=infra init -backend-config="storage_account_name=<TF_STATE_STORAGE>"
terraform -chdir=infra plan
terraform -chdir=infra apply
```

Commit the generated `infra/.terraform.lock.hcl`. Requires Terraform >= 1.6 and `az login`.

## Secrets

Terraform creates the vault but not the secret values. After the first apply, seed them once:

```bash
scripts/seed-secrets.sh kv-botcolletta-bfdc1a   # prompts for the Telegram token; generates cred-key and webhook-secret
```

Until `telegram-token`, `cred-key` and `webhook-secret` exist, the Key Vault references stay unresolved and the app
cannot start. For WhatsApp also add `wa-app-secret`, `wa-verify-token`, `wa-token`, then apply with
`-var whatsapp_enabled=true -var whatsapp_phone_id=...`.

## Adopting the existing deployment

`name_suffix` defaults to `bfdc1a`, the suffix of the resources created by hand. Import them into the state before the
first apply, then read the plan and check nothing is destroyed or replaced (the storage account turns shared-key
access off, and the Function App gets a Flex deployment container):

```bash
cd infra
sub=$(az account show --query id -o tsv); rg=rg-bot-colletta; s=bfdc1a
base=/subscriptions/$sub/resourceGroups/$rg/providers
terraform import azurerm_log_analytics_workspace.logs   $base/Microsoft.OperationalInsights/workspaces/log-bot-colletta-$s
terraform import azurerm_application_insights.insights  $base/Microsoft.Insights/components/func-bot-colletta-$s
terraform import azurerm_storage_account.storage        $base/Microsoft.Storage/storageAccounts/stbotcolletta$s
terraform import azurerm_key_vault.vault                $base/Microsoft.KeyVault/vaults/kv-botcolletta-$s
terraform import azurerm_service_plan.plan              $base/Microsoft.Web/serverfarms/plan-bot-colletta-$s
terraform import azurerm_function_app_flex_consumption.app $base/Microsoft.Web/sites/func-bot-colletta-$s
```

Anything that does not exist yet (container, table, role assignments) is simply created. If the plan wants to replace
something, deploy to a fresh resource group with another `name_suffix` first and switch the webhook over.

## CI/CD

See `.github/workflows/`. Authentication is GitHub OIDC (no stored credentials): `scripts/setup-oidc.sh` creates the
Entra app registration, the federated credentials and the role assignments, and prints the three repository
variables to set (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`) plus the principal id for
`deployer_principal_id`. Also set `TF_STATE_STORAGE` (from `bootstrap-tfstate.sh`; re-run it with
`CI_PRINCIPAL_ID=<that principal id>` so CI can read and write the state).
