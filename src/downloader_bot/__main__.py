import asyncio
import os
from contextlib import AsyncExitStack
from dataclasses import replace

import asyncpg
import httpx
from redis.asyncio import Redis
from telethon import TelegramClient, errors
from telethon.crypto import aes
from telethon.sessions import MemorySession

from downloader_bot.config import Settings
from downloader_bot.database import FileRepository
from downloader_bot.downloaders.instagram.client import InstagramClient
from downloader_bot.downloaders.instagram.downloader import InstagramDownloader
from downloader_bot.downloaders.pinterest.client import PinterestClient
from downloader_bot.downloaders.pinterest.downloader import PinterestDownloader
from downloader_bot.downloaders.router import DownloaderRouter
from downloader_bot.downloaders.soundcloud.client import SoundCloudClient
from downloader_bot.downloaders.soundcloud.downloader import SoundCloudDownloader
from downloader_bot.downloaders.xnxx.client import XNXXClient
from downloader_bot.downloaders.xnxx.downloader import XNXXDownloader
from downloader_bot.downloaders.xvideos.client import XVideosClient
from downloader_bot.downloaders.xvideos.downloader import XVideosDownloader
from downloader_bot.downloaders.youtube.client import YouTubeClient
from downloader_bot.downloaders.youtube.downloader import YouTubeDownloader
from downloader_bot.handlers import register_handlers
from downloader_bot.jobs import TransferJobs
from downloader_bot.limits import RequestLimiter
from downloader_bot.menus import MenuStore
from downloader_bot.service import DownloadService
from downloader_bot.telegram import TelegramDelivery


async def create_provider_session(stack: AsyncExitStack) -> httpx.AsyncClient:
    """Own one HTTP pool and cookie jar for a provider or authenticated account."""
    return await stack.enter_async_context(
        httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0"},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    )


async def sign_in_bot(client, bot_token: str):
    """Honor Telegram's login cooldown without restarting or blocking the event loop."""
    while True:
        try:
            return await client.sign_in(bot_token=bot_token)
        except errors.FloodWaitError as error:
            await asyncio.sleep(max(1, error.seconds))


async def connect_bot(client):
    """Keep the process alive during network outages; avoid Docker restart/login loops."""
    delay = 2
    while True:
        try:
            await client.connect()
            return
        except (OSError, TimeoutError):
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)


async def main() -> None:
    settings = Settings.from_environment()
    if aes.cryptg is None:
        raise RuntimeError("Install cryptg to avoid slow pure-Python encryption in the event loop")
    async with AsyncExitStack() as stack:
        pool = await stack.enter_async_context(
            await asyncpg.create_pool(
                settings.database_url,
                min_size=1,
                max_size=8,
                command_timeout=30,
            )
        )
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0"},
                limits=httpx.Limits(
                    max_connections=max(16, settings.concurrency),
                    max_keepalive_connections=min(64, settings.concurrency),
                ),
            )
        )
        redis = None
        if settings.redis_url:
            redis = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
                max_connections=16,
            )
            stack.push_async_callback(redis.aclose)
        client = TelegramClient(
            MemorySession(),
            settings.api_id,
            settings.api_hash,
            request_retries=2,
            connection_retries=2,
            flood_sleep_threshold=0,
        )
        stack.push_async_callback(client.disconnect)
        await connect_bot(client)
        await sign_in_bot(client, settings.bot_token)
        bot = await client.get_me()
        # Enforce one coordinator per bot; conversion workers do not own database/cache writes.
        coordinator = await stack.enter_async_context(pool.acquire())
        if not await coordinator.fetchval("SELECT pg_try_advisory_lock($1)", bot.id):
            raise RuntimeError("Another coordinator is already running for this Telegram bot")
        coordinator.add_termination_listener(lambda _: asyncio.ensure_future(client.disconnect()))
        repository = FileRepository(pool, redis, bot.id)
        await repository.initialize()
        provider_http = {
            site: await create_provider_session(stack)
            for site in ("soundcloud", "xnxx", "xvideos", "pinterest")
        }
        accounts = {}
        for row in await repository.accounts("soundcloud"):
            if token := os.environ.get(row["credential_env"]):
                accounts[str(row["id"])] = SoundCloudClient(
                    await create_provider_session(stack),
                    token,
                )
        if token := os.environ.get("SOUNDCLOUD_OAUTH_TOKEN"):
            accounts["environment"] = SoundCloudClient(await create_provider_session(stack), token)

        async def instagram_client(cookie=None):
            # Each account needs an isolated cookie jar; CDN delivery uses the shared client.
            session = await stack.enter_async_context(httpx.AsyncClient(timeout=15))
            return InstagramClient(
                session,
                cookie,
                root_doc_id=os.environ.get("INSTAGRAM_ROOT_DOC_ID", "27130156389949648"),
                shortcode_doc_id=os.environ.get("INSTAGRAM_SHORTCODE_DOC_ID", "27128499623469141"),
            )

        instagram_accounts = {}
        for row in await repository.accounts("instagram"):
            if cookie := os.environ.get(row["credential_env"]):
                instagram_accounts[str(row["id"])] = await instagram_client(cookie)
        if cookie := os.environ.get("INSTAGRAM_COOKIE"):
            instagram_accounts["environment"] = await instagram_client(cookie)
        youtube_client = YouTubeClient.from_environment()
        youtube_accounts = {}
        for row in await repository.accounts("youtube"):
            if path := os.environ.get(row["credential_env"]):
                youtube_accounts[str(row["id"])] = replace(youtube_client, cookies_file=path)
        if youtube_client.cookies_file:
            youtube_accounts["environment"] = youtube_client
        downloader = DownloaderRouter(
            {
                "soundcloud": SoundCloudDownloader(
                    SoundCloudClient(provider_http["soundcloud"]), accounts
                ),
                "xnxx": XNXXDownloader(XNXXClient(provider_http["xnxx"])),
                "xvideos": XVideosDownloader(XVideosClient(provider_http["xvideos"])),
                "pinterest": PinterestDownloader(PinterestClient(provider_http["pinterest"])),
                "youtube": YouTubeDownloader(
                    replace(youtube_client, cookies_file=None), youtube_accounts
                ),
                "instagram": InstagramDownloader(await instagram_client(), instagram_accounts),
            }
        )
        storage_peer = None
        if settings.storage_chat:
            storage_peer = await client.get_input_entity(settings.storage_chat)
        service = DownloadService(
            downloader,
            repository,
            TelegramDelivery(
                client,
                http,
                settings.ffmpeg,
                upload_parallelism=settings.upload_parallelism,
                upload_inflight_parts=settings.upload_inflight_parts,
                remux_concurrency=settings.remux_concurrency,
                sends_per_second=settings.sends_per_second,
                max_file_bytes=settings.max_file_bytes,
            ),
            storage_peer=storage_peer,
            concurrency=settings.concurrency,
            cached_concurrency=settings.cached_concurrency,
            metadata_concurrency=settings.metadata_concurrency,
            metadata_capacity=settings.max_requests,
            transfer_timeout=settings.transfer_timeout,
        )
        jobs = await stack.enter_async_context(TransferJobs(capacity=settings.max_requests))
        menus = MenuStore(limit=max(2048, settings.max_requests))
        limits = RequestLimiter(settings.request_interval, repository)
        register_handlers(client, service, menus, jobs, limits)
        await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
