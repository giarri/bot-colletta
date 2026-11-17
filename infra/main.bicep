// bot-colletta infrastructure: Function App (Flex Consumption), Table Storage, Key Vault, Application Insights.
// Scope: resource group.  az deployment group create -g rg-bot-colletta -f infra/main.bicep -p infra/main.bicepparam
// Secret VALUES are never deployed from here: seed them with scripts/seed-secrets.sh (see infra/README.md).

targetScope = 'resourceGroup'

@description('Azure region. westeurope rejects new resources on the owner subscription, so default to italynorth.')
param location string = 'italynorth'

@description('Base name used for every resource.')
param appName string = 'bot-colletta'

@description('Suffix that keeps global names unique. Pass bfdc1a to line up with the resources created by hand.')
param nameSuffix string = take(uniqueString(resourceGroup().id), 6)

@description('Base URL of the food-drive web app.')
param siteBase string = 'https://www.itud.eu/gnca'

@description('Days after which stored access codes are purged.')
param credTtlDays int = 7

@description('Memory per instance in MB (2048 or 4096 are the other valid sizes).')
@allowed([512, 2048, 4096])
param instanceMemoryMB int = 512

@description('Upper bound on instances, which also caps the cost of a traffic spike.')
param maximumInstanceCount int = 40

@description('Enable the WhatsApp channel: adds the WA_* app settings. The Key Vault secrets must exist first.')
param whatsappEnabled bool = false

@description('WhatsApp phone number id (not a secret). Only used when whatsappEnabled is true.')
param whatsappPhoneId string = ''

@description('Object id of the CI/CD principal. It gets Key Vault Secrets User so the deploy can refresh the Telegram command menu. Leave empty to skip.')
param deployerPrincipalId string = ''

var functionAppName = 'func-${appName}-${nameSuffix}'
var storageName = toLower('st${replace(appName, '-', '')}${nameSuffix}')
var keyVaultName = 'kv-${replace(appName, '-', '')}-${nameSuffix}'
var deploymentContainer = 'app-package'
var tableName = 'botusers'

// Built-in role definition ids
var roles = {
  blobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  queueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  tableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
}

func secretRef(vault string, secret string) string =>
  '@Microsoft.KeyVault(SecretUri=https://${vault}${environment().suffixes.keyvaultDns}/secrets/${secret}/)'

// ---------- Observability ----------

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${appName}-${nameSuffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: functionAppName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// ---------- Storage: Functions host + deployment package + the botusers table ----------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false // everything goes through managed identity
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainer
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource usersTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: tableName
}

// ---------- Key Vault (RBAC mode; secrets are seeded out of band) ----------

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
  }
}

// ---------- Function App on Flex Consumption ----------

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-${appName}-${nameSuffix}'
  location: location
  kind: 'functionapp'
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}

var baseSettings = {
  AzureWebJobsStorage__accountName: storage.name
  APPLICATIONINSIGHTS_CONNECTION_STRING: insights.properties.ConnectionString
  TABLES_ENDPOINT: storage.properties.primaryEndpoints.table
  SITE_BASE: siteBase
  CRED_TTL_DAYS: string(credTtlDays)
  TELEGRAM_TOKEN: secretRef(vault.name, 'telegram-token')
  CRED_KEY: secretRef(vault.name, 'cred-key')
  WEBHOOK_SECRET: secretRef(vault.name, 'webhook-secret')
}

var whatsappSettings = whatsappEnabled ? {
  WA_APP_SECRET: secretRef(vault.name, 'wa-app-secret')
  WA_VERIFY_TOKEN: secretRef(vault.name, 'wa-verify-token')
  WA_TOKEN: secretRef(vault.name, 'wa-token')
  WA_PHONE_ID: whatsappPhoneId
} : {}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [for s in items(union(baseSettings, whatsappSettings)): { name: s.key, value: s.value }]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        instanceMemoryMB: instanceMemoryMB
        maximumInstanceCount: maximumInstanceCount
      }
      runtime: { name: 'python', version: '3.11' }
    }
  }
  dependsOn: [packageContainer, usersTable]
}

// ---------- Role assignments ----------

var storageRoleIds = [
  roles.blobDataOwner // host lease blobs + deployment package
  roles.queueDataContributor
  roles.tableDataContributor // host bookkeeping + the botusers table
]

resource storageRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for r in storageRoleIds: {
  scope: storage
  name: guid(storage.id, functionApp.id, r)
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
  }
}]

resource appReadsSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, functionApp.id, roles.keyVaultSecretsUser)
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
  }
}

resource deployerReadsSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  scope: vault
  name: guid(vault.id, deployerPrincipalId, roles.keyVaultSecretsUser)
  properties: {
    principalId: deployerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
  }
}

output functionAppName string = functionApp.name
output functionAppHost string = functionApp.properties.defaultHostName
output keyVaultName string = vault.name
output storageAccountName string = storage.name
