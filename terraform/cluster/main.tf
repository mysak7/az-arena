data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "main" {
  name = var.resource_group_name
}

data "azurerm_storage_account" "main" {
  name                = var.storage_account_name
  resource_group_name = var.resource_group_name
}

# ── AKS Cluster ────────────────────────────────────────────────────────────────
resource "azurerm_kubernetes_cluster" "main" {
  name                = "aks-dev-euc1-${var.project}"
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name
  dns_prefix          = "chaos-arena"

  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  # System node pool — lean, always on
  default_node_pool {
    name                 = "system"
    vm_size              = "Standard_B2s"
    node_count           = 2
    os_disk_size_gb      = 30
    type                 = "VirtualMachineScaleSets"
    only_critical_addons_enabled = true

    upgrade_settings {
      max_surge = "10%"
    }
  }

  # Azure CNI Overlay — supports NetworkPolicy without VNet IP exhaustion
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_policy      = "azure"
    pod_cidr            = "192.168.0.0/16"
    service_cidr        = "10.96.0.0/16"
    dns_service_ip      = "10.96.0.10"
    load_balancer_sku   = "standard"
  }

  identity {
    type = "SystemAssigned"
  }

  # Key Vault CSI driver for secret injection
  key_vault_secrets_provider {
    secret_rotation_enabled  = true
    secret_rotation_interval = "2m"
  }

  # Azure Blob CSI for BlobFuse2 (Dangling Storage Lease attack)
  storage_profile {
    blob_driver_enabled         = true
    disk_driver_enabled         = true
    file_driver_enabled         = false
    snapshot_controller_enabled = false
  }

  tags = local.tags
}

# ── User Node Pool (target-app + dashboard + postgresql workloads) ─────────────
resource "azurerm_kubernetes_cluster_node_pool" "user" {
  name                  = "user"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.main.id
  vm_size               = "Standard_B4ms"
  node_count            = 1
  min_count             = 1
  max_count             = 3
  enable_auto_scaling   = true
  os_disk_size_gb       = 50

  node_labels = {
    "arena/role" = "workload"
  }

  upgrade_settings {
    max_surge = "1"
  }

  tags = local.tags
}

# ── ACR pull permission for AKS kubelet identity ───────────────────────────────
resource "azurerm_role_assignment" "aks_acr_pull" {
  count                = var.acr_id != "" ? 1 : 0
  scope                = var.acr_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id
}

# ── Storage access for target-app BlobFuse2 mount ─────────────────────────────
resource "azurerm_role_assignment" "target_app_blob" {
  scope                = data.azurerm_storage_account.main.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.target_app.principal_id
}

# ── CosmosDB access for arena-dashboard ───────────────────────────────────────
resource "azurerm_cosmosdb_sql_role_assignment" "dashboard_reader" {
  count               = var.cosmosdb_endpoint != "" ? 1 : 0
  resource_group_name = var.resource_group_name
  account_name        = "cosmos-dev-euc1-chaos-arena"
  role_definition_id  = "/subscriptions/${data.azurerm_client_config.current.subscription_id}/resourceGroups/${var.resource_group_name}/providers/Microsoft.DocumentDB/databaseAccounts/cosmos-dev-euc1-chaos-arena/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = azurerm_user_assigned_identity.arena_dashboard.principal_id
  scope               = "/"
}

# ── nginx-ingress (via Helm) ──────────────────────────────────────────────────
resource "helm_release" "nginx_ingress" {
  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  version          = "4.10.1"
  namespace        = "ingress-nginx"
  create_namespace = true

  set {
    name  = "controller.service.annotations.service\\.beta\\.kubernetes\\.io/azure-load-balancer-health-probe-request-path"
    value = "/healthz"
  }

  depends_on = [azurerm_kubernetes_cluster_node_pool.user]
}

# ── Namespaces ─────────────────────────────────────────────────────────────────
resource "kubernetes_namespace" "target" {
  metadata {
    name   = "target"
    labels = { "arena/role" = "target" }
  }
  depends_on = [azurerm_kubernetes_cluster.main]
}

resource "kubernetes_namespace" "arena" {
  metadata {
    name   = "arena"
    labels = { "arena/role" = "control" }
  }
  depends_on = [azurerm_kubernetes_cluster.main]
}
