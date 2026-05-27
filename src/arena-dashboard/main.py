"""
arena-dashboard — az-arena Live Battle Dashboard
FastAPI + HTMX + Server-Sent Events

Routes:
  GET /                      — main HTML dashboard
  GET /stream                — SSE stream of new battle-log events (as <tr> rows)
  GET /health                — health check
  GET /status                — current target-app status (proxied)
  GET /api/incident-snapshot — cluster state JSON (for Breaker to include in GitHub Issue)
  GET /incidents-panel       — HTML fragment with live incidents (HTMX polling)
  GET /api/incidents         — raw JSON list of open GitHub Issues with label 'incident'
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

import httpx
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

try:
    from kubernetes import client as k8s_client, config as k8s_config
    _K8S_AVAILABLE = True
except ImportError:
    _K8S_AVAILABLE = False

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
COSMOSDB_ENDPOINT  = os.environ["COSMOSDB_ENDPOINT"]
COSMOSDB_DATABASE  = os.environ.get("COSMOSDB_DATABASE", "arena")
COSMOSDB_CONTAINER = os.environ.get("COSMOSDB_CONTAINER", "battle-log")
TARGET_APP_URL     = os.environ.get("TARGET_APP_URL", "http://target-app.target.svc.cluster.local")
POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL_SECONDS", "3"))
PROMETHEUS_URL     = os.environ.get(
    "PROMETHEUS_URL",
    "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090",
)
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO        = os.environ.get("GITHUB_REPO", "")   # e.g. "mi/az-arena"

# Namespaces to watch for cluster state
WATCH_NAMESPACES   = ["target", "arena", "keda"]

# ── App setup ──────────────────────────────────────────────────────────────────
_cosmos_client: CosmosClient | None = None
_credential: DefaultAzureCredential | None = None
_k8s_v1: "k8s_client.CoreV1Api | None" = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cosmos_client, _credential, _k8s_v1
    _credential = DefaultAzureCredential()
    _cosmos_client = CosmosClient(COSMOSDB_ENDPOINT, credential=_credential)
    log.info("arena-dashboard: CosmosDB client initialised")

    if _K8S_AVAILABLE:
        try:
            k8s_config.load_incluster_config()
            log.info("arena-dashboard: Kubernetes in-cluster config loaded")
        except Exception:
            try:
                k8s_config.load_kube_config()
                log.info("arena-dashboard: Kubernetes local kubeconfig loaded")
            except Exception as e:
                log.warning(f"arena-dashboard: Kubernetes client unavailable: {e}")
        if _K8S_AVAILABLE:
            _k8s_v1 = k8s_client.CoreV1Api()
    else:
        log.warning("arena-dashboard: kubernetes package not installed — /api/incident-snapshot will omit pod/event data")

    yield
    if _cosmos_client:
        await _cosmos_client.close()
    if _credential:
        await _credential.close()


app = FastAPI(title="arena-dashboard", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ── CosmosDB helpers ───────────────────────────────────────────────────────────
async def get_recent_events(limit: int = 50) -> list[dict]:
    """Fetch the most recent battle-log events from CosmosDB."""
    try:
        container = _cosmos_client.get_database_client(COSMOSDB_DATABASE).get_container_client(COSMOSDB_CONTAINER)
        query = f"""
            SELECT TOP {limit} *
            FROM c
            ORDER BY c._ts DESC
        """
        items = []
        async for item in container.query_items(query=query, enable_cross_partition_query=True):
            items.append(item)
        return items
    except Exception as e:
        log.error(f"CosmosDB query failed: {e}")
        return []


async def stream_new_events(since_ts: float) -> list[dict]:
    """Fetch events newer than since_ts."""
    try:
        container = _cosmos_client.get_database_client(COSMOSDB_DATABASE).get_container_client(COSMOSDB_CONTAINER)
        query = f"SELECT * FROM c WHERE c._ts > {int(since_ts)} ORDER BY c._ts ASC"
        items = []
        async for item in container.query_items(query=query, enable_cross_partition_query=True):
            items.append(item)
        return items
    except Exception as e:
        log.error(f"CosmosDB stream query failed: {e}")
        return []


def agent_class(agent: str) -> str:
    return "text-red-400" if agent == "breaker" else "text-blue-400"


def render_event_row(event: dict) -> str:
    """Render a single battle-log event as an HTMX <tr> fragment."""
    ts = datetime.fromtimestamp(event.get("_ts", 0)).strftime("%H:%M:%S")
    agent = event.get("agent", "?")
    color = agent_class(agent)
    scenario = event.get("scenario", "—")
    action = event.get("action", "—")
    result = event.get("result", "—")
    mttr = event.get("mttr_seconds")
    mttr_str = f"{mttr}s" if mttr else "—"
    pr_url = event.get("pr_url", "")
    pr_link = f'<a href="{pr_url}" target="_blank" class="underline">PR</a>' if pr_url else "—"

    return f"""
<tr class="border-b border-gray-700 hover:bg-gray-800 transition-colors">
  <td class="px-4 py-2 font-mono text-xs text-gray-400">{ts}</td>
  <td class="px-4 py-2 font-bold {color} uppercase text-xs">{agent}</td>
  <td class="px-4 py-2 text-xs text-yellow-300">{scenario}</td>
  <td class="px-4 py-2 text-xs text-gray-200">{action}</td>
  <td class="px-4 py-2 text-xs text-gray-300 max-w-xs truncate" title="{result}">{result}</td>
  <td class="px-4 py-2 text-xs text-green-400">{mttr_str}</td>
  <td class="px-4 py-2 text-xs">{pr_link}</td>
</tr>"""


# ── Kubernetes helpers ─────────────────────────────────────────────────────────
def _get_pod_snapshot() -> list[dict]:
    """Synchronously fetch pod states from watched namespaces."""
    if not _k8s_v1:
        return []
    pods = []
    for ns in WATCH_NAMESPACES:
        try:
            pod_list = _k8s_v1.list_namespaced_pod(namespace=ns)
            for pod in pod_list.items:
                container_statuses = pod.status.container_statuses or []
                restarts = sum(cs.restart_count for cs in container_statuses)
                # Determine simple phase string, including OOMKilled
                phase = pod.status.phase or "Unknown"
                reason = ""
                for cs in container_statuses:
                    if cs.last_state and cs.last_state.terminated:
                        reason = cs.last_state.terminated.reason or ""
                    if cs.state and cs.state.waiting:
                        reason = cs.state.waiting.reason or ""
                    if cs.state and cs.state.terminated:
                        reason = cs.state.terminated.reason or ""

                pods.append({
                    "namespace": ns,
                    "name": pod.metadata.name,
                    "phase": phase,
                    "reason": reason,
                    "restarts": restarts,
                    "ready": all(cs.ready for cs in container_statuses) if container_statuses else False,
                })
        except Exception as e:
            log.warning(f"k8s pods {ns}: {e}")
    return pods


def _get_event_snapshot() -> list[dict]:
    """Synchronously fetch recent Warning events from watched namespaces."""
    if not _k8s_v1:
        return []
    events = []
    for ns in WATCH_NAMESPACES:
        try:
            ev_list = _k8s_v1.list_namespaced_event(
                namespace=ns,
                field_selector="type=Warning",
            )
            for ev in sorted(ev_list.items, key=lambda e: e.last_timestamp or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:10]:
                events.append({
                    "namespace": ns,
                    "name": ev.involved_object.name,
                    "reason": ev.reason,
                    "message": ev.message,
                    "count": ev.count,
                    "timestamp": ev.last_timestamp.isoformat() if ev.last_timestamp else None,
                })
        except Exception as e:
            log.warning(f"k8s events {ns}: {e}")
    return events[:20]  # cap at 20 total


# ── Prometheus helpers ─────────────────────────────────────────────────────────
async def _prom_query(query: str) -> float | None:
    """Run an instant PromQL query, return scalar value or None."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query})
            r.raise_for_status()
            data = r.json()
            results = data.get("data", {}).get("result", [])
            if results:
                return float(results[0]["value"][1])
    except Exception as e:
        log.debug(f"Prometheus query '{query}' failed: {e}")
    return None


async def _get_metrics_snapshot() -> dict:
    """Fetch key metrics from Prometheus."""
    mem_bytes, pg_conns, http_5xx_rate, keda_queue = await asyncio.gather(
        _prom_query(
            'sum(container_memory_working_set_bytes{namespace="target",container="target-app"})'
        ),
        _prom_query(
            'pg_stat_database_numbackends{datname="arena"}'
        ),
        _prom_query(
            'sum(rate(http_requests_total{namespace="target",status=~"5.."}[1m]))'
        ),
        _prom_query(
            'keda_scaler_metrics_value{namespace="arena",scaledObject="queue-worker-scaler"}'
        ),
        return_exceptions=False,
    )
    return {
        "target_memory_mb": round(mem_bytes / 1024 / 1024, 1) if mem_bytes is not None else None,
        "pg_connections": int(pg_conns) if pg_conns is not None else None,
        "http_5xx_rate_per_sec": round(http_5xx_rate, 4) if http_5xx_rate is not None else None,
        "keda_queue_depth": int(keda_queue) if keda_queue is not None else None,
    }


# ── GitHub helpers ─────────────────────────────────────────────────────────────
async def _get_github_incidents() -> list[dict]:
    """Fetch open GitHub Issues with label 'incident'."""
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return []
    try:
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
        params = {"state": "open", "labels": "incident", "per_page": 20}
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.warning(f"GitHub incidents fetch failed: {e}")
        return []


def _fault_badge_color(labels: list[dict]) -> str:
    label_names = {l["name"] for l in labels}
    if "fault-001" in label_names:
        return "bg-orange-900/50 border-orange-500/50 text-orange-300"
    if "fault-002" in label_names:
        return "bg-yellow-900/50 border-yellow-500/50 text-yellow-300"
    if "fault-003" in label_names:
        return "bg-red-900/50 border-red-500/50 text-red-300"
    if "fault-004" in label_names:
        return "bg-purple-900/50 border-purple-500/50 text-purple-300"
    if "fault-005" in label_names:
        return "bg-blue-900/50 border-blue-500/50 text-blue-300"
    if "fault-006" in label_names:
        return "bg-cyan-900/50 border-cyan-500/50 text-cyan-300"
    return "bg-gray-800 border-gray-600 text-gray-300"


def _render_incident_panel(issues: list[dict]) -> str:
    if not issues:
        return """
<div class="bg-gray-900 border border-green-900/40 rounded-xl p-4 flex items-center gap-3">
  <div class="w-3 h-3 rounded-full bg-green-500"></div>
  <span class="text-green-400 font-semibold text-sm">All clear — no active incidents</span>
</div>"""

    cards = []
    for issue in issues:
        title = issue.get("title", "Untitled")
        url = issue.get("html_url", "#")
        number = issue.get("number", "?")
        labels = issue.get("labels", [])
        created_at = issue.get("created_at", "")
        color_class = _fault_badge_color(labels)

        fault_tags = " ".join(
            f'<span class="text-xs px-2 py-0.5 rounded-full bg-gray-700 text-gray-300">#{l["name"]}</span>'
            for l in labels
            if l["name"] != "incident"
        )

        age = ""
        if created_at:
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                elapsed = int((datetime.now(timezone.utc) - dt).total_seconds())
                age = f"{elapsed // 60}m {elapsed % 60}s ago" if elapsed < 3600 else f"{elapsed // 3600}h ago"
            except Exception:
                pass

        cards.append(f"""
<div class="border {color_class} rounded-lg px-4 py-3 flex items-center justify-between gap-4">
  <div class="flex items-center gap-3 min-w-0">
    <div class="w-2.5 h-2.5 rounded-full bg-red-500 breaker-pulse flex-shrink-0"></div>
    <a href="{url}" target="_blank"
       class="font-semibold text-sm hover:underline truncate">
      #{number} {title}
    </a>
    <div class="flex gap-1 flex-shrink-0">{fault_tags}</div>
  </div>
  <span class="text-xs text-gray-500 flex-shrink-0">{age}</span>
</div>""")

    header = f'<div class="text-xs text-red-400 font-bold uppercase tracking-widest mb-3">🚨 Active Incidents ({len(issues)})</div>'
    return f'<div class="space-y-2">{header}{"".join(cards)}</div>'


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": time.time()}


@app.get("/status")
async def target_status():
    """Proxy /health from target-app for dashboard widget."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{TARGET_APP_URL}/health")
            return r.json()
    except Exception as e:
        return {"status": "unreachable", "error": str(e)}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    events = await get_recent_events(limit=50)
    rows = "".join(render_event_row(e) for e in events)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "initial_rows": rows, "event_count": len(events)},
    )


@app.get("/stream")
async def stream(request: Request):
    """
    Server-Sent Events stream.
    Polls CosmosDB every POLL_INTERVAL seconds for new events.
    Sends HTMX-compatible <tr> fragments that get prepended to the battle table.
    """
    async def event_generator() -> AsyncGenerator[str, None]:
        last_ts = time.time()
        # Initial keepalive
        yield "data: \n\n"

        while True:
            if await request.is_disconnected():
                break

            new_events = await stream_new_events(since_ts=last_ts)
            if new_events:
                last_ts = max(e.get("_ts", last_ts) for e in new_events)
                for event in reversed(new_events):  # newest first
                    row_html = render_event_row(event)
                    # HTMX swap via OOB — prepend to #battle-body
                    payload = json.dumps({"html": row_html})
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(POLL_INTERVAL)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


@app.get("/api/incident-snapshot")
async def incident_snapshot():
    """
    Returns a snapshot of cluster state for Breaker to include in GitHub Issue.

    Called by Breaker immediately after triggering a fault scenario.
    Response includes: pod states, recent K8s warning events, Prometheus metrics.
    """
    pods, metrics = await asyncio.gather(
        asyncio.get_event_loop().run_in_executor(None, _get_pod_snapshot),
        _get_metrics_snapshot(),
    )
    events = await asyncio.get_event_loop().run_in_executor(None, _get_event_snapshot)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pods": pods,
        "events": events,
        "metrics": metrics,
    }


@app.get("/api/incidents")
async def api_incidents():
    """Returns open GitHub Issues with label 'incident' (raw JSON from GitHub API)."""
    return await _get_github_incidents()


@app.get("/incidents-panel", response_class=HTMLResponse)
async def incidents_panel():
    """
    HTML fragment for the incident panel — polled by HTMX every 30s.
    Shows open GitHub Issues with label 'incident'.
    """
    issues = await _get_github_incidents()
    return HTMLResponse(_render_incident_panel(issues))
