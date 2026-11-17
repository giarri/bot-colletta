# Infrastructure (Bicep)

Everything the bot runs on, as code: Function App (Flex Consumption, Python 3.11, 512 MB), storage account
(Functions host, deployment package, table `botusers`), Key Vault (RBAC), Log Analytics + Application Insights, and
the role assignments that tie them together via managed identity. No shared keys, no secrets in the template.

```bash
az group create -n rg-bot-colletta -l italynorth
az deployment group what-if -g rg-bot-colletta -f infra/main.bicep -p infra/main.bicepparam
az deployment group create  -g rg-bot-colletta -f infra/main.bicep -p infra/main.bicepparam
```

## Secrets

The template creates the vault but not the secret values. After the first deploy, seed them once:

```bash
scripts/seed-secrets.sh kv-botcolletta-bfdc1a   # prompts for the Telegram token; generates cred-key and webhook-secret
```

Until `telegram-token`, `cred-key` and `webhook-secret` exist, the Key Vault references stay unresolved and the app
cannot start. For WhatsApp also add `wa-app-secret`, `wa-verify-token`, `wa-token`, then redeploy with
`whatsappEnabled = true` and `whatsappPhoneId`.

## Adopting the existing deployment

`main.bicepparam` pins `nameSuffix = 'bfdc1a'`, the suffix of the resources created by hand. Before the first real
deploy, run `what-if` and read it: Bicep converges them to this template (for example the storage account turns
shared-key access off, and the Function App gets a Flex deployment container). If the what-if shows anything
destructive, deploy to a fresh resource group with another suffix first and switch the webhook over.

## CI/CD

See `.github/workflows/`. Authentication is GitHub OIDC (no stored credentials): `scripts/setup-oidc.sh` creates the
Entra app registration, the federated credentials and the role assignments, and prints the three repository
variables to set (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`) plus the principal id to put in
`deployerPrincipalId`.
