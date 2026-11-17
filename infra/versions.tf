terraform {
  required_version = ">= 1.6"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.20"
    }
  }

  # Partial configuration: the storage account is passed at init time (see README.md and the workflows).
  backend "azurerm" {
    resource_group_name = "rg-bot-colletta"
    container_name      = "tfstate"
    key                 = "bot-colletta.tfstate"
    use_azuread_auth    = true
  }
}

provider "azurerm" {
  features {}
  storage_use_azuread = true # the storage account has shared keys disabled
}
