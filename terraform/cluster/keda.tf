# ── KEDA (Kubernetes Event-Driven Autoscaling) ────────────────────────────────
resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = "2.14.1"
  namespace        = "keda"
  create_namespace = true

  set {
    name  = "resources.operator.requests.cpu"
    value = "50m"
  }
  set {
    name  = "resources.operator.requests.memory"
    value = "64Mi"
  }
  set {
    name  = "resources.operator.limits.memory"
    value = "128Mi"
  }

  depends_on = [azurerm_kubernetes_cluster_node_pool.user]
}

# ── kube-prometheus-stack (Prometheus + Grafana, no AlertManager) ─────────────
resource "helm_release" "prometheus" {
  name             = "kube-prometheus-stack"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  version          = "58.6.0"
  namespace        = "monitoring"
  create_namespace = true

  values = [<<-EOT
    alertmanager:
      enabled: false

    grafana:
      adminPassword: "arena-grafana"
      resources:
        requests:
          cpu: "50m"
          memory: "128Mi"
        limits:
          memory: "256Mi"

    prometheus:
      prometheusSpec:
        retention: 6h
        # Pick up ServiceMonitors from all namespaces (required for target-app scrape)
        serviceMonitorSelectorNilUsesHelmValues: false
        podMonitorSelectorNilUsesHelmValues: false
        resources:
          requests:
            cpu: "100m"
            memory: "256Mi"
          limits:
            cpu: "500m"
            memory: "512Mi"

    nodeExporter:
      enabled: true

    kubeStateMetrics:
      enabled: true
  EOT
  ]

  depends_on = [azurerm_kubernetes_cluster_node_pool.user]
}
