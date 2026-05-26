# az-chaos-arena

**Chaos Engineering Battle Arena on AKS** — two autonomous AI agents fight each other: a Breaker (red team) that injects faults and a Healer (blue team SRE) that diagnoses and remediates them. All actions are logged to CosmosDB and streamed live to an HTMX dashboard.

---

## 1. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  AKS Cluster (az-chaos-arena)                                │
│                                                              │
│  chaos-system ns          sre-system ns                      │
│  ┌─────────────┐          ┌─────────────┐                    │
│  │ chaos-agent │  attack  │  sre-agent  │  remediate         │
│  │ (Breaker)   │ ──────►  │  (Healer)   │ ──────────────►   │
│  └─────────────┘          └─────────────┘   default ns       │
│        │                        │           ┌────────────┐   │
│        │ log                    │ log       │ target-app │   │
│        ▼                        ▼           │  FastAPI   │   │
│  ┌──────────────────────────────────────┐   │ /leak      │   │
│  │        CosmosDB (battle-log)         │   │ /lock-db   │   │
│  └──────────────────────────────────────┘   │ /health    │   │
│        │ read                              └────────────┘   │
│        ▼                                                      │
│  ┌─────────────────┐                                         │
│  │ arena-dashboard │ ← HTMX live stream                      │
│  │    FastAPI      │                                         │
│  └─────────────────┘                                         │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. Repository Structure

```
az-chaos-arena/
├── terraform/
│   ├── base/              # CosmosDB + Storage (permanent, always-on)
│   └── cluster/           # AKS + KEDA + Ingress (ephemeral)
├── k8s/
│   ├── chaos-agent/       # chaos-agent Deployment, SA, RBAC, WI federation
│   ├── sre-agent/         # sre-agent Deployment, SA, RBAC, WI federation
│   ├── target-app/        # target-app Deployment + Service + HPA
│   └── arena-dashboard/   # arena-dashboard Deployment + Ingress
├── src/
│   ├── chaos-agent/       # Python: Breaker agent loop
│   ├── sre-agent/         # Python: Healer agent loop
│   ├── target-app/        # Python: FastAPI with fault endpoints
│   └── arena-dashboard/   # Python: FastAPI + HTMX live dashboard
├── docker/
│   ├── chaos-agent.Dockerfile
│   ├── sre-agent.Dockerfile
│   ├── target-app.Dockerfile
│   └── arena-dashboard.Dockerfile
└── scripts/
    ├── bootstrap.sh       # Full cluster bring-up
    └── teardown.sh        # Destroy ephemeral cluster (keep base)
```

---

## 3. Azure Infrastructure

| Resource | Name convention | Notes |
|----------|-----------------|-------|
| Resource Group | `rg-dev-euc1-chaos-arena` | All resources land here |
| AKS Cluster | `aks-dev-euc1-chaos-arena` | Node Autoprovision (NAP) |
| CosmosDB | `cosmos-dev-euc1-chaos-arena` | Serverless, battle-log container |
| Storage Account | `stdevchaosbattle` | Chaos lease blobs |
| ACR | `acrdevchaosbattle` | Container images |
| Managed Identity (Breaker) | `id-dev-chaos-agent` | UAMI for chaos-agent |
| Managed Identity (Healer) | `id-dev-sre-agent` | UAMI for sre-agent |

**Region:** `eastus` (default; override via `var.location`)

---

## 4. Agent Tooling & Security Guardrails (Workload Identity)

Both agents run as Kubernetes pods and authenticate to Azure **passwordlessly** using **Azure AD Workload Identity** (OIDC federation). No secrets in manifests.

### A. Breaker Agent (`chaos-agent`)
- **K8s ServiceAccount**: `chaos-agent-sa` in `chaos-system` namespace
- **Azure Identity**: `id-dev-chaos-agent` (User-Assigned Managed Identity)
- **Azure RBAC**: `Contributor` at Resource Group level — may delete VMSS instances, break storage leases
- **K8s RBAC**: `cluster-admin` — may evict pods, scale deployments to 0, inject NetworkPolicies
- **CLI tools inside container**: `az`, `kubectl`

### B. Healer Agent (`sre-agent`)
- **K8s ServiceAccount**: `sre-agent-sa` in `sre-system` namespace
- **Azure Identity**: `id-dev-sre-agent` (User-Assigned Managed Identity)
- **Azure RBAC**: `Reader` at RG level + `Cosmos DB Data Contributor`
- **K8s RBAC**: Namespace-admin in `default` only — **cannot** touch `kube-system` or `chaos-system`
- **CLI tools inside container**: `az`, `kubectl`, `git`, `gh` (GitHub App token mounted as secret)

### Workload Identity Federation pattern

```hcl
# Terraform federation block (repeat for each SA/identity pair)
resource "azurerm_federated_identity_credential" "chaos_agent" {
  name                = "chaos-agent-federation"
  resource_group_name = azurerm_resource_group.main.name
  parent_id           = azurerm_user_assigned_identity.chaos_agent.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject             = "system:serviceaccount:chaos-system:chaos-agent-sa"
}
```

---

## 5. Development Milestones & Implementation Guide

### Phase 1: Permanent Base & Ephemeral Cluster Bootstrapping
1. Write `terraform/base/` — CosmosDB (serverless) + Storage Account
2. Write `terraform/cluster/` — AKS (NAP), nginx-ingress, KEDA, ACR attachment
3. Configure Workload Identity federations for both SAs

**Deploy order:**
```bash
cd terraform/base  && terraform apply
cd terraform/cluster && terraform apply
```

### Phase 2: Target App & Dashboard
1. `src/target-app/` — FastAPI with:
   - `GET /health` → `{"status": "ok", "mem_mb": <current>}`
   - `POST /leak` → starts a background coroutine allocating ~50 MB/s until OOM
   - `POST /lock-db` → opens SQLite connections without releasing them
2. `src/arena-dashboard/` — FastAPI + HTMX:
   - Reads CosmosDB `battle-log` container
   - `GET /stream` → SSE endpoint streaming new events as `<tr>` rows
   - Shows: timestamp, agent, scenario, action, result, PR link

### Phase 3: Breaker Agent (Red Team)
`src/chaos-agent/main.py` — async loop, runs a random scenario every 10 minutes:

| Scenario | Action |
|----------|--------|
| `SCENARIO_OOM` | `POST /leak` on target-app |
| `SCENARIO_EVICT` | `kubectl delete pod -l app=target-app --force` |
| `SCENARIO_SCALE` | `kubectl scale deploy/target-app --replicas=0` |

**Before every action:** log `{scenario, payload, timestamp, agent: "breaker"}` to CosmosDB.

### Phase 4: Healer Agent (Blue Team)
`src/sre-agent/main.py` — async loop polling `/health` + AKS events every 30 s:

| Detected condition | Remediation |
|-------------------|-------------|
| OOM / pod CrashLoopBackOff | Pull previous logs → determine current memory limits → clone config repo → patch HCL → `terraform validate` → push → `gh pr create` → log PR URL to CosmosDB |
| Replicas = 0 | `kubectl scale deploy/target-app --replicas=<target>` → log hotfix |
| General pod failure | `kubectl describe pod` + `kubectl logs --previous` → diagnose + log |

---

## 6. Code Style & Tooling Conventions

### Python
- **Async-first**: `asyncio` + `httpx` (no `requests`)
- **Type hints** everywhere; Pydantic v2 for all data models
- **Config**: 100% from Environment Variables (no hardcoded values)
- **Formatting**: `ruff format`, linting via `ruff check`
- **CosmosDB client**: `azure-cosmos` SDK with `DefaultAzureCredential` (picks up Workload Identity token automatically)

### Terraform
- **Naming**: `{resource_type}-{env}-{region}-{purpose}` (e.g., `aks-dev-euc1-chaos-arena`)
- **Secrets**: Never hardcoded — use Azure Key Vault or OIDC environment variables
- **State**: Azure Blob Storage backend (Storage Account from `base/`)
- **Modules**: `base/` and `cluster/` are independent; `cluster/` reads `base/` outputs via remote state

### Docker
- **Multi-arch**: build `linux/amd64` and `linux/arm64` in parallel via `docker buildx`
- **Non-root**: all containers run as UID 1000
- **Base image**: `python:3.12-slim` (agents), `python:3.12-slim` (apps)
- **Layer caching**: copy `requirements.txt` before source code

### Kubernetes
- All manifests use `apps/v1` Deployment with `strategy: RollingUpdate`
- Resource `requests` and `limits` always set
- `readinessProbe` and `livenessProbe` on every container
- Secrets only via Kubernetes Secrets mounted as env vars (never in Deployment spec directly)

---

## 7. Key Environment Variables

### chaos-agent
| Var | Purpose |
|-----|---------|
| `AZURE_CLIENT_ID` | UAMI client ID (injected by Workload Identity webhook) |
| `COSMOSDB_ENDPOINT` | CosmosDB endpoint URL |
| `COSMOSDB_DATABASE` | Database name (e.g., `arena`) |
| `COSMOSDB_CONTAINER` | Container name (e.g., `battle-log`) |
| `TARGET_APP_URL` | Internal ClusterIP URL of target-app |
| `INTERVAL_SECONDS` | Scenario interval (default: `600`) |

### sre-agent
| Var | Purpose |
|-----|---------|
| `AZURE_CLIENT_ID` | UAMI client ID |
| `COSMOSDB_ENDPOINT` | CosmosDB endpoint |
| `COSMOSDB_DATABASE` | Database name |
| `COSMOSDB_CONTAINER` | Container name |
| `TARGET_APP_URL` | Internal URL of target-app |
| `TARGET_REPLICAS` | Desired replica count to restore (default: `2`) |
| `CONFIG_REPO_URL` | Git repo with Terraform HCL (for PR-based remediation) |
| `GITHUB_TOKEN` | GitHub token for `gh pr create` (mounted from Secret) |
| `POLL_INTERVAL_SECONDS` | Health poll interval (default: `30`) |

---

## 8. Quick Start

```bash
# 1. Deploy permanent base (CosmosDB + Storage)
cd terraform/base
terraform init -backend-config=../../backend.hcl
terraform apply

# 2. Deploy AKS cluster
cd terraform/cluster
terraform init -backend-config=../../backend.hcl
terraform apply

# 3. Get kubeconfig
az aks get-credentials --resource-group rg-dev-euc1-chaos-arena \
  --name aks-dev-euc1-chaos-arena

# 4. Build & push images
cd docker
./build-all.sh   # uses docker buildx bake

# 5. Apply K8s manifests
kubectl apply -f k8s/

# 6. Watch the battle
kubectl logs -f -n chaos-system deploy/chaos-agent
kubectl logs -f -n sre-system   deploy/sre-agent
# or open arena-dashboard in browser
```

---

## 9. Teardown

```bash
# Destroy ephemeral cluster (keeps CosmosDB + Storage)
cd terraform/cluster && terraform destroy

# Full teardown (destroys everything including battle logs)
cd terraform/base    && terraform destroy
```
