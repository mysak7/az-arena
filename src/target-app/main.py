"""
target-app — az-chaos-arena Battle Target
FastAPI app with deliberate fault injection endpoints.

Endpoints:
  GET  /health      — health check (mem, replicas info)
  GET  /metrics     — Prometheus-compatible metrics
  POST /leak        — allocate 200 MB RAM → triggers OOMKilled
  POST /lock-db     — open 50 idle PostgreSQL connections (connection exhaustion)
  POST /reset       — release all leaks and idle connections
"""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import psutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
POSTGRES_HOST = os.environ["POSTGRES_HOST"]
POSTGRES_DB   = os.environ.get("POSTGRES_DB", "arena")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "arena")
POSTGRES_PASS = os.environ["POSTGRES_PASSWORD"]

# ── Global mutable state (intentionally simple — chaos target) ─────────────────
_leak_chunks: list[bytearray] = []          # holds leaked memory
_idle_connections: list[asyncpg.Connection] = []  # holds DB connections open
_start_time = time.time()

# ── Prometheus metrics ─────────────────────────────────────────────────────────
request_counter    = Counter("target_requests_total", "Total requests", ["endpoint"])
leak_bytes_gauge   = Gauge("target_leak_bytes", "Current leaked bytes")
idle_conns_gauge   = Gauge("target_idle_connections", "Current idle DB connections")

# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("target-app starting up")
    yield
    log.info("target-app shutting down — releasing resources")
    await _release_all()

app = FastAPI(title="target-app", lifespan=lifespan)

# ── Helpers ────────────────────────────────────────────────────────────────────
async def _release_all() -> dict[str, Any]:
    """Release all held memory and DB connections."""
    mem_freed = len(_leak_chunks) * 200 * 1024 * 1024
    _leak_chunks.clear()
    leak_bytes_gauge.set(0)

    closed = len(_idle_connections)
    for conn in _idle_connections:
        try:
            await conn.close()
        except Exception:
            pass
    _idle_connections.clear()
    idle_conns_gauge.set(0)

    return {"mem_freed_mb": mem_freed // (1024 * 1024), "connections_closed": closed}


async def _pg_dsn() -> str:
    return f"postgresql://{POSTGRES_USER}:{POSTGRES_PASS}@{POSTGRES_HOST}/{POSTGRES_DB}"


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    request_counter.labels(endpoint="/health").inc()
    proc = psutil.Process()
    mem = proc.memory_info()
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _start_time, 1),
        "mem_rss_mb": round(mem.rss / 1024 / 1024, 1),
        "mem_vms_mb": round(mem.vms / 1024 / 1024, 1),
        "leak_chunks": len(_leak_chunks),
        "idle_connections": len(_idle_connections),
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    request_counter.labels(endpoint="/metrics").inc()
    return PlainTextResponse(
        content=generate_latest().decode("utf-8"),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.post("/leak")
async def leak():
    """
    Allocate 200 MB of RAM in one shot.
    K8s memory limit is 100Mi → pod will be OOMKilled immediately.
    """
    request_counter.labels(endpoint="/leak").inc()
    log.warning("🔴 ATTACK: Memory leak triggered — allocating 200 MB")

    chunk = bytearray(200 * 1024 * 1024)  # 200 MB
    _leak_chunks.append(chunk)
    leak_bytes_gauge.set(sum(len(c) for c in _leak_chunks))

    return {
        "action": "leak",
        "allocated_mb": 200,
        "total_leaked_mb": round(sum(len(c) for c in _leak_chunks) / 1024 / 1024, 1),
    }


@app.post("/lock-db")
async def lock_db(count: int = 50):
    """
    Open `count` idle PostgreSQL connections running pg_sleep(300).
    Exhausts connection pool → FATAL: too many clients already.
    """
    request_counter.labels(endpoint="/lock-db").inc()
    if count > 200:
        raise HTTPException(status_code=400, detail="count must be ≤ 200")

    log.warning(f"🔴 ATTACK: DB connection exhaustion — opening {count} idle connections")
    dsn = await _pg_dsn()

    async def hold_connection():
        conn = await asyncpg.connect(dsn=dsn)
        _idle_connections.append(conn)
        idle_conns_gauge.set(len(_idle_connections))
        try:
            await conn.execute("SELECT pg_sleep(300)")  # hold for 5 min
        except Exception:
            pass  # expected — connection will be closed by /reset or shutdown

    # Open connections concurrently
    tasks = [asyncio.create_task(hold_connection()) for _ in range(count)]
    # Don't await — let them run in background while we return 200
    asyncio.gather(*tasks, return_exceptions=True)

    return {
        "action": "lock-db",
        "connections_opened": count,
        "total_idle": len(_idle_connections),
    }


@app.post("/reset")
async def reset():
    """
    Emergency reset: release all leaked memory and close all idle connections.
    Used by Healer for rapid hotfix without restarting the pod.
    """
    request_counter.labels(endpoint="/reset").inc()
    log.info("🟢 RESET: Releasing all resources")
    result = await _release_all()
    return {"action": "reset", **result}
