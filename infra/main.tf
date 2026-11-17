# bot-colletta infrastructure: Function App (Flex Consumption), Table Storage, Key Vault, Application Insights.
# Secret VALUES are never managed here: seed them with scripts/seed-secrets.sh (see README.md).

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

data "azurerm_client_config" "current" {}

locals {
  function_app_name    = "func-${var.app_name}-${var.name_suffix}"
  storage_name         = lower("st${replace(var.app_name, "-", "")}${var.name_suffix}")
  key_vault_name       = "kv-${replace(var.app_name, "-", "")}-${var.name_suffix}"
  deployment_container = "app-package"
  table_name           = "botusers"

  secret_ref = { for s in ["telegram-token", "cred-key", "webhook-secret", "wa-app-secret", "wa-verify-token", "wa-token"] :
    s => "@Microsoft.KeyVault(SecretUri=https://${local.key_vault_name}.vault.azure.net/secrets/${s}/)"
  }

  base_settings = {
    AzureWebJobsStorage__accountName      = azurerm_storage_account.storage.name
    APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.insights.connection_string
    TABLES_ENDPOINT                       = azurerm_storage_account.storage.primary_table_endpoint
    SITE_BASE                             = var.site_base
    CRED_TTL_DAYS                         = tostring(var.cred_ttl_days)
    TELEGRAM_TOKEN                        = local.secret_ref["telegram-token"]
    CRED_KEY                              = local.secret_ref["cred-key"]
    WEBHOOK_SECRET                        = local.secret_ref["webhook-secret"]
  }

  whatsapp_settings = var.whatsapp_enabled ? {
    WA_APP_SECRET   = local.secret_ref["wa-app-secret"]
    WA_VERIFY_TOKEN = local.secret_ref["wa-verify-token"]
    WA_TOKEN        = local.secret_ref["wa-token"]
    WA_PHONE_ID     = var.whatsapp_phone_id
  } : {}
}

# ---------- Observability ----------

resource "azurerm_log_analytics_workspace" "logs" {
  name                = "log-${var.app_name}-${var.name_suffix}"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_application_insights" "insights" {
  name                = local.function_app_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  application_type    = "web"
  workspace_id        = azurerm_log_analytics_workspace.logs.id
}

# ---------- Storage: Functions host + deployment package + the botusers table ----------

resource "azurerm_storage_account" "storage" {
  name                            = local.storage_name
  location                        = var.location
  resource_group_name             = data.azurerm_resource_group.rg.name
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  account_kind                    = "StorageV2"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false # everything goes through managed identity
  https_traffic_only_enabled      = true
}

resource "azurerm_storage_container" "package" {
  name                  = local.deployment_container
  storage_account_id    = azurerm_storage_account.storage.id
  container_access_type = "private"
}

resource "azurerm_storage_table" "users" {
  name               = local.table_name
  storage_account_id = azurerm_storage_account.storage.id
}

# ---------- Key Vault (RBAC mode; secrets are seeded out of band) ----------

resource "azurerm_key_vault" "vault" {
  name                       = local.key_vault_name
  location                   = var.location
  resource_group_name        = data.azurerm_resource_group.rg.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  soft_delete_retention_days = 90
}

# ---------- Function App on Flex Consumption ----------

resource "azurerm_service_plan" "plan" {
  name                = "plan-${var.app_name}-${var.name_suffix}"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  os_type             = "Linux"
  sku_name            = "FC1"
}

resource "azurerm_function_app_flex_consumption" "app" {
  name                = local.function_app_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  service_plan_id     = azurerm_service_plan.plan.id

  storage_container_type      = "blobContainer"
  storage_container_endpoint  = "${azurerm_storage_account.storage.primary_blob_endpoint}${azurerm_storage_container.package.name}"
  storage_authentication_type = "SystemAssignedIdentity"

  runtime_name           = "python"
  runtime_version        = "3.11"
  instance_memory_in_mb  = var.instance_memory_mb
  maximum_instance_count = var.maximum_instance_count

  https_only = true

  identity {
    type = "SystemAssigned"
  }

  site_config {
    minimum_tls_version = "1.2"
  }

  app_settings = merge(local.base_settings, local.whatsapp_settings)

  depends_on = [azurerm_storage_table.users]
}

# ---------- Role assignments ----------

locals {
  storage_roles = {
    blob  = "Storage Blob Data Owner" # host lease blobs + deployment package
    queue = "Storage Queue Data Contributor"
    table = "Storage Table Data Contributor" # host bookkeeping + the botusers table
  }
}

resource "azurerm_role_assignment" "storage" {
  for_each             = local.storage_roles
  scope                = azurerm_storage_account.storage.id
  role_definition_name = each.value
  principal_id         = azurerm_function_app_flex_consumption.app.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "app_reads_secrets" {
  scope                = azurerm_key_vault.vault.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_function_app_flex_consumption.app.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "deployer_reads_secrets" {
  count                = var.deployer_principal_id == "" ? 0 : 1
  scope                = azurerm_key_vault.vault.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = var.deployer_principal_id
  principal_type       = "ServicePrincipal"
}
