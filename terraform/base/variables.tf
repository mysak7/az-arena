variable "location" {
  description = "Azure region"
  type        = string
  default     = "eastus"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "dev"
}

variable "project" {
  description = "Project name"
  type        = string
  default     = "chaos-arena"
}

variable "cosmosdb_database_name" {
  description = "CosmosDB database name"
  type        = string
  default     = "arena"
}

variable "cosmosdb_container_name" {
  description = "CosmosDB battle-log container name"
  type        = string
  default     = "battle-log"
}

locals {
  prefix = "${var.environment}-euc1"
  tags = {
    environment = var.environment
    project     = var.project
    managed_by  = "terraform"
  }
}
