data "azurerm_client_config" "current" {}

# ── Resource Group ─────────────────────────────────────────────────────────────
resource "azurerm_resource_group" "main" {
  name     = "rg-${local.prefix}-${var.project}"
  location = var.location
  tags     = local.tags
}

# ── Storage Account (Terraform state + BlobFuse2 target) ──────────────────────
resource "azurerm_storage_account" "main" {
  name                     = "stdevchaosbattle"
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"

  blob_properties {
    versioning_enabled = false
  }

  tags = local.tags
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_name  = azurerm_storage_account.main.name
  container_access_type = "private"
}

# "Model weights" blob container — target for the Dangling Storage Lease attack
resource "azurerm_storage_container" "model_weights" {
  name                  = "model-weights"
  storage_account_name  = azurerm_storage_account.main.name
  container_access_type = "private"
}

# Dummy blob so the mount has something to serve
resource "azurerm_storage_blob" "dummy_weights" {
  name                   = "weights.bin"
  storage_account_name   = azurerm_storage_account.main.name
  storage_container_name = azurerm_storage_container.model_weights.name
  type                   = "Block"
  source_content         = "DUMMY_MODEL_WEIGHTS_v1"
}

# ── Container Registry ─────────────────────────────────────────────────────────
resource "azurerm_container_registry" "main" {
  name                = "acrdevchaosbattle"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

# ── Key Vault ──────────────────────────────────────────────────────────────────
resource "azurerm_key_vault" "main" {
  name                       = "kv-dev-euc1-chaos"
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  soft_delete_retention_days = 7
  purge_protection_enabled   = false

  # GitHub Actions / deployer gets full access
  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id

    secret_permissions = ["Get", "List", "Set", "Delete", "Purge", "Recover"]
  }

  tags = local.tags
}

# PostgreSQL password stored in Key Vault
resource "random_password" "postgres" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "azurerm_key_vault_secret" "postgres_password" {
  name         = "postgres-password"
  value        = random_password.postgres.result
  key_vault_id = azurerm_key_vault.main.id
}

# ── CosmosDB (Serverless NoSQL) ────────────────────────────────────────────────
resource "azurerm_cosmosdb_account" "main" {
  name                = "cosmos-dev-euc1-chaos-arena"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = azurerm_resource_group.main.location
    failover_priority = 0
  }

  capabilities {
    name = "EnableServerless"
  }

  tags = local.tags
}

resource "azurerm_cosmosdb_sql_database" "arena" {
  name                = var.cosmosdb_database_name
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
}

resource "azurerm_cosmosdb_sql_container" "battle_log" {
  name                = var.cosmosdb_container_name
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.arena.name
  partition_key_path  = "/battle_id"

  indexing_policy {
    indexing_mode = "consistent"

    included_path { path = "/*" }
    excluded_path { path = "/\"_etag\"/?" }
  }
}
