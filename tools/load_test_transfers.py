"""Local 1000-request test: real HTTP/streaming/tasks, simulated Telegram RPCs.

--database additionally uses the configured PostgreSQL/Redis under a private test
account ID. No Telegram login or user messages; test rows and Redis keys are removed.
"""

import argparse
import asyncio
import json
import os
import secrets
import time
import tracemalloc
from contextlib import AsyncExitStack, suppress
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import asyncpg
import httpx
from redis.asyncio import Redis
from telethon import errors, types

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.bot.jobs.transfers import TransferJobs
from downloader_bot.bot.progress import TransferProgress
from downloader_bot.bot.transfers.streaming import READ_SIZE
from downloader_bot.core.log_writer import JsonLogWriter
from downloader_bot.core.request_context import user_request
from downloader_bot.repositories.redis.media import FileRepository
from downloader_bot.schemas.media import Media, Quality, Source, TelegramFile
from downloader_bot.services.download import DownloadService
from downloader_bot.services.observability import Telemetry

SIZE = 4 * 1024 * 1024 + 123
QUALITY = Quality(
    "original", "Original", "binary", None, "bin", "application/octet-stream", "progressive", ""
)
MEDIA = Media("loadtest", "", "Public test bytes", "", 10, "", None, (QUALITY,))
PEER = types.InputPeerUser(1, 2)
FILE = TelegramFile(10, 20, b"test-reference", bytes(PEER), 1)


class MemoryRepository:
    def __init__(self):
        self.files = {}

    def key(self, site, identity, quality):
        return f"{site}:{identity}:{quality}"

    async def get(self, site, identity, quality):
        return self.files.get(self.key(site, identity, quality))

    async def save(self, site, identity, quality, reference):
        self.files[self.key(site, identity, quality)] = reference


class SimulatedTelegram:
    def __init__(self, latency):
        self.latency = latency
        self.active_parts = self.peak_parts = self.sent = self.uploaded = 0
        self.uploads = {}

    async def __call__(self, request):
        if isinstance(request, types.InputMediaDocumentExternal):
            raise AssertionError("The production path should register before publishing")
        if hasattr(request, "media"):
            # Force HTTP streaming instead of pretending Telegram fetched an external URL.
            raise errors.WebpageCurlFailedError(request=None)
        self.active_parts += 1
        self.peak_parts = max(self.peak_parts, self.active_parts)
        try:
            await asyncio.sleep(self.latency)
            parts = self.uploads.setdefault(request.file_id, {})
            assert request.file_part not in parts
            parts[request.file_part] = len(request.bytes)
            self.uploaded += len(request.bytes)
            if request.file_total_parts != -1:
                expected = request.file_total_parts
                assert all(index in parts for index in range(expected))
                assert sum(parts.values()) == SIZE
            return True
        finally:
            self.active_parts -= 1

    async def send_file(self, peer, media, **options):
        await asyncio.sleep(self.latency)
        self.sent += 1
        if isinstance(media, types.InputMediaUploadedDocument):
            assert sum(self.uploads.pop(media.file.id).values()) == SIZE
        document = SimpleNamespace(
            id=10 + self.sent, access_hash=20, file_reference=b"test", size=SIZE
        )
        return SimpleNamespace(document=document, id=self.sent)


async def run(args):
    if not args.no_memory_tracing:
        tracemalloc.start()
    source_active = source_peak = source_requests = source_bytes = 0
    source_errors = []
    handlers: set[asyncio.Task] = set()
    sources_ready = asyncio.Event()
    expected_sources = min(
        args.unique_downloads, args.requests if args.all_new else args.requests // 2
    )

    async def serve(reader, writer):
        nonlocal source_active, source_peak, source_requests, source_bytes
        task = asyncio.current_task()
        handlers.add(task)
        source_active += 1
        source_peak = max(source_peak, source_active)
        try:
            await reader.readuntil(b"\r\n\r\n")
            source_requests += 1
            if source_requests == expected_sources:
                sources_ready.set()
            if args.synchronize_sources:
                await sources_ready.wait()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {SIZE}\r\n"
                    "Content-Type: application/octet-stream\r\nConnection: close\r\n\r\n"
                ).encode()
            )
            remaining = SIZE
            block = b"x" * READ_SIZE
            while remaining:
                count = min(remaining, READ_SIZE)
                writer.write(block[:count])
                await writer.drain()
                source_bytes += count
                remaining -= count
        except (OSError, asyncio.IncompleteReadError) as error:
            source_errors.append(type(error).__name__)
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            source_active -= 1
            handlers.discard(task)

    samples = []

    async def watch_loop():
        while True:
            started = time.perf_counter()
            await asyncio.sleep(0.01)
            samples.append(max(0, time.perf_counter() - started - 0.01))

    async with AsyncExitStack() as stack:
        server = await asyncio.start_server(serve, "127.0.0.1", 0, backlog=max(100, args.requests))
        await stack.enter_async_context(server)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/file.bin"
        repository = MemoryRepository()
        pool = cache = None
        if args.database:
            pool = await stack.enter_async_context(
                await asyncpg.create_pool(
                    os.environ["DATABASE_URL"],
                    min_size=1,
                    max_size=8,
                    command_timeout=30,
                )
            )
            if os.environ.get("REDIS_URL"):
                cache = Redis.from_url(os.environ["REDIS_URL"], max_connections=16)
                stack.push_async_callback(cache.aclose)
            repository = FileRepository(
                pool, cache, -10_000_000_000 - secrets.randbelow(1_000_000_000)
            )
            await repository.initialize()

        async def cleanup():
            if pool is not None:
                await pool.execute(
                    "DELETE FROM media_files WHERE telegram_account_id=$1",
                    repository.bot_id,
                )
                for table in (
                    "user_link_history",
                    "user_media_history",
                    "media_catalog",
                    "media_aliases",
                ):
                    await pool.execute(
                        f"DELETE FROM {table} WHERE telegram_account_id=$1", repository.bot_id
                    )
            if cache is not None:
                keys = [key async for key in cache.scan_iter(match=repository.namespace + "*")]
                if keys:
                    await cache.delete(*keys)

        stack.push_async_callback(cleanup)
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                trust_env=False,
                limits=httpx.Limits(max_connections=args.concurrency),
                timeout=30,
            )
        )
        telegram = SimulatedTelegram(args.rpc_latency)
        telemetry = None
        if args.log_file:
            telemetry = await stack.enter_async_context(
                Telemetry(JsonLogWriter(args.log_file, stdout=False), interval=1)
            )
        resolved = 0

        async def resolve(media, quality):
            nonlocal resolved
            resolved += 1
            return Source(url, "progressive", size_bytes=SIZE)

        service = DownloadService(
            SimpleNamespace(resolve=resolve),
            repository,
            TelegramDelivery(
                telegram,
                http,
                "",
                upload_parallelism=args.parallelism,
                upload_inflight_parts=args.global_parts,
            ),
            concurrency=args.concurrency,
            cached_concurrency=args.cached_concurrency,
            telemetry=telemetry,
        )
        unique = min(args.unique_downloads, args.requests if args.all_new else args.requests // 2)
        for index in range(0 if args.all_new else unique):
            await repository.save("loadtest", f"cached-{index}", QUALITY.key, FILE)
        release = asyncio.Event()
        outcomes, failures, latencies = [], [], []
        started = time.perf_counter()
        ticker = asyncio.create_task(watch_loop())
        try:
            async with TransferJobs(capacity=args.requests, telemetry=telemetry) as jobs:
                for index in range(args.requests):
                    cached = not args.all_new and index % 2 == 0
                    content = (index if args.all_new else index // 2) % unique
                    identity = f"{'cached' if cached else 'new'}-{content}"
                    sites = ("soundcloud", "youtube", "instagram", "pinterest", "xvideos", "xnxx")
                    site = sites[content % len(sites)] if args.provider_mix else "loadtest"
                    media = replace(MEDIA, content_id=identity, site=site)
                    progress = TransferProgress()
                    job = jobs.reserve(index + 1, index + 1, progress)

                    async def work(media=media, progress=progress, user=index + 1):
                        await release.wait()
                        try:
                            if args.user_history:
                                with user_request(user):
                                    identity = await repository.record_link_view(
                                        user, url + f"?content={media.content_id}"
                                    )
                                    await repository.finish_link_view(user, identity, media)
                                    outcomes.append(
                                        await service.deliver(PEER, media, QUALITY, progress)
                                    )
                            else:
                                outcomes.append(
                                    await service.deliver(PEER, media, QUALITY, progress)
                                )
                        except Exception as error:
                            failures.append(type(error).__name__)
                        latencies.append(time.perf_counter() - started)

                    jobs.start(job, work)
                admitted = jobs.count
                release.set()
                async with asyncio.timeout(120):
                    await jobs.join()
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            if handlers:
                await asyncio.gather(*handlers)
        seconds = time.perf_counter() - started
        peak = None
        if not args.no_memory_tracing:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        latencies.sort()
        result = {
            "checked_at": datetime.now(UTC).isoformat(),
            "requests": args.requests,
            "admitted_before_release": admitted,
            "completed": len(outcomes),
            "failures": failures,
            "transferred": outcomes.count("transferred"),
            "reused": outcomes.count("reused"),
            "seconds": round(seconds, 3),
            "request_completion_p95_seconds": round(latencies[int(len(latencies) * 0.95) - 1], 3),
            "concurrency": args.concurrency,
            "cached_concurrency": args.cached_concurrency,
            "upload_parallelism": args.parallelism,
            "global_inflight_parts": args.global_parts,
            "synchronized_sources": args.synchronize_sources,
            "provider_mix": args.provider_mix,
            "resolved": resolved,
            "http_requests": source_requests,
            "http_peak_connections": source_peak,
            "http_bytes": source_bytes,
            "upload_bytes": telegram.uploaded,
            "peak_upload_parts": telegram.peak_parts,
            "peak_traced_python_mib": round(peak / 1024 / 1024, 2) if peak is not None else None,
            "memory_tracing": not args.no_memory_tracing,
            "max_event_loop_delay_ms": round(max(samples, default=0) * 1000, 2),
            "simulated_send_count": telegram.sent,
            "real_telegram_messages": 0,
            "database": "postgres_redis" if cache is not None else "postgres" if pool else "memory",
            "simulated_rpc_latency_seconds": args.rpc_latency,
            "source_errors": source_errors,
            "complete_media_on_disk": False,
            "logging_enabled": telemetry is not None,
            "log_records_dropped": telemetry.writer.dropped if telemetry else 0,
            "file_bytes": SIZE,
            "all_new": args.all_new,
        }
        assert len(outcomes) == args.requests and not failures and not source_errors
        assert telegram.sent == args.requests and source_requests == resolved == unique
        assert source_bytes == telegram.uploaded == unique * SIZE
        assert telegram.active_parts == 0 and not telegram.uploads
        assert telegram.peak_parts <= args.global_parts
        if args.synchronize_sources:
            assert source_peak == expected_sources
        if args.user_history:
            result["history_users"] = await pool.fetchval(
                "SELECT count(*) FROM user_media_history WHERE telegram_account_id=$1",
                repository.bot_id,
            )
            assert result["history_users"] == args.requests
        if pool is not None:
            result["stored_files"] = await pool.fetchval(
                "SELECT count(*) FROM media_files WHERE telegram_account_id=$1",
                repository.bot_id,
            )
            assert result["stored_files"] == unique * (1 if args.all_new else 2)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--unique-downloads", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=1000)
    parser.add_argument("--cached-concurrency", type=int, default=1000)
    parser.add_argument("--parallelism", type=int, default=4)
    parser.add_argument("--global-parts", type=int, default=64)
    parser.add_argument("--synchronize-sources", action="store_true")
    parser.add_argument("--provider-mix", action="store_true")
    parser.add_argument("--user-history", action="store_true")
    parser.add_argument("--rpc-latency", type=float, default=0.005)
    parser.add_argument("--database", action="store_true")
    parser.add_argument("--log-file", help="Exercise JSON performance logging during the load test")
    parser.add_argument(
        "--no-memory-tracing",
        action="store_true",
        help="Measure scheduling delay without tracemalloc overhead; memory is unreported",
    )
    parser.add_argument("--all-new", action="store_true")
    parser.add_argument("--file-mib", type=int, default=4)
    args = parser.parse_args()
    if not 2 <= args.requests <= 10000 or args.unique_downloads < 1:
        parser.error("requests must be 2..10000 and unique-downloads positive")
    if not 1 <= args.concurrency <= 10000 or not 1 <= args.cached_concurrency <= 10000:
        parser.error("concurrency must be 1..10000")
    if not 1 <= args.parallelism <= 8 or args.rpc_latency < 0:
        parser.error("parallelism must be 1..8 and RPC latency non-negative")
    if not 1 <= args.global_parts <= 1024:
        parser.error("global-parts must be 1..1024")
    if args.user_history and not args.database:
        parser.error("user-history requires database")
    if args.provider_mix and not args.all_new:
        parser.error("provider-mix currently requires all-new")
    expected_sources = min(
        args.unique_downloads, args.requests if args.all_new else args.requests // 2
    )
    if args.synchronize_sources and args.concurrency < expected_sources:
        parser.error("synchronize-sources requires enough transfer slots for every distinct source")
    if not 1 <= args.file_mib <= 64:
        parser.error("file-mib must be 1..64")
    global SIZE
    SIZE = args.file_mib * 1024 * 1024 + 123
    try:
        result = asyncio.run(run(args))
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "benchmark_completed": False}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
