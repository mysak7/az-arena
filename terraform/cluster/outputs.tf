output "cluster_name" {
  value = azurerm_kubernetes_cluster.main.name
}

output "oidc_issuer_url" {
  value = azurerm_kubernetes_cluster.main.oidc_issuer_url
}

output "kube_config_raw" {
  value     = azurerm_kubernetes_cluster.main.kube_config_raw
  sensitive = true
}

output "target_app_client_id" {
  value = azurerm_user_assigned_identity.target_app.client_id
}

output "arena_dashboard_client_id" {
  value = azurerm_user_assigned_identity.arena_dashboard.client_id
}

output "ingress_ip" {
  description = "nginx-ingress LoadBalancer IP — use for arena.mysak.fun DNS"
  value       = helm_release.nginx_ingress.status
}
