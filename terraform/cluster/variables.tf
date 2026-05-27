variable "location" {
  type    = string
  default = "eastus"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "project" {
  type    = string
  default = "arena"
}

# Pulled from base remote state — can override for local testing
variable "resource_group_name" {
  type    = string
  default = "rg-dev-euc1-arena"
}

variable "acr_id" {
  type        = string
  description = "ACR resource ID — from base outputs"
  default     = ""
}

variable "key_vault_id" {
  type        = string
  description = "Key Vault resource ID — from base outputs"
  default     = ""
}

variable "storage_account_name" {
  type    = string
  default = "stdevarena"
}

variable "cosmosdb_endpoint" {
  type    = string
  default = ""
}

variable "cosmosdb_database_name" {
  type    = string
  default = "arena"
}

variable "cosmosdb_container_name" {
  type    = string
  default = "battle-log"
}

variable "model_weights_container_name" {
  type    = string
  default = "model-weights"
}

locals {
  prefix = "${var.environment}-euc1"
  tags = {
    environment = var.environment
    project     = var.project
    managed_by  = "terraform"
  }
}
