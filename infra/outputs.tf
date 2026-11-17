output "function_app_name" {
  value = azurerm_function_app_flex_consumption.app.name
}

output "function_app_host" {
  value = azurerm_function_app_flex_consumption.app.default_hostname
}

output "key_vault_name" {
  value = azurerm_key_vault.vault.name
}

output "storage_account_name" {
  value = azurerm_storage_account.storage.name
}
