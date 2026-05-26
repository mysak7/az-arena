# az-chaos-arena

**Chaos Engineering Battle Arena on AKS** — infrastructure for fault injection and self-healing demos.
Live dashboard at `arena.mysak.fun`.

---

## Architecture

```
GitHub Actions
  ├── terraform-apply.yml    → provisions AKS + all resources
  ├── terraform-destroy.yml  → teardown (daily 20:00 UTC auto + manual)
  └── build-push.yml         → builds + pushes Docker images to ACR

AKS Cluster (aks-dev-euc1-chaos-arena, eastus)
  ├── namespace: target
  │   └── target-app (FastAPI) ← fault injection target
  │       ├── POST /leak        → 200 MB alloc → OOMKilled
  │       ├── POST /lock-db     → 50 idle PG connections → exhaustion
  │       ├── POST /reset       → emergency release
  │       └── GET  /health, /metrics
  │       └── BlobFuse2 mount (/mnt/weights) ← dangling lease attack target
  │
  ├── namespace: arena
  │   ├── arena-dashboard (FastAPI + HTMX + SSE) → arena.mysak.fun
  │   └── postgresql (StatefulSet, postgres:16-alpine)
  │
  └── namespace: ingress-nginx (nginx-ingress)

Azure (permanent, survives cluster teardown):
  ├── CosmosDB serverless    → battle-log container (partition: /battle_id)
  ├── Storage Account        → TF state + model-weights blob (BlobFuse2 target)
  ├── ACR                    → container images
  └── Key Vault              → postgres-password secret
```

---

## Attack Matrix

| Scenario | How | K8s effect |
|----------|-----|-----------|
| `SCENARIO_OOM` | `POST /leak` on target-app | OOMKilled (Exit 137) |
| `SCENARIO_DB_EXHAUST` | `POST /lock-db` | PG FATAL: too many clients |
| `SCENARIO_NETWORK` | Apply NetworkPolicy blocking ingress | 503 from nginx |
| `SCENARIO_BLOB_LEASE` | Delete VMSS node with BlobFuse2 mounted | ContainerCreating stuck |
| `SCENARIO_SCALE_ZERO` | `kubectl scale deploy/target-app --replicas=0` | App down |

Agents (Breaker + Healer) live **outside this repo** — they call this cluster's APIs remotely.

---

## Infrastructure

| Resource | Name | Notes |
|----------|------|-------|
| Resource Group | `rg-dev-euc1-chaos-arena` | All resources |
| AKS | `aks-dev-euc1-chaos-arena` | Azure CNI Overlay, NetworkPolicy |
| CosmosDB | `cosmos-dev-euc1-chaos-arena` | Serverless, battle-log |
| Storage Account | `stdevchaosbattle` | TF state + BlobFuse2 |
| ACR | `acrdevchaosbattle` | Images |
| Key Vault | `kv-dev-euc1-chaos` | Secrets |
| UAMI target-app | `id-dev-target-app` | Storage Blob Data Contributor |
| UAMI dashboard | `id-dev-arena-dashboard` | CosmosDB Contributor |

Node pools:
- `system`: 2× Standard_B2s (fixed)
- `user`: 1–3× Standard_B4ms (autoscale) — workloads land here

---

## Workload Identity

Both K8s ServiceAccounts are federated with their Azure Managed Identity:

| SA | Namespace | UAMI | Azure RBAC |
|----|-----------|------|------------|
| `target-app-sa` | `target` | `id-dev-target-app` | Storage Blob Data Contributor |
| `arena-dashboard-sa` | `arena` | `id-dev-arena-dashboard` | Cosmos DB Built-in Data Contributor |

---

## First Deploy (after cloning)

```bash
# 1. Login to Azure
az login

# 2. Bootstrap (one-time): creates storage account + OIDC federation + GitHub secrets
./scripts/bootstrap.sh

# 3. Push to main or trigger manually
git push origin main
# → GitHub Actions: terraform-apply.yml runs automatically

# 4. Get ingress IP from GH Actions logs and update DNS
# arena.mysak.fun A → <ingress IP>   (in dns-mysak-cloudflare repo)
```

---

## Daily Workflow

```bash
# Bring cluster up (manual)
gh workflow run terraform-apply.yml

# Watch the battle
kubectl logs -f -n arena deploy/arena-dashboard
kubectl logs -f -n target deploy/target-app

# Open dashboard
open https://arena.mysak.fun

# Cluster auto-destroys at 20:00 UTC daily
# Manual destroy:
gh workflow run terraform-destroy.yml -f target=cluster -f confirm=DESTROY
```

---

## Terraform State

- Backend: Azure Blob Storage (`stdevchaosbattle` / `tfstate`)
- `base.tfstate` — CosmosDB, Storage, Key Vault, ACR
- `cluster.tfstate` — AKS, node pools, Workload Identity, nginx-ingress

`base/` is permanent. `cluster/` is ephemeral (destroyed nightly).

---

## Key Environment Variables

### target-app
| Var | Value |
|-----|-------|
| `POSTGRES_HOST` | `postgresql.arena.svc.cluster.local` |
| `POSTGRES_DB` | `arena` |
| `POSTGRES_USER` | `arena` |
| `POSTGRES_PASSWORD` | from K8s Secret `postgres-secret` |
| `COSMOSDB_ENDPOINT` | from Terraform output |

### arena-dashboard
| Var | Value |
|-----|-------|
| `COSMOSDB_ENDPOINT` | from Terraform output |
| `COSMOSDB_DATABASE` | `arena` |
| `COSMOSDB_CONTAINER` | `battle-log` |
| `TARGET_APP_URL` | `http://target-app.target.svc.cluster.local` |
| `POLL_INTERVAL_SECONDS` | `3` |

---

## Code Style

- Python 3.12, async-first (`asyncio` + `httpx`)
- FastAPI + Pydantic v2
- Ruff for formatting + linting
- `DefaultAzureCredential` for all Azure SDK calls (picks up Workload Identity)
- Terraform: `~> 3.110` azurerm provider
