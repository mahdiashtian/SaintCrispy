import asyncio
import os
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path

import httpx
from telethon import TelegramClient
from telethon.crypto import aes
from telethon.sessions import SQLiteSession

from downloader_bot.bot.delivery import TelegramDelivery
from downloader_bot.bot.handlers import register_handlers
from downloader_bot.bot.jobs.transfers import TransferJobs
from downloader_bot.bot.presentation import BotPresentation
from downloader_bot.bot.session import connect_bot, sign_in_bot
from downloader_bot.bot.state.manager import ConversationManager
from downloader_bot.bot.state.menus import PersistentMenuStore
from downloader_bot.core.config import Settings
from downloader_bot.core.log_writer import JsonLogWriter
from downloader_bot.core.logging_config import ApplicationLogging
from downloader_bot.db.postgres.engine import create_pool
from downloader_bot.db.redis.pool import create_redis
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
from downloader_bot.repositories.postgres.workflow import WorkflowRepository
from downloader_bot.repositories.redis.media import FileRepository
from downloader_bot.services.download import DownloadService
from downloader_bot.services.limits import RequestLimiter
from downloader_bot.services.observability import Telemetry, error_fields
from downloader_bot.services.user import UserService


async def create_provider_session(stack: AsyncExitStack) -> httpx.AsyncClient:
    """Own one HTTP pool and cookie jar for a provider or authenticated account."""
    return await stack.enter_async_context(
        httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0"},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    )


class Container:
    """Composition root owns resources; handlers receive application services."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._stack = AsyncExitStack()

    async def __aenter__(self):
        try:
            await self.startup()
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *args):
        await self._stack.aclose()

    async def startup(self) -> None:
        settings, stack = self.settings, self._stack
        if aes.cryptg is None:
            raise RuntimeError(
                "Install cryptg to avoid slow pure-Python encryption in the event loop"
            )
        application_logging = await stack.enter_async_context(ApplicationLogging(settings))
        telemetry = await stack.enter_async_context(
            Telemetry(
                JsonLogWriter(
                    settings.log_file,
                    settings.log_max_bytes,
                    settings.log_backups,
                    settings.log_queue_size,
                    settings.log_stdout,
                ),
                settings.metrics_interval,
                settings.metrics_network_interface,
                log_streams=application_logging.writers,
            )
        )
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _, context: telemetry.emit(
                "async_task_failure",
                **error_fields(context.get("exception") or RuntimeError()),
            )
        )
        stack.callback(loop.set_exception_handler, previous_handler)
        telemetry.emit(
            "runtime_configured",
            concurrency=settings.concurrency,
            request_capacity=settings.max_requests,
            metadata_concurrency=settings.metadata_concurrency,
            remux_concurrency=settings.remux_concurrency,
            upload_inflight_parts=settings.upload_inflight_parts,
            upload_parallelism=settings.upload_parallelism,
            max_file_bytes=settings.max_file_bytes,
            transfer_timeout_seconds=settings.transfer_timeout,
        )
        telemetry.emit("startup_progress", stage="database_connecting")
        pool = await stack.enter_async_context(create_pool(settings.database_url))
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
        if settings.redis_cache_url or settings.redis_url:
            redis = create_redis(settings.redis_cache_url or settings.redis_url)
            stack.push_async_callback(redis.aclose)
        bot_id = int(settings.bot_token.split(":", 1)[0])
        coordinator = await stack.enter_async_context(pool.acquire())
        if not await coordinator.fetchval("SELECT pg_try_advisory_lock($1)", bot_id):
            raise RuntimeError("Another coordinator is already running for this Telegram bot")
        repository = FileRepository(pool, redis, bot_id)
        telemetry.emit("startup_progress", stage="database_initializing")
        await repository.initialize()
        session_path = Path(f"{settings.session_name}-{settings.bot_token.split(':', 1)[0]}")
        session_path.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            SQLiteSession(str(session_path)),
            settings.api_id,
            settings.api_hash,
            request_retries=2,
            connection_retries=2,
            flood_sleep_threshold=0,
            catch_up=True,
        )
        stack.push_async_callback(client.disconnect)
        telemetry.emit("startup_progress", stage="telegram_connecting")
        await connect_bot(client, telemetry)
        telemetry.emit("startup_progress", stage="telegram_authenticating")
        if not await client.is_user_authorized():
            await sign_in_bot(client, settings.bot_token, telemetry)
        bot = await client.get_me()
        if not bot or bot.id != bot_id:
            raise RuntimeError("Telegram session does not match the configured bot")
        # Enforce one coordinator per bot; conversion workers do not own database/cache writes.
        coordinator.add_termination_listener(lambda _: asyncio.ensure_future(client.disconnect()))
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
            telemetry=telemetry,
        )
        jobs = await stack.enter_async_context(
            TransferJobs(capacity=settings.max_requests, telemetry=telemetry)
        )
        workflow = WorkflowRepository(pool, bot.id)
        conversations = ConversationManager(workflow)
        recovered = await workflow.recover()
        telemetry.emit("conversations_recovered", interrupted_requests=recovered)
        menus = PersistentMenuStore(
            workflow, limit=max(2048, settings.max_requests), conversations=conversations
        )
        limits = RequestLimiter(settings.request_interval, repository, telemetry=telemetry)
        self.client = client
        self.pool = pool
        self.repository = repository
        self.workflow = workflow
        self.conversations = conversations
        self.service = service
        self.jobs = jobs
        self.telemetry = telemetry
        presentation = BotPresentation()
        await presentation.load(client, settings.custom_emoji_set)
        register_handlers(
            client,
            service,
            menus,
            jobs,
            limits,
            conversations=conversations,
            users=UserService(workflow),
            bot_username=bot.username,
            presentation=presentation,
        )
        telemetry.emit("bot_ready", bot_username=bot.username)
