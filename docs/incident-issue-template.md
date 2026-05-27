# Incident Issue Template

Toto je šablona pro GitHub Issues vytvářené Breakerem při spuštění fault scénáře.

**Použití**: Breaker zavolá `GET arena.mysak.fun/api/incident-snapshot`, dostane JSON,
a vytvoří GitHub Issue v tomto repozitáři s tímto formátem.

**GitHub labels**: `incident` + `fault-XXX` (podle aktivního faultu) + `scenario-YYY`

---

## Template (Markdown body)

```markdown
## 🔴 Incident: <SCENARIO_NAME>

**Triggered at**: <ISO timestamp>
**Fault type**: <FAULT_001 … FAULT_006>
**Attack vector**: <POST /leak | POST /lock-db | apply NetworkPolicy | …>

---

## 🖥️ Pod State

| Namespace | Pod | Phase | Reason | Restarts | Ready |
|-----------|-----|-------|--------|----------|-------|
| target | target-app-xxx | Failed | OOMKilled | 3 | false |
| arena  | queue-worker-yyy | Running | — | 0 | true |

_(copied from /api/incident-snapshot → pods)_

---

## 📊 Prometheus Metrics

| Metric | Value |
|--------|-------|
| target_memory_mb | 95.2 |
| pg_connections | 48 |
| http_5xx_rate_per_sec | 0.12 |
| keda_queue_depth | 25 |

_(null = metric not available / Prometheus unreachable)_

---

## ⚠️ Recent K8s Warning Events

| Namespace | Object | Reason | Message | Count |
|-----------|--------|--------|---------|-------|
| target | target-app-xxx | OOMKilling | Memory limit exceeded | 1 |
| arena  | postgresql-0   | BackOff    | Back-off restarting failed container | 3 |

---

## 🔧 Fix Hint

<!-- Copied from FAULTS.md for the active fault -->

### FAULT_001 — OOM Kill
Zvýšit `memory limit` na ≥ 256Mi v `k8s/target-app/deployment.yaml`, nebo přidat VPA.

### FAULT_002 — DB Connection Exhaustion
Přidat PgBouncer jako Service před PostgreSQL (`pool_mode=transaction`, `max_client_conn=200`).

### FAULT_003 — Network Blackout
Obnovit `allow-ingress-nginx` NetworkPolicy:
`kubectl apply -f k8s/target-app/networkpolicy.yaml`

### FAULT_004 — Autoscaler Deadlock
Přepnout KEDA trigger na Prometheus scaler nebo přidat PgBouncer s dedikovaným `keda` userem.

### FAULT_005 — BlobFuse2 Dangling Lease
Přidat mount options `--lease-duration=15 --cancel-list-on-mount-failure=true` do PV spec.

### FAULT_006 — No PodDisruptionBudget
Přidat PDB s `minAvailable: 1` pro target-app.

---

## ✅ Resolution Checklist

- [ ] Root cause identified
- [ ] Fix applied (kubectl / terraform / code change)
- [ ] Service health restored (`/health` returns 200)
- [ ] PR opened with permanent fix
- [ ] Issue closed by Healer
```

---

## API Reference

### `GET /api/incident-snapshot`

Volej z Breakera **po** spuštění útoku. Vrací:

```json
{
  "timestamp": "2026-05-27T10:30:00+00:00",
  "pods": [
    {
      "namespace": "target",
      "name": "target-app-abc123",
      "phase": "Running",
      "reason": "OOMKilled",
      "restarts": 3,
      "ready": false
    }
  ],
  "events": [
    {
      "namespace": "target",
      "name": "target-app-abc123",
      "reason": "OOMKilling",
      "message": "Memory limit of container exceeded",
      "count": 1,
      "timestamp": "2026-05-27T10:29:55+00:00"
    }
  ],
  "metrics": {
    "target_memory_mb": 95.2,
    "pg_connections": 48,
    "http_5xx_rate_per_sec": 0.12,
    "keda_queue_depth": 25
  }
}
```

### GitHub Issue creation

```python
import httpx

def create_incident_issue(token: str, repo: str, title: str, body: str, labels: list[str]):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    r = httpx.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers=headers,
        json={"title": title, "body": body, "labels": labels},
    )
    r.raise_for_status()
    return r.json()["number"]  # issue number for later close

def close_incident_issue(token: str, repo: str, issue_number: int, comment: str = ""):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if comment:
        httpx.post(
            f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments",
            headers=headers,
            json={"body": comment},
        )
    httpx.patch(
        f"https://api.github.com/repos/{repo}/issues/{issue_number}",
        headers=headers,
        json={"state": "closed"},
    )
```

### Labels

| Label | Barva | Použití |
|-------|-------|---------|
| `incident` | `#e11d48` (červená) | Všechny aktivní incidenty |
| `fault-001` | `#f97316` (oranžová) | OOM Kill |
| `fault-002` | `#eab308` (žlutá) | DB Connection Exhaustion |
| `fault-003` | `#ef4444` (červená) | Network Blackout |
| `fault-004` | `#a855f7` (fialová) | Autoscaler Deadlock |
| `fault-005` | `#3b82f6` (modrá) | BlobFuse2 Dangling Lease |
| `fault-006` | `#06b6d4` (cyan) | No PodDisruptionBudget |

Vytvoř labely přes: `./scripts/create-github-labels.sh`
