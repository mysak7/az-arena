# az-arena

**Chaos Engineering Battle Arena on AKS** — tréninkový prostor pro chaos engineering a SRE recovery scénáře.
Live dashboard na `arena.mysak.fun`.

Infrastruktura je záměrně reálná, ale obsahuje **6 záměrných slabin** (viz [`FAULTS.md`](FAULTS.md)).
Cíl: naučit se je najít, pochopit root cause a opravit — pod tlakem živého útoku.

---

## Architecture

```
GitHub Actions
  ├── terraform-apply.yml    → provisions AKS + all resources
  ├── terraform-destroy.yml  → teardown (daily 20:00 UTC auto + manual)
  └── build-push.yml         → builds + pushes Docker images to ACR

AKS Cluster (aks-dev-euc1-arena, eastus)
  ├── namespace: target
  │   └── target-app (FastAPI) ← fault injection target
  │       ├── POST /leak           → 200 MB alloc → OOMKilled
  │       ├── POST /lock-db        → 50 idle PG connections → exhaustion
  │       ├── POST /flood-queue    → N pending jobs → triggers KEDA queue-worker scale
  │       ├── POST /reset          → emergency release
  │       └── GET  /health, /metrics
  │       └── BlobFuse2 mount (/mnt/weights) ← dangling lease attack target
  │
  ├── namespace: arena
  │   ├── arena-dashboard (FastAPI + HTMX + SSE) → arena.mysak.fun
  │   ├── postgresql (StatefulSet, postgres:16-alpine, max_connections=50)
  │   └── queue-worker (FastAPI + asyncpg) ← scales via KEDA (0–5 replicas)
  │
  ├── namespace: keda
  │   └── KEDA operator (v2.14) — manages ScaledObjects
  │       ├── queue-worker-scaler  → PostgreSQL trigger (arena_jobs pending count)
  │       └── target-app-scaler   → Prometheus trigger (HTTP RPS)
  │
  ├── namespace: monitoring
  │   ├── Prometheus (kube-prometheus-stack)
  │   └── Grafana (admin: arena-grafana)
  │
  └── namespace: ingress-nginx (nginx-ingress)

Azure (permanent, survives cluster teardown):
  ├── CosmosDB serverless    → battle-log container (partition: /battle_id)
  ├── Storage Account        → TF state + model-weights blob (BlobFuse2 target)
  ├── ACR                    → container images
  └── Key Vault              → postgres-password secret
```

---

## Fault Matrix

6 záměrných slabin — každá má `FAULT_XXX` tag v kódu. Viz **[FAULTS.md](FAULTS.md)**.

```bash
# Rychlé vyhledání tagů:
grep -r "FAULT_" k8s/ src/ --include="*.yaml" --include="*.py"
```

| ID | Název | Obtížnost |
|----|-------|-----------|
| FAULT_001 | OOM Kill (memory limit 100Mi) | ⭐ easy |
| FAULT_002 | DB Connection Exhaustion (no PgBouncer) | ⭐⭐ medium |
| FAULT_003 | Network Blackout (deny-all NetworkPolicy) | ⭐ easy |
| FAULT_004 | Autoscaler Deadlock (KEDA → same DB as attack) | ⭐⭐⭐ hard |
| FAULT_005 | BlobFuse2 Dangling Lease (no lease timeout) | ⭐⭐ medium |
| FAULT_006 | No PodDisruptionBudget (eviction during drain) | ⭐⭐ medium |

---

## Attack Matrix

| Scenario | How | K8s effect |
|----------|-----|-----------|
| `SCENARIO_OOM` | `POST /leak` on target-app | OOMKilled (Exit 137) |
| `SCENARIO_DB_EXHAUST` | `POST /lock-db` | PG FATAL: too many clients |
| `SCENARIO_NETWORK` | Apply NetworkPolicy blocking ingress | 503 from nginx |
| `SCENARIO_BLOB_LEASE` | Delete VMSS node with BlobFuse2 mounted | ContainerCreating stuck |
| `SCENARIO_SCALE_ZERO` | `kubectl scale deploy/target-app --replicas=0` | App down |
| `SCENARIO_QUEUE_FLOOD` | `POST /flood-queue?count=50` | KEDA scales queue-worker 0→5 |
| `SCENARIO_AUTOSCALER_DEADLOCK` | `POST /lock-db` + `POST /flood-queue` | KEDA freeze + queue pile-up |

Agents (Breaker + Healer) live **outside this repo** — they call this cluster's APIs remotely.

---

## Infrastructure

| Resource | Name | Notes |
|----------|------|-------|
| Resource Group | `rg-dev-euc1-arena` | All resources |
| AKS | `aks-dev-euc1-arena` | Azure CNI Overlay, NetworkPolicy |
| CosmosDB | `cosmos-dev-euc1-arena` | Serverless, battle-log |
| Storage Account | `stdevarena` | TF state + BlobFuse2 |
| ACR | `acrdevarena` | Images |
| Key Vault | `kv-dev-euc1-arena` | Secrets |
| UAMI target-app | `id-dev-target-app` | Storage Blob Data Contributor |
| UAMI dashboard | `id-dev-arena-dashboard` | CosmosDB Contributor |

Node pools:
- `system`: 2× Standard_B2s (fixed)
- `user`: 1–3× Standard_B4ms (autoscale) — workloads land here

---

## Workload Identity

| SA | Namespace | UAMI | Azure RBAC |
|----|-----------|------|------------|
| `target-app-sa` | `target` | `id-dev-target-app` | Storage Blob Data Contributor |
| `arena-dashboard-sa` | `arena` | `id-dev-arena-dashboard` | Cosmos DB Built-in Data Contributor |

`queue-worker-sa` — no Azure RBAC needed (only accesses in-cluster PostgreSQL).

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

# Watch KEDA scale queue-worker
kubectl get pods -n arena -w

# Trigger chaos scenarios
curl -X POST https://arena.mysak.fun/leak          # FAULT_001: OOM
curl -X POST https://arena.mysak.fun/lock-db       # FAULT_002: DB exhaustion
curl -X POST "https://arena.mysak.fun/flood-queue?count=50"  # SCENARIO_QUEUE_FLOOD
curl -X POST https://arena.mysak.fun/reset         # emergency reset

# Check KEDA logs (FAULT_004 visible here during lock-db attack)
kubectl logs -n keda deploy/keda-operator | grep -E "FAULT|deadlock|deadline exceeded"

# Open dashboard
open https://arena.mysak.fun

# Cluster auto-destroys at 20:00 UTC daily
# Manual destroy:
gh workflow run terraform-destroy.yml -f target=cluster -f confirm=DESTROY
```

---

## Terraform State

- Backend: Azure Blob Storage (`stdevarena` / `tfstate`)
- `base.tfstate` — CosmosDB, Storage, Key Vault, ACR
- `cluster.tfstate` — AKS, node pools, Workload Identity, nginx-ingress, KEDA, Prometheus

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

### queue-worker
| Var | Value |
|-----|-------|
| `POSTGRES_HOST` | `postgresql.arena.svc.cluster.local` |
| `POSTGRES_DB` | `arena` |
| `POSTGRES_USER` | `arena` |
| `POSTGRES_PASSWORD` | from K8s Secret `postgres-secret` |
| `WORKER_BATCH_SIZE` | `3` |
| `POLL_INTERVAL_SECONDS` | `2` |

---

## Monitoring & Incident Ticketing

**Design decisions (grilled 2026-05-27):**

| Decision | Choice | Důvod |
|----------|--------|-------|
| Healer typ | AI agent (Claude/LLM) | Autonomní — čte issue, fixuje, zavírá |
| Ticketing | GitHub Issues | Jednoduchá integrace, tracking v repo |
| Issue trigger | Breaker spouští scénář | Proaktivní, přesný scenario typ |
| Issue creator | Breaker volá GitHub API přímo | PAT v env Breakera |
| Issue obsah | Structured Markdown template | Healer snadno parsuje sekce |
| Data collector | `GET /api/incident-snapshot` | Dashboard sbírá pod stav + metriky |
| Issue lifecycle | Healer autonomně zavírá | Plně autonomní flow |
| Dashboard | Live incident panel | GitHub API polling, label `incident` |

### Flow

```
Breaker
  1. POST /leak (nebo jiný fault endpoint)
  2. GET arena.mysak.fun/api/incident-snapshot  → cluster state JSON
  3. GitHub API → vytvoří Issue (label: incident + fault-001..006)
       body: Markdown template (scenario / pod state / metrics / fix hint)

arena-dashboard
  GET /api/incident-snapshot  ← Kubernetes client (pods, events) + Prometheus
  GET /incidents-panel        ← HTML fragment se živými incidenty (HTMX polling 30s)

Healer (AI agent, external)
  1. Čte GitHub Issues (label: incident)
  2. Diagnostikuje + fixuje
  3. GitHub API → close issue
```

### Co je v tomto repozitáři

| Soubor | Co dělá |
|--------|---------|
| `src/arena-dashboard/main.py` | `/api/incident-snapshot` + `/incidents-panel` endpoints |
| `k8s/arena-dashboard/rbac.yaml` | ClusterRole pro čtení pods/events |
| `k8s/arena-dashboard/deployment.yaml` | Přidány env: `PROMETHEUS_URL`, `GITHUB_TOKEN`, `GITHUB_REPO` |
| `docs/incident-issue-template.md` | Markdown template pro Breakera |
| `scripts/create-github-labels.sh` | Vytvoří GitHub labels (incident, fault-001..006) |

### Env vars arena-dashboard

| Var | Hodnota |
|-----|---------|
| `PROMETHEUS_URL` | `http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090` |
| `GITHUB_TOKEN` | PAT s `issues:read` (K8s Secret `github-token`) |
| `GITHUB_REPO` | `mi/az-arena` (nebo správný org/repo) |

### GitHub labels

- `incident` — aktivní incident (otevřené issue = live fault)
- `fault-001` … `fault-006` — konkrétní fault typ
- `scenario-oom`, `scenario-db-exhaust`, … — scenario tagy

---

## Code Style

- Python 3.12, async-first (`asyncio` + `httpx`)
- FastAPI + Pydantic v2
- Ruff for formatting + linting
- `DefaultAzureCredential` for all Azure SDK calls (picks up Workload Identity)
- Terraform: `~> 3.110` azurerm provider
