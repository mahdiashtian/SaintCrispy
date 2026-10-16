import asyncio
import os
from contextlib import AsyncExitStack
from dataclasses import replace
from functools import partial
from pathlib import Path

import httpx
from telethon import TelegramClient
from telethon.crypto import aes

from bot.delivery import TelegramDelivery
from bot.file_expiry import FileExpiry
from bot.handlers import register_handlers
from bot.handlers.groups import GroupTracker
from bot.jobs import TransferJobs
from bot.management import ManagementHooks
from bot.operations import EffectDelivery, OperationalWorkers
from bot.premium_emoji import BotPresentation
from bot.roster_crawl import RosterCrawler
from bot.session import catch_up_bot, connect_bot, create_session, sign_in_bot
from bot.state.manager import ConversationManager
from bot.state.menus import PersistentMenuStore
from bot.state.storage import PostgresStateStorage
from bot.texts.downloads import download_caption
from core.config import Settings
from core.dns_tunnel import configured_proxy
from core.log_writer import JsonLogWriter
from core.logging_config import ApplicationLogging
from core.maintenance import MaintenanceGate
from db.postgres.engine import Database
from db.redis.pool import create_redis
from downloader_bot.downloaders.instagram.client import InstagramClient, instagram_session
from downloader_bot.downloaders.instagram.downloader import InstagramDownloader
from downloader_bot.downloaders.instagram.urls import DEFAULT_RESOLVER as INSTAGRAM_RESOLVER
from downloader_bot.downloaders.pinterest.client import PinterestClient
from downloader_bot.downloaders.pinterest.downloader import PinterestDownloader
from downloader_bot.downloaders.router import DownloaderRouter
from downloader_bot.downloaders.soundcloud.client import SoundCloudClient
from downloader_bot.downloaders.soundcloud.downloader import SoundCloudDownloader
from downloader_bot.downloaders.spotify.client import SpotifyClient, spotify_sessions
from downloader_bot.downloaders.spotify.downloader import SpotifyDownloader
from downloader_bot.downloaders.tiktok.client import TikTokClient, tiktok_session
from downloader_bot.downloaders.tiktok.downloader import TikTokDownloader
from downloader_bot.downloaders.xnxx.client import XNXXClient
from downloader_bot.downloaders.xnxx.downloader import XNXXDownloader
from downloader_bot.downloaders.xvideos.client import XVideosClient
from downloader_bot.downloaders.xvideos.downloader import XVideosDownloader
from downloader_bot.downloaders.youtube.client import YouTubeClient
from downloader_bot.downloaders.youtube.downloader import YouTubeDownloader
from downloader_bot.downloaders.youtube.urls import network_host as youtube_network_host
from repositories.postgres.admin import AdminRepository
from repositories.postgres.download_quota import DownloadQuotaRepository
from repositories.postgres.file_expiry import FileExpiryRepository
from repositories.postgres.group import GroupRepository
from repositories.postgres.membership import MembershipRepository
from repositories.postgres.outbox import OutboxRepository
from repositories.postgres.roster_crawl import RosterCrawlRepository
from repositories.postgres.unit_of_work import PostgresUnitOfWork
from repositories.postgres.workflow import WorkflowRepository
from repositories.redis.activity import ActivityStream, LifecycleStream
from repositories.redis.media import FileRepository
from repositories.redis.objects import ObjectCache
from repositories.redis.telegram_rate import TelegramRateRepository
from services.access import ProviderAccess
from services.admin import AdminService
from services.download import DownloadService
from services.group import GroupService
from services.limits import RequestLimiter
from services.management import ManagementService
from services.membership import MembershipService
from services.observability import Telemetry, error_fields
from services.operations import OperationsService
from services.roster_crawl import RosterCrawlService
from services.telegram_rate import TelegramRate
from services.user import UserService


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
        self._entered = False

    async def __aenter__(self):
        if self._entered:
            raise RuntimeError("Create a new container for each runtime lifecycle")
        self._entered = True
        try:
            await self.startup()
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *args):
        await self._stack.aclose()

    async def startup(self) -> None:
        if aes.cryptg is None:
            raise RuntimeError(
                "Install cryptg to avoid slow pure-Python encryption in the event loop"
            )
        telemetry = await self._configure_logging()
        bot_id = int(self.settings.bot_token.split(":", 1)[0])
        pool, http, repository, coordinator = await self._connect_storage(telemetry, bot_id)
        client, bot = await self._connect_telegram(telemetry, bot_id, coordinator)
        downloader = await self._create_downloaders(repository)
        await self._configure_application(
            telemetry, pool, http, repository, client, bot, downloader
        )

    async def _configure_logging(self):
        settings, stack = self.settings, self._stack
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
            postgres_pool_size=settings.postgres_pool_size,
            remux_concurrency=settings.remux_concurrency,
            upload_inflight_parts=settings.upload_inflight_parts,
            upload_parallelism=settings.upload_parallelism,
            max_file_bytes=settings.max_file_bytes,
            transfer_timeout_seconds=settings.transfer_timeout,
        )
        return telemetry

    async def _connect_storage(self, telemetry, bot_id):
        settings, stack = self.settings, self._stack
        telemetry.emit("startup_progress", stage="database_connecting")
        database = await stack.enter_async_context(
            Database(settings.database_url, settings.postgres_pool_size)
        )
        pool = database.pool
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
        coordinator = await stack.enter_async_context(pool.acquire())
        if not await coordinator.fetchval("SELECT pg_try_advisory_lock($1)", bot_id):
            raise RuntimeError("Another coordinator is already running for this Telegram bot")
        repository = FileRepository(pool, redis, bot_id)
        self.cache_redis = redis
        self.state_redis = None
        if settings.redis_state_url:
            self.state_redis = create_redis(settings.redis_state_url)
            stack.push_async_callback(self.state_redis.aclose)
        telemetry.emit("startup_progress", stage="database_initializing")
        await repository.initialize()
        return pool, http, repository, coordinator

    async def _connect_telegram(self, telemetry, bot_id, coordinator):
        settings, stack = self.settings, self._stack
        session_path = Path(f"{settings.session_name}-{settings.bot_token.split(':', 1)[0]}")
        session = await stack.enter_async_context(create_session(session_path))
        client = TelegramClient(
            session,
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
        return client, bot

    async def _create_downloaders(self, repository):
        stack = self._stack
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
            # Each account owns a cookie jar and keeps the same egress through delivery.
            proxy = os.environ.get("INSTAGRAM_PROXY", "")
            session = await stack.enter_async_context(instagram_session(proxy))
            return InstagramClient(
                session,
                cookie,
                proxy=proxy,
                root_doc_id=os.environ.get("INSTAGRAM_ROOT_DOC_ID", "27130156389949648"),
                shortcode_doc_id=os.environ.get("INSTAGRAM_SHORTCODE_DOC_ID", "27128499623469141"),
                web_doc_id=os.environ.get("INSTAGRAM_WEB_DOC_ID") or "27830990013244856",
                resolver_url=os.environ.get("INSTAGRAM_RESOLVER_URL", INSTAGRAM_RESOLVER),
            )

        instagram_accounts = {}
        for row in await repository.accounts("instagram"):
            if cookie := os.environ.get(row["credential_env"]):
                instagram_accounts[str(row["id"])] = await instagram_client(cookie)
        if cookie := os.environ.get("INSTAGRAM_COOKIE"):
            instagram_accounts["environment"] = await instagram_client(cookie)
        youtube_client = YouTubeClient.from_environment()
        youtube_client = replace(
            youtube_client,
            proxy=await configured_proxy(stack, os.environ, "YOUTUBE", youtube_network_host),
        )
        youtube_accounts = {}
        for row in await repository.accounts("youtube"):
            if path := os.environ.get(row["credential_env"]):
                youtube_accounts[str(row["id"])] = replace(youtube_client, cookies_file=path)
        if youtube_client.cookies_file:
            youtube_accounts["environment"] = youtube_client
        tiktok = TikTokClient.from_environment(
            await stack.enter_async_context(tiktok_session()),
            await stack.enter_async_context(tiktok_session(os.environ.get("TIKTOK_PROXY", ""))),
        )
        spotify = SpotifyClient.from_environment(
            *await stack.enter_async_context(spotify_sessions(os.environ.get("SPOTIFY_PROXY", "")))
        )
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
                "tiktok": TikTokDownloader(tiktok),
                "spotify": SpotifyDownloader(spotify),
            }
        )
        return downloader

    async def _configure_application(
        self, telemetry, pool, http, repository, client, bot, downloader
    ):
        settings, stack = self.settings, self._stack
        storage_peer = None
        if settings.storage_chat:
            storage_peer = await client.get_input_entity(settings.storage_chat)
        presentation = BotPresentation()
        await stack.enter_async_context(presentation.loading(client, settings.custom_emoji_set))
        access = ProviderAccess(settings.main_admin_id)
        rate = TelegramRate(
            TelegramRateRepository(self.state_redis, bot.id)
            if self.state_redis is not None
            else None,
            per_second=settings.sends_per_second or 25,
        )
        file_expiry = FileExpiry(client, FileExpiryRepository(pool, bot.id), repository)
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
                presentation=presentation,
                bot_username=bot.username,
                file_expiry=file_expiry,
                rate=rate,
            ),
            storage_peer=storage_peer,
            concurrency=settings.concurrency,
            cached_concurrency=settings.cached_concurrency,
            metadata_concurrency=settings.metadata_concurrency,
            metadata_capacity=settings.max_requests,
            transfer_timeout=settings.transfer_timeout,
            telemetry=telemetry,
            caption_builder=partial(download_caption, bot_username=bot.username),
            access=access,
        )
        jobs = await stack.enter_async_context(
            TransferJobs(capacity=settings.max_requests, telemetry=telemetry)
        )
        workflow = WorkflowRepository(pool, bot.id)
        conversations = ConversationManager(PostgresStateStorage(workflow))
        self.uow_factory = lambda: PostgresUnitOfWork(pool, bot.id)
        groups = GroupService(
            GroupRepository(pool, bot.id),
            self.uow_factory,
            settings.group_links_per_minute,
            cache=ObjectCache(self.cache_redis, bot.id, settings.cache_ttl_seconds),
            main_admin_id=settings.main_admin_id,
            roster_enabled=settings.roster_crawl_enabled,
            management_enabled=True,
            retry_queue=LifecycleStream(self.state_redis, bot.id)
            if self.state_redis is not None
            else None,
        )
        admin = AdminService(
            AdminRepository(pool, bot.id),
            self.uow_factory,
            groups,
            settings.main_admin_id,
            groups.cache,
        )
        memberships = MembershipService(
            MembershipRepository(pool, bot.id),
            self.uow_factory,
            ActivityStream(self.state_redis, bot.id) if self.state_redis is not None else None,
        )
        tracker = GroupTracker(client, groups, bot.id)
        memberships.recover_groups = groups.flush_deferred
        memberships.lifecycle_stream = groups.retry_queue
        gate = MaintenanceGate()
        roster = (
            RosterCrawlService(
                RosterCrawlRepository(pool, bot.id),
                self.uow_factory,
                max_concurrent=settings.roster_crawl_max_concurrent,
            )
            if settings.roster_crawl_enabled
            else None
        )
        admin.roster = roster
        management = ManagementService(admin, groups, bot.id)
        groups.management_changed = admin.channels.invalidate
        hooks = ManagementHooks(client, management, rate=rate)
        service.delivery.archive_hook = management.archive_download
        operations = OperationsService(
            OutboxRepository(pool, bot.id),
            self.uow_factory,
            admin,
            EffectDelivery(
                client,
                tracker,
                groups,
                presentation=presentation,
                rate=rate,
                admin=admin,
                roster=roster,
                memberships=memberships,
                management=management,
            ),
            management=management,
        )
        operational_workers = OperationalWorkers(
            operations, memberships, settings.activity_flush_seconds, groups, management
        )
        workers = [operational_workers]
        if roster is not None:
            workers.append(
                RosterCrawler(
                    client,
                    roster,
                    groups,
                    tracker,
                    bot.id,
                    rate=rate,
                    max_passes=settings.roster_crawl_max_passes,
                    max_requests=settings.roster_crawl_max_requests,
                    delay=settings.roster_crawl_delay_seconds,
                )
            )
        admin.maintenance.guard = partial(
            gate.guard,
            jobs=jobs,
            workers=workers,
            memberships=memberships,
            groups=groups,
            tracker=tracker,
            cache=groups.cache,
            bot_id=bot.id,
        )
        recovered = await workflow.recover()
        telemetry.emit("conversations_recovered", interrupted_requests=recovered)
        menus = PersistentMenuStore(
            workflow, limit=max(2048, settings.max_requests), conversations=conversations
        )
        limits = RequestLimiter(
            settings.request_interval,
            repository,
            telemetry=telemetry,
            admission_repository=workflow,
            groups=groups,
            quota_repository=DownloadQuotaRepository(
                pool, bot.id, group_default=settings.group_links_per_minute
            ),
        )
        self.client = client
        self.pool = pool
        self.repository = repository
        self.workflow = workflow
        self.groups = groups
        self.conversations = conversations
        self.service = service
        self.jobs = jobs
        self.telemetry = telemetry
        self.admin, self.memberships, self.operations = admin, memberships, operations
        register_handlers(
            client,
            service,
            menus,
            jobs,
            limits,
            conversations=conversations,
            users=UserService(workflow, self.uow_factory),
            bot_username=bot.username,
            presentation=presentation,
            max_link_handlers=settings.max_requests,
            groups=groups,
            bot_id=bot.id,
            access=access,
            admin=admin,
            memberships=memberships,
            tracker=tracker,
            gate=gate,
            policy=hooks,
        )
        await catch_up_bot(client, telemetry)
        await stack.enter_async_context(file_expiry)
        for worker in workers:
            await stack.enter_async_context(worker)
        gate.ready = True
        telemetry.emit("bot_ready", bot_username=bot.username)
