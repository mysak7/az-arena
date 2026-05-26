# ── Workload Identity: target-app ─────────────────────────────────────────────
resource "azurerm_user_assigned_identity" "target_app" {
  name                = "id-dev-target-app"
  resource_group_name = var.resource_group_name
  location            = data.azurerm_resource_group.main.location
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "target_app" {
  name                = "target-app-federation"
  resource_group_name = var.resource_group_name
  parent_id           = azurerm_user_assigned_identity.target_app.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject             = "system:serviceaccount:target:target-app-sa"
}

# ── Workload Identity: arena-dashboard ────────────────────────────────────────
resource "azurerm_user_assigned_identity" "arena_dashboard" {
  name                = "id-dev-arena-dashboard"
  resource_group_name = var.resource_group_name
  location            = data.azurerm_resource_group.main.location
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "arena_dashboard" {
  name                = "arena-dashboard-federation"
  resource_group_name = var.resource_group_name
  parent_id           = azurerm_user_assigned_identity.arena_dashboard.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject             = "system:serviceaccount:arena:arena-dashboard-sa"
}

# ── Key Vault access policies for CSI driver ──────────────────────────────────
resource "azurerm_key_vault_access_policy" "target_app" {
  count        = var.key_vault_id != "" ? 1 : 0
  key_vault_id = var.key_vault_id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = azurerm_user_assigned_identity.target_app.principal_id

  secret_permissions = ["Get"]
}

resource "azurerm_key_vault_access_policy" "arena_dashboard" {
  count        = var.key_vault_id != "" ? 1 : 0
  key_vault_id = var.key_vault_id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = azurerm_user_assigned_identity.arena_dashboard.principal_id

  secret_permissions = ["Get"]
}

# AKS CSI driver managed identity also needs KV access
resource "azurerm_key_vault_access_policy" "aks_kv_csi" {
  count        = var.key_vault_id != "" ? 1 : 0
  key_vault_id = var.key_vault_id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = azurerm_kubernetes_cluster.main.key_vault_secrets_provider[0].secret_identity[0].object_id

  secret_permissions = ["Get"]
}
