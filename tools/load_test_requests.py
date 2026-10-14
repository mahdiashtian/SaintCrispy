"""Open-loop link-arrival benchmark with real PostgreSQL and simulated origins/Telegram.

Uses TEST_DATABASE_URL and a randomly named private schema; existing tables are
untouched. No Telegram login, origin HTTP requests or media downloads occur.
"""

import argparse
import asyncio
import json
import math
import os
import re
import secrets
import time
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import asyncpg
from dotenv import load_dotenv

from bot.handlers.dispatcher import Dispatcher
from bot.handlers.limited import LimitedHandlers
from bot.state.manager import ConversationManager
from bot.state.menus import PersistentMenuStore
from core.log_writer import JsonLogWriter
from core.logging_config import ApplicationLogging
from db.postgres.engine import migrate
from downloader_bot.downloaders.base import Downloader
from downloader_bot.downloaders.instagram.handler import register_handler as register_instagram
from downloader_bot.downloaders.router import DownloaderRouter
from downloader_bot.downloaders.youtube.handler import register_handler as register_youtube
from repositories.postgres.media import PostgresMediaRepository
from repositories.postgres.workflow import WorkflowRepository
from schemas.media import Media, Quality
from services.download import DownloadService
from services.limits import RequestLimiter
from services.observability import Telemetry, error_fields
from services.user import UserService

ROOT = Path(__file__).resolve().parents[1]
QUALITY = Quality("full", "Full", "h264", None, "mp4", "video/mp4", "progressive", "")


def input_link(index):
    identity = f"{index:011d}"
    return (
        f"https://www.youtube.com/watch?v={identity}"
        if index % 2 == 0
        else f"https://www.instagram.com/reel/{identity}/"
    )


def percentile(values, fraction):
    return (
        round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 6) if values else None
    )


class SimulatedProvider(Downloader):
    def __init__(self, site, delay, qualities=8):
        self.site, self.delay, self.calls = site, delay, 0
        self.qualities = tuple(
            replace(QUALITY, key=f"full_{index}", label=f"Full {index}")
            for index in range(qualities)
        )

    async def inspect(self, url):
        self.calls += 1
        await asyncio.sleep(self.delay)
        parts = urlsplit(url)
        identity = (
            parse_qs(parts.query)["v"][0]
            if self.site == "youtube"
            else parts.path.rstrip("/").rsplit("/", 1)[-1]
        )
        return Media(self.site, identity, "Test", "", 60, url, None, self.qualities)

    async def resolve(self, media, quality):
        raise AssertionError("The arrival benchmark does not transfer media")


async def run(args):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        raise ValueError("TEST_DATABASE_URL is required")
    # Explicit test settings never fall back to the production .env database.
    if not urlsplit(url).hostname:
        raise ValueError("Invalid test database configuration")
    schema = "request_rate_" + secrets.token_hex(8)
    admin = await asyncpg.connect(url, timeout=10)
    created = False
    log_dir = args.output.parent / (args.output.stem + "-logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    report = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        async with AsyncExitStack() as stack:
            pool = await stack.enter_async_context(
                await asyncpg.create_pool(
                    url,
                    min_size=1,
                    max_size=args.pool_size,
                    command_timeout=30,
                    server_settings={"search_path": schema},
                )
            )
            await migrate(pool)
            # Production keeps one coordinator connection outside the query pool.
            coordinator = await pool.acquire()
            stack.push_async_callback(pool.release, coordinator)
            log_settings = SimpleNamespace(
                log_file=str(log_dir / "performance.jsonl"),
                log_max_bytes=20 * 1024**2,
                log_backups=2,
                log_queue_size=10000,
                log_stdout=False,
            )
            logging = await stack.enter_async_context(ApplicationLogging(log_settings))
            telemetry = await stack.enter_async_context(
                Telemetry(
                    JsonLogWriter(log_settings.log_file, 20 * 1024**2, 2, 10000, False),
                    interval=30,
                    log_streams=logging.writers,
                )
            )
            files, workflow = PostgresMediaRepository(pool, 123), WorkflowRepository(pool, 123)
            conversations = ConversationManager(workflow)
            menus = PersistentMenuStore(workflow, limit=2048, conversations=conversations)
            providers = {
                site: SimulatedProvider(site, args.origin_delay, args.qualities)
                for site in ("youtube", "instagram")
            }
            service = DownloadService(
                DownloaderRouter(providers),
                files,
                None,
                metadata_concurrency=args.metadata_concurrency,
                metadata_capacity=args.capacity,
                telemetry=telemetry,
            )
            callbacks = []
            client = SimpleNamespace(
                add_event_handler=lambda callback, builder: callbacks.append(callback)
            )
            admission = (
                {"admission_repository": workflow} if hasattr(workflow, "claim_link") else {}
            )
            limits = RequestLimiter(60, files, telemetry=telemetry, **admission)
            handler_options = {"max_active": args.capacity} if hasattr(limits, "check_link") else {}
            wrapper = LimitedHandlers(
                Dispatcher(client), limits, conversations, UserService(workflow), **handler_options
            )
            register_youtube(wrapper, service, menus)
            register_instagram(wrapper, service, menus)
            if args.mode == "cached":
                for index in range(args.links):
                    await service.inspect(input_link(index))
            initial_origin_calls = sum(provider.calls for provider in providers.values())
            counters = {"menus": 0, "replies": 0, "exceptions": 0}
            arrivals, latencies, scheduler_lags, completed_at = [], [], [], []
            tasks = set()
            loop_delays = []
            started = time.perf_counter()

            async def watch_loop():
                while True:
                    target = time.perf_counter() + 0.01
                    await asyncio.sleep(0.01)
                    loop_delays.append(max(0, time.perf_counter() - target))

            async def process(index, sent):
                link_index = index if args.mode == "cold" else index % args.links

                async def respond(text, **options):
                    await asyncio.sleep(args.telegram_latency)
                    counters["replies"] += 1
                    counters["menus"] += int(bool(options.get("buttons")))

                event = SimpleNamespace(
                    raw_text=input_link(link_index),
                    sender_id=1 if args.same_user else index + 1,
                    chat_id=1 if args.same_user else index + 1,
                    id=index + 1,
                    respond=respond,
                )
                try:
                    await callbacks[link_index % 2](event)
                except Exception:
                    counters["exceptions"] += 1
                finally:
                    ended = time.perf_counter()
                    completed_at.append(ended - started)
                    latencies.append(ended - sent)

            ticker = asyncio.create_task(watch_loop())
            count = args.rate * args.seconds
            try:
                for index in range(count):
                    target = started + index / args.rate
                    if (delay := target - time.perf_counter()) > 0:
                        await asyncio.sleep(delay)
                    sent = time.perf_counter()
                    arrivals.append(sent - started)
                    scheduler_lags.append(sent - target)
                    task = asyncio.create_task(process(index, sent))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                async with asyncio.timeout(120):
                    while tasks:
                        await asyncio.gather(*tuple(tasks))
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tuple(tasks), return_exceptions=True)
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)
            expected = 1 if args.same_user else count
            bins = [
                sum(second <= value < second + 1 for value in arrivals)
                for second in range(args.seconds)
            ]
            report = {
                "checked_at": datetime.now(UTC).isoformat(),
                "mode": args.mode,
                "requested_arrivals_per_second": args.rate,
                "arrival_counts_per_second": bins,
                "arrival_scheduler_p95_lag_seconds": percentile(scheduler_lags, 0.95),
                "requests": count,
                "completed": len(latencies),
                "completion_counts_per_second": [
                    sum(second <= value < second + 1 for value in completed_at)
                    for second in range(math.ceil(time.perf_counter() - started))
                ],
                "responses_during_arrival_window": sum(
                    value < args.seconds for value in completed_at
                ),
                **counters,
                "expected_menus": expected,
                "passed": counters["menus"] == expected and counters["exceptions"] == 0,
                "response_sla_seconds": args.response_sla,
                "response_sla_met": percentile(latencies, 0.95) <= args.response_sla,
                "arrival_scheduling_met": max(scheduler_lags, default=0) <= 0.1,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
                "completion_p95_seconds": percentile(latencies, 0.95),
                "maximum_event_loop_delay_seconds": round(max(loop_delays, default=0), 6),
                "origin_inspections": sum(p.calls for p in providers.values())
                - initial_origin_calls,
                "admission_database_checks": telemetry.counters["admission_database_checks"],
                "admission_local_rejections": telemetry.counters["admission_local_rejections"],
                "capacity_rejections": telemetry.counters["link_handler_capacity_rejections"],
                "peak_active_handlers": getattr(wrapper, "peak_active", None),
                "error_replies_suppressed": telemetry.counters["link_error_replies_suppressed"],
                "database": "real_postgresql_private_schema",
                "pool_size": args.pool_size,
                "coordinator_connections": 1,
                "metadata_concurrency": args.metadata_concurrency,
                "metadata_capacity": args.capacity,
                "qualities_per_menu": args.qualities,
                "simulated_origins": True,
                "simulated_telegram": True,
                "real_telegram_messages": 0,
                "log_queue_capacity_per_stream": log_settings.log_queue_size,
                "log_stdout": log_settings.log_stdout,
                "history_views": int(
                    await pool.fetchval("SELECT COALESCE(sum(view_count),0) FROM user_link_history")
                ),
                "durable_menus": await pool.fetchval("SELECT count(*) FROM download_menus"),
            }
        report["log_records_dropped"] = (
            sum(w.dropped for w in logging.writers.values()) + telemetry.writer.dropped
        )
        report["log_write_errors"] = (
            sum(w.write_errors for w in logging.writers.values()) + telemetry.writer.write_errors
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    finally:
        # Only the exact schema created by this invocation can be removed.
        if created and re.fullmatch(r"request_rate_[0-9a-f]{16}", schema):
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=int, default=1000)
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--links", type=int, default=100)
    parser.add_argument("--qualities", type=int, default=8)
    parser.add_argument("--response-sla", type=float, default=1)
    parser.add_argument("--mode", choices=("cached", "cold", "coalesced"), default="cached")
    parser.add_argument("--same-user", action="store_true")
    parser.add_argument("--pool-size", type=int, default=32)
    parser.add_argument("--strict-rate", action="store_true")
    parser.add_argument("--metadata-concurrency", type=int, default=8)
    parser.add_argument("--capacity", type=int, default=1000)
    parser.add_argument("--origin-delay", type=float, default=0.01)
    parser.add_argument("--telegram-latency", type=float, default=0.001)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/request-rate.json")
    args = parser.parse_args()
    if (
        not 1 <= args.rate <= 10000
        or not 1 <= args.seconds <= 30
        or args.rate * args.seconds > 30000
    ):
        parser.error("Use 1..10000 arrivals/second, 1..30 seconds and at most 30000 requests")
    if not 2 <= args.pool_size <= 64 or not 1 <= args.links <= 10000:
        parser.error("Use 2..64 pool connections and 1..10000 link identities")
    if not 0 <= args.origin_delay <= 2 or not 0 <= args.telegram_latency <= 1:
        parser.error("Simulation delays are outside the supported range")
    if not 1 <= args.qualities <= 50 or not 0 < args.response_sla <= 120:
        parser.error("Use 1..50 qualities and a positive response SLA up to 120 seconds")
    if not 1 <= args.metadata_concurrency <= 1000 or not 1 <= args.capacity <= 10000:
        parser.error("Use 1..1000 metadata workers and 1..10000 pending handlers")
    if args.mode == "cached" and args.links > args.capacity:
        parser.error("Cached runs require --links no greater than --capacity")
    load_dotenv(ROOT / ".env")
    try:
        report = await run(args)
    except Exception as error:
        print(json.dumps({"benchmark_failed": True, **error_fields(error)}))
        return 2
    print(json.dumps(report, indent=2))
    passed = (
        report["passed"] and not report["log_records_dropped"] and not report["log_write_errors"]
    )
    if args.strict_rate:
        passed = passed and report["response_sla_met"] and report["arrival_scheduling_met"]
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
