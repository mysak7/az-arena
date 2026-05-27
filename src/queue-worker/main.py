"""
queue-worker — az-arena Background Job Processor

Polls arena_jobs table for pending jobs, processes them (simulated work),
marks them done. Scales via KEDA PostgreSQL scaler.

KEDA trigger query:
  SELECT COUNT(*) FROM arena_jobs WHERE status = 'pending'
  → 1 pod per 5 pending jobs, scale-to-zero when queue is empty

Endpoints:
  GET /health   — liveness / readiness
  GET /metrics  — Prometheus metrics
"""
import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgresql.arena.svc.cluster.local")
POSTGRES_DB   = os.environ.get("POSTGRES_DB", "arena")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "arena")
POSTGRES_PASS = os.environ["POSTGRES_PASSWORD"]
WORKER_BATCH  = int(os.environ.get("WORKER_BATCH_SIZE", "3"))   # jobs per poll
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "2"))

# ── Prometheus metrics ─────────────────────────────────────────────────────────
jobs_processed  = Counter("queue_worker_jobs_processed_total", "Jobs processed successfully")
jobs_failed     = Counter("queue_worker_jobs_failed_total", "Jobs failed to process")
jobs_pending    = Gauge("queue_worker_jobs_pending", "Current pending jobs in queue")
processing_time = Histogram("queue_worker_processing_seconds", "Job processing duration")

# ── Global pool ────────────────────────────────────────────────────────────────
_pool: asyncpg.Pool | None = None
_start_time = time.time()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        _pool = await asyncpg.create_pool(
            host=POSTGRES_HOST,
            database=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASS,
            min_size=1,
            max_size=3,   # intentionally small — queue-worker stays lean
            command_timeout=10,
        )
    return _pool


# ── Job processing ─────────────────────────────────────────────────────────────
async def process_job(conn: asyncpg.Connection, job_id: int, payload: dict) -> None:
    """Simulate work: random 0.5-2.5s delay then mark done."""
    duration = random.uniform(0.5, 2.5)
    await asyncio.sleep(duration)
    await conn.execute(
        "UPDATE arena_jobs SET status = 'done', processed_at = NOW() WHERE id = $1",
        job_id,
    )
    jobs_processed.inc()
    log.info(f"✅ Job {job_id} done in {duration:.2f}s")


async def poll_and_process() -> int:
    """Fetch up to WORKER_BATCH pending jobs, process them. Returns count processed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, payload FROM arena_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT $1
                """,
                WORKER_BATCH,
            )
            if not rows:
                return 0

            # Mark as processing atomically
            job_ids = [r["id"] for r in rows]
            await conn.execute(
                "UPDATE arena_jobs SET status = 'processing' WHERE id = ANY($1::int[])",
                job_ids,
            )

        # Process outside transaction (so lock is released)
        tasks = []
        for row in rows:
            with processing_time.time():
                tasks.append(process_job(conn, row["id"], row["payload"] or {}))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                jobs_failed.inc()
                log.error(f"Job failed: {r}")

    return len(rows)


async def update_pending_gauge() -> None:
    """Update the pending jobs gauge for Prometheus."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM arena_jobs WHERE status = 'pending'"
            )
            jobs_pending.set(count or 0)
    except Exception as e:
        log.warning(f"Could not update pending gauge: {e}")


# ── Background worker loop ─────────────────────────────────────────────────────
async def worker_loop() -> None:
    log.info("queue-worker: starting poll loop")
    while True:
        try:
            processed = await poll_and_process()
            await update_pending_gauge()
            if processed == 0:
                await asyncio.sleep(POLL_INTERVAL)
        except asyncpg.TooManyConnectionsError:
            log.error("🔴 Too many DB connections — backing off 10s (DB connection exhaustion?)")
            await asyncio.sleep(10)
        except Exception as e:
            log.error(f"Worker loop error: {e}")
            await asyncio.sleep(POLL_INTERVAL)


# ── App ────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = await asyncpg.create_pool(
        host=POSTGRES_HOST,
        database=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASS,
        min_size=1,
        max_size=3,
    )
    loop_task = asyncio.create_task(worker_loop())
    log.info("queue-worker ready")
    yield
    loop_task.cancel()
    if _pool:
        await _pool.close()


app = FastAPI(title="queue-worker", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _start_time, 1),
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(
        content=generate_latest().decode("utf-8"),
        media_type=CONTENT_TYPE_LATEST,
    )
