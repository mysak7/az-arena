"""
arena-dashboard — az-arena Live Battle Dashboard
FastAPI + HTMX + Server-Sent Events

Routes:
  GET /          — main HTML dashboard
  GET /stream    — SSE stream of new battle-log events (as <tr> rows)
  GET /health    — health check
  GET /status    — current target-app status (proxied)
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import AsyncGenerator

import httpx
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
COSMOSDB_ENDPOINT  = os.environ["COSMOSDB_ENDPOINT"]
COSMOSDB_DATABASE  = os.environ.get("COSMOSDB_DATABASE", "arena")
COSMOSDB_CONTAINER = os.environ.get("COSMOSDB_CONTAINER", "battle-log")
TARGET_APP_URL     = os.environ.get("TARGET_APP_URL", "http://target-app.target.svc.cluster.local")
POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL_SECONDS", "3"))

# ── App setup ──────────────────────────────────────────────────────────────────
_cosmos_client: CosmosClient | None = None
_credential: DefaultAzureCredential | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cosmos_client, _credential
    _credential = DefaultAzureCredential()
    _cosmos_client = CosmosClient(COSMOSDB_ENDPOINT, credential=_credential)
    log.info("arena-dashboard: CosmosDB client initialised")
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
