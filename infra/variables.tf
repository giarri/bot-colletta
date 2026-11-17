variable "resource_group_name" {
  description = "Existing resource group that holds everything."
  type        = string
  default     = "rg-bot-colletta"
}

variable "location" {
  description = "Azure region. Must match the existing resources: changing it replaces them. westeurope rejects NEW resources on the owner subscription, so a fresh deployment needs another region (e.g. italynorth)."
  type        = string
  default     = "westeurope"
}

variable "app_name" {
  description = "Base name used for every resource."
  type        = string
  default     = "bot-colletta"
}

variable "name_suffix" {
  description = "Suffix that keeps global names unique. bfdc1a lines up with the resources created by hand."
  type        = string
  default     = "bfdc1a"

  validation {
    condition     = can(regex("^[a-z0-9]{3,8}$", var.name_suffix))
    error_message = "name_suffix must be 3-8 lowercase letters or digits."
  }
}

variable "site_base" {
  description = "Base URL of the food-drive web app."
  type        = string
  default     = "https://www.itud.eu/gnca"
}

variable "cred_ttl_days" {
  description = "Days after which stored access codes are purged."
  type        = number
  default     = 7
}

variable "instance_memory_mb" {
  description = "Memory per instance in MB."
  type        = number
  default     = 512

  validation {
    condition     = contains([512, 2048, 4096], var.instance_memory_mb)
    error_message = "instance_memory_mb must be 512, 2048 or 4096."
  }
}

variable "maximum_instance_count" {
  description = "Upper bound on instances, which also caps the cost of a traffic spike."
  type        = number
  default     = 40
}

variable "whatsapp_enabled" {
  description = "Enable the WhatsApp channel: adds the WA_* app settings. The Key Vault secrets must exist first."
  type        = bool
  default     = false
}

variable "whatsapp_phone_id" {
  description = "WhatsApp phone number id (not a secret). Only used when whatsapp_enabled is true."
  type        = string
  default     = ""
}

variable "deployer_principal_id" {
  description = "Object id of the CI/CD principal. It gets Key Vault Secrets User so the deploy can refresh the Telegram command menu. Empty skips it."
  type        = string
  default     = "ce27cc27-cc68-4b41-a502-f3bba1ae56e9" # printed by scripts/setup-oidc.sh
}
