"""Open-loop group activity load against isolated SQL/STATE; no Telegram traffic."""

import argparse
import asyncio
import json
import math
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.postgres.engine import migrate  # noqa: E402
from db.redis.pool import create_redis  # noqa: E402
from repositories.postgres.admin import AdminRepository  # noqa: E402
from repositories.postgres.membership import MembershipRepository  # noqa: E402
from repositories.postgres.unit_of_work import PostgresUnitOfWork  # noqa: E402
from repositories.redis.activity import ActivityStream  # noqa: E402
from schemas.membership import MembershipEvent  # noqa: E402
from services.membership import MembershipService  # noqa: E402


def percentile(values, fraction):
    return (
        round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 6) if values else None
    )


async def run(args):
    dsn, redis_url = os.environ.get("TEST_DATABASE_URL"), os.environ.get("TEST_REDIS_URL")
    if not dsn or not redis_url:
        raise ValueError("Dedicated TEST_DATABASE_URL and TEST_REDIS_URL required")
    schema = "group_load_" + uuid.uuid4().hex
    bot = uuid.uuid4().int % (2**62)
    owner = await asyncpg.connect(dsn)
    redis = create_redis(redis_url)
    await owner.execute(f'CREATE SCHEMA "{schema}"')
    report = {}
    try:
        async with asyncpg.create_pool(
            dsn,
            min_size=2,
            max_size=20,
            command_timeout=30,
            server_settings={"search_path": schema},
        ) as pool:
            await migrate(pool)
            await pool.execute(
                "INSERT INTO bot_groups(telegram_account_id,chat_id,chat_kind,is_active,is_admin) SELECT $1,-n,'group',TRUE,TRUE FROM generate_series(1,50)n",
                bot,
            )
            await pool.execute(
                "INSERT INTO bot_users(telegram_account_id,user_id,account_kind) SELECT $1,n,'real' FROM generate_series(1,$2)n",
                bot,
                args.users,
            )
            await pool.execute(
                "INSERT INTO group_memberships(telegram_account_id,chat_id,user_id,is_active,state_changed_at) SELECT $1,-(1+(n-1)%50),n,TRUE,'epoch' FROM generate_series(1,$2)n",
                bot,
                args.users,
            )
            stream = ActivityStream(redis, bot, shards=16, batch_size=2000, reclaim_ms=0)
            service = MembershipService(
                MembershipRepository(pool, bot), lambda: PostgresUnitOfWork(pool, bot), stream
            )
            tasks = set()
            latencies = []
            lag = []
            admin_latencies = []
            accepted = failed = rejected = peak = 0
            stop = asyncio.Event()

            async def ingest(index):
                nonlocal accepted, failed
                started = time.perf_counter()
                user = 1 + index % args.users
                event = MembershipEvent(
                    -(1 + (user - 1) % 50),
                    user,
                    f"message:{index}",
                    "message",
                    datetime.now(UTC),
                    observed_admin=True,
                )
                try:
                    await service.observe(event)
                    accepted += 1
                except Exception:
                    failed += 1
                latencies.append(time.perf_counter() - started)

            async def flush():
                while not stop.is_set():
                    await service.flush()
                    await asyncio.sleep(0.25)

            async def monitor():
                target = time.perf_counter()
                admin = AdminRepository(pool, bot)
                while not stop.is_set():
                    target += 0.05
                    await asyncio.sleep(max(0, target - time.perf_counter()))
                    lag.append(max(0, time.perf_counter() - target))
                    if len(lag) % 10 == 0:
                        started = time.perf_counter()
                        await admin.stats()
                        admin_latencies.append(time.perf_counter() - started)

            worker = asyncio.create_task(flush())
            observer = asyncio.create_task(monitor())
            started = time.perf_counter()
            total = args.rate * args.seconds
            try:
                for index in range(total):
                    await asyncio.sleep(max(0, started + index / args.rate - time.perf_counter()))
                    if len(tasks) >= args.capacity:
                        rejected += 1
                        continue
                    task = asyncio.create_task(ingest(index))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                    peak = max(peak, len(tasks))
                if tasks:
                    await asyncio.gather(*tuple(tasks))
                ingest_elapsed = time.perf_counter() - started
                async with asyncio.timeout(180):
                    while sum(
                        await asyncio.gather(
                            *(redis.xlen(stream.key(i)) for i in range(stream.shards))
                        )
                    ):
                        if worker.done():
                            await worker
                        await asyncio.sleep(0.1)
                elapsed = time.perf_counter() - started
            finally:
                stop.set()
                worker.cancel()
                observer.cancel()
                await asyncio.gather(worker, observer, return_exceptions=True)
            committed = await pool.fetchval(
                "SELECT count(*) FROM membership_events WHERE telegram_account_id=$1", bot
            )
            counted = await pool.fetchval(
                "SELECT sum(message_count) FROM group_memberships WHERE telegram_account_id=$1", bot
            )
            # Exact replay must not change SQL counters, even under concurrent replay.
            replay = MembershipEvent(
                -1, 1, "message:0", "message", datetime.now(UTC), observed_admin=True
            )
            await asyncio.gather(*(service.persist(replay) for _ in range(100)))
            recounted = await pool.fetchval(
                "SELECT sum(message_count) FROM group_memberships WHERE telegram_account_id=$1", bot
            )
            report = {
                "scope": "workstation_real_postgres_redis_simulated_updates_no_telegram",
                "offered_rate": args.rate,
                "offered_events": total,
                "accepted": accepted,
                "failed": failed,
                "capacity_rejected": rejected,
                "peak_active_tasks": peak,
                "ingest_elapsed_seconds": round(ingest_elapsed, 3),
                "drained_elapsed_seconds": round(elapsed, 3),
                "ingestion_p95_seconds": percentile(latencies, 0.95),
                "ingestion_p99_seconds": percentile(latencies, 0.99),
                "event_loop_lag_p99_seconds": percentile(lag, 0.99),
                "admin_stats_p95_seconds": percentile(admin_latencies, 0.95),
                "committed_events": committed,
                "counted_messages": int(counted),
                "replay_count_unchanged": recounted == counted,
                "event_loss": accepted - committed,
            }
            if committed != accepted or counted != accepted or recounted != counted or failed:
                raise AssertionError("Load accounting invariant failed")
    finally:
        keys = [key async for key in redis.scan_iter(match=f"*:{bot}:*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
        await owner.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await owner.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=int, default=1000)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--capacity", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/group-state-load.json")
    args = parser.parse_args()
    if (
        not 1 <= args.rate <= 10000
        or not 1 <= args.seconds <= 120
        or not 1 <= args.users <= 100000
        or not 1 <= args.capacity <= 10000
    ):
        parser.error("Load bounds exceeded")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
