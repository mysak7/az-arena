# Fault Matrix — az-arena

Záměrná slabá místa infrastruktury. Každá chyba má `FAULT_XXX` tag přímo v kódu/manifestu.

Najdeš je rychle přes:
```bash
grep -r "FAULT_" k8s/ src/ --include="*.yaml" --include="*.py" -l
```

| ID | Název | Soubor | Útok | Obtížnost |
|----|-------|--------|------|-----------|
| FAULT_001 | OOM Kill | `k8s/target-app/deployment.yaml` | `POST /leak` | ⭐ easy |
| FAULT_002 | DB Connection Exhaustion | `k8s/postgresql/deployment.yaml` | `POST /lock-db` | ⭐⭐ medium |
| FAULT_003 | Network Blackout | `k8s/target-app/networkpolicy.yaml` | Apply deny policy | ⭐ easy |
| FAULT_004 | Autoscaler Deadlock | `k8s/keda/scaled-objects.yaml` | `POST /lock-db` + watch KEDA logs | ⭐⭐⭐ hard |
| FAULT_005 | BlobFuse2 Dangling Lease | `k8s/target-app/pvc-blobfuse.yaml` | Node deletion | ⭐⭐ medium |
| FAULT_006 | No PodDisruptionBudget | `k8s/target-app/deployment.yaml` | `kubectl drain <node>` | ⭐⭐ medium |

---

## FAULT_001 — OOM Kill

**Root cause**: `memory limit: 100Mi` na target-app je záměrně nízký.  
Endpoint `/leak` alokuje 200 MB najednou — K8s pod okamžitě zabije (Exit 137).

**Symptom**:
```
kubectl get pods -n target
# target-app-xxx   0/1   OOMKilled   3   5m
```

**Oprava**: Zvýšit `memory limit` na ≥ 256Mi, nebo přidat VPA (`VerticalPodAutoscaler`),
nebo opravit `/leak` aby alokoval postupně a nepřekračoval limit.

**Lokace**: `k8s/target-app/deployment.yaml` → hledej `# FAULT_001`

---

## FAULT_002 — DB Connection Exhaustion

**Root cause**: PostgreSQL běží s `max_connections=50` a bez connection pooleru (žádný PgBouncer).
Endpoint `/lock-db?count=50` otevře 50 idle spojení přes `pg_sleep(300)`.

**Symptom**:
```
FATAL: sorry, too many clients already
# Všechny nové dotazy (včetně KEDA, readiness probe) failují
```

**Oprava**: Přidat PgBouncer jako Service před PostgreSQL (`pool_mode=transaction`,
`max_client_conn=200`, `default_pool_size=10`). Nebo zvýšit `max_connections` + přidat
connection pooling v aplikaci (asyncpg Pool).

**Lokace**: `k8s/postgresql/deployment.yaml` → hledej `# FAULT_002`

---

## FAULT_003 — Network Blackout

**Root cause**: `default-deny-all` NetworkPolicy existuje v manifestech — stačí ji aplikovat
na namespace `target` a veškerý provoz (včetně ingress-nginx → target-app) se zastaví.

**Útok**:
```bash
# Breaker ji aplikuje:
kubectl apply -f k8s/target-app/networkpolicy.yaml
# nebo odstraní allow-ingress-nginx policy
kubectl delete networkpolicy allow-ingress-nginx -n target
```

**Symptom**: nginx vrací 503, kubectl exec do podu nefunguje.

**Oprava**: Obnovit `allow-ingress-nginx` NetworkPolicy, nebo zkontrolovat
`kubectl get networkpolicy -n target` a odstranit blokující pravidla.

**Lokace**: `k8s/target-app/networkpolicy.yaml` → hledej `# FAULT_003`

---

## FAULT_004 — Autoscaler Deadlock

**Root cause**: KEDA ScaledObject pro `queue-worker` dotazuje přímo produkční PostgreSQL
(stejné credentials jako aplikace). Pokud `/lock-db` vyčerpá `max_connections=50`,
KEDA nemůže spustit trigger query → autoscaler zamrzne.

**Symptom**:
```bash
kubectl logs -n keda deploy/keda-operator | grep "context deadline exceeded"
# queue-worker se nenaškáluje, i když fronta roste přes POST /flood-queue
```

**Cascade efekt**: lock-db attack → PG full → KEDA freeze → queue-worker stays at 0 replicas → jobs pile up

**Oprava**:
1. Přidat PgBouncer s dedikovaným `keda` userem (`pool_mode=session`, max 2 connections)
2. Nebo přepnout KEDA trigger na Prometheus scaler (nezávisí na DB)
3. Nebo zvýšit `max_connections` a reservovat část pro systémové queries

**Lokace**: `k8s/keda/scaled-objects.yaml` → hledej `# FAULT_004`

---

## FAULT_005 — BlobFuse2 Dangling Lease

**Root cause**: BlobFuse2 PV je nakonfigurovaný bez explicit lease break timeoutu a bez
`--use-adls`. Pokud VMSS node odpadne se stále připojeným BlobFuse2 volume, Azure Storage
drží stale lease na blob container.

**Útok**: Delete/cordon node s běžícím target-app:
```bash
# Simulace: kubectl delete node <node-name>
# nebo přes Azure: az vmss delete-instances --ids ...
```

**Symptom**:
```
kubectl describe pod target-app-xxx -n target
# Events: MountVolume.MountDevice failed: "failed to acquire blob lease"
# Pod stuck v ContainerCreating
```

**Oprava**: Přidat BlobFuse2 mount options do PV spec:
```yaml
mountOptions:
  - --lease-duration=15
  - --cancel-list-on-mount-failure=true
```
Nebo přidat `blobfuse2-cleanup` DaemonSet který proaktivně uvolňuje stale lease.

**Lokace**: `k8s/target-app/pvc-blobfuse.yaml` → hledej `# FAULT_005`

---

## FAULT_006 — No PodDisruptionBudget

**Root cause**: target-app nemá `PodDisruptionBudget`. Při `kubectl drain` nebo AKS node pool
upgrade může K8s evictovat všechny repliky najednou → krátkodobý výpadek.

**Útok**:
```bash
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
# Pokud obě repliky target-app sedí na stejném nodu → oba pody evictovány najednou
```

**Symptom**: arena dashboard ukazuje target-app jako `unreachable` během drain operace.

**Oprava**: Přidat PDB:
```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: target-app-pdb
  namespace: target
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: target-app
```

**Lokace**: `k8s/target-app/deployment.yaml` → hledej `# FAULT_006`
