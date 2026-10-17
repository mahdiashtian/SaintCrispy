"""Couplyo-style bounded, multi-pass Telegram roster paging and recovery."""

import asyncio
import logging
import time
from datetime import UTC, datetime

from telethon import errors, functions, types

from bot.checks.group import group_kind, saved_input_peer
from core.diagnostics import error_fields
from schemas.admin import RetryEffect
from schemas.group import Standing
from schemas.roster_crawl import RosterUser

log = logging.getLogger("activity")


class RosterCrawler:
    def __init__(
        self,
        client,
        service,
        groups,
        tracker,
        bot_id,
        *,
        rate=None,
        max_passes=3,
        max_requests=400,
        delay=1,
        ambiguous_limit=5,
        progress=None,
    ):
        self.client, self.service, self.groups, self.tracker, self.bot_id = (
            client,
            service,
            groups,
            tracker,
            bot_id,
        )
        self.rate, self.max_passes, self.max_requests, self.delay, self.ambiguous_limit = (
            rate,
            max_passes,
            max_requests,
            delay,
            ambiguous_limit,
        )
        self.tasks = []
        self.progress = progress

    async def _request(self, request):
        async with asyncio.timeout(30):
            if self.rate:
                while (delay := await self.rate.reserve()) > 0:
                    if delay > 1:
                        raise RetryEffect(delay, "flood_wait")
                    await asyncio.sleep(delay)
            return await self.client(request)

    async def _peer(self, crawl):
        try:
            async with asyncio.timeout(3):
                return await self.client.get_input_entity(crawl.chat_id)
        except ValueError:
            saved = await self.groups.peer(crawl.chat_id)
            if saved:
                return saved_input_peer(saved)
            entity = await self.tracker._entity(crawl.chat_id)
            if not group_kind(entity):
                raise ValueError("Unresolved verified group peer")
            await self.tracker.remember(entity, crawl.chat_id)
            async with asyncio.timeout(3):
                return await self.client.get_input_entity(entity)

    def _users(self, entities, allowed_ids):
        keep, skipped = {}, set()
        for user in entities:
            identity = getattr(user, "id", None)
            if type(identity) is not int or identity <= 0 or identity not in allowed_ids:
                continue
            if (
                isinstance(user, types.User)
                and not user.bot
                and not user.deleted
                and user.id != self.bot_id
            ):
                keep[user.id] = RosterUser(user.id, user.username, user.first_name, user.last_name)
            else:
                skipped.add(identity)
        skipped.update(allowed_ids - set(keep))
        return list(keep.values()), sorted(skipped - set(keep))

    async def run(self, crawl):
        started = time.monotonic()
        try:
            standing = await self.tracker.health(crawl.chat_id)
            if standing.standing is Standing.UNKNOWN:
                return await self._ambiguous(crawl, "standing_unknown")
            if standing.standing is Standing.GONE:
                return await self.service.finish(crawl, "group_gone", failed=True)
            if standing.standing is not Standing.ADMIN:
                return await self.service.pause(crawl, "not_admin", 300)
            peer = await self._peer(crawl)
            if isinstance(peer, types.InputPeerChat):
                return await self._basic(crawl, peer)
            if not isinstance(peer, types.InputPeerChannel):
                return await self.service.finish(crawl, "unsupported_chat_type", failed=True)
            total = None
            try:
                full = await self._request(functions.channels.GetFullChannelRequest(peer))
                entity = next(
                    (chat for chat in full.chats if getattr(chat, "id", None) == peer.channel_id),
                    None,
                )
                if entity is not None and (
                    not group_kind(entity) or getattr(entity, "gigagroup", False)
                ):
                    return await self.service.finish(crawl, "unsupported_chat_type", failed=True)
                total = getattr(full.full_chat, "participants_count", None)
            except (
                errors.FloodWaitError,
                errors.ChatAdminRequiredError,
                errors.UserNotParticipantError,
            ):
                raise
            except (errors.RPCError, ValueError, OSError, TimeoutError):
                pass
            while True:
                if not await self.service.valid(crawl):
                    await self.service.pause(crawl, "not_admin", 300)
                    return
                if crawl.requests_made >= self.max_requests:
                    return await self.service.finish(crawl, "budget_exhausted")
                fence = await self.service.fence()
                read_at = datetime.now(UTC)
                request_started = time.monotonic()
                page = await self._request(
                    functions.channels.GetParticipantsRequest(
                        channel=peer,
                        filter=types.ChannelParticipantsSearch("")
                        if crawl.filter_index == 0
                        else types.ChannelParticipantsRecent(),
                        offset=crawl.offset_count,
                        limit=200,
                        hash=0,
                    )
                )
                if not hasattr(page, "participants") or not hasattr(page, "users"):
                    return await self._ambiguous(crawl, "incomplete_page")
                ids = {getattr(item, "user_id", None) for item in page.participants}
                ids = {identity for identity in ids if type(identity) is int and identity > 0}
                keep, skipped = self._users(page.users, ids)
                advance = len(page.participants)
                page_total = getattr(page, "count", None)
                if type(page_total) is int and page_total >= 0:
                    total = max(total or 0, page_total)
                filter_ended = advance == 0
                pass_ended = filter_ended and crawl.filter_index == 1
                current = await self.service.page(
                    crawl,
                    keep,
                    skipped,
                    fence=fence,
                    read_at=read_at,
                    offset=0 if filter_ended else crawl.offset_count + advance,
                    total=total,
                    next_filter=1 if filter_ended and not pass_ended else None,
                )
                if current is None:
                    await self.service.pause(crawl, "not_admin", 300)
                    return
                crawl = current
                if self.progress:
                    await self.progress.update()
                if pass_ended:
                    if crawl.pass_new_count == 0:
                        return await self.service.finish(crawl, "converged", converged=True)
                    if crawl.pass_number >= self.max_passes:
                        return await self.service.finish(crawl, "max_passes")
                    crawl = await self.service.advance_pass(crawl)
                    if crawl is None:
                        return
                log.info(
                    {
                        "event": "roster_page",
                        "chat_id": crawl.chat_id,
                        "pass": crawl.pass_number,
                        "filter": crawl.filter_index,
                        "offset": crawl.offset_count,
                        "seen_unique": crawl.seen_unique,
                        "stored_new": crawl.stored_new,
                        "skipped_accounts": crawl.skipped_accounts,
                        "claimed_total": crawl.claimed_total,
                    }
                )
                seconds = time.monotonic() - request_started
                await asyncio.sleep(max(self.delay, seconds if seconds > 5 else 0))
        except asyncio.CancelledError:
            raise
        except errors.FloodWaitError as error:
            if self.rate:
                await self.rate.pause(error.seconds)
            await self.service.pause(crawl, "flood_wait", max(1, error.seconds), flood=True)
        except RetryEffect as error:
            await self.service.pause(crawl, error.code, max(1, error.seconds), flood=True)
        except errors.ChatAdminRequiredError:
            await self.tracker.health(crawl.chat_id)
            await self.service.pause(crawl, "not_admin", 300)
        except (errors.UserNotParticipantError, errors.ChatForbiddenError):
            await self.service.finish(crawl, "group_gone", failed=True)
            await self.tracker.send_failure(
                crawl.chat_id, errors.UserNotParticipantError(request=None)
            )
        except Exception as error:
            log.warning(
                {"event": "roster_crawl_failure", "chat_id": crawl.chat_id, **error_fields(error)}
            )
            await self._ambiguous(crawl, type(error).__name__)
        finally:
            log.info(
                {
                    "event": "roster_visit",
                    "chat_id": crawl.chat_id,
                    "elapsed_seconds": round(time.monotonic() - started, 4),
                }
            )

    async def _basic(self, crawl, peer):
        fence = await self.service.fence()
        read_at = datetime.now(UTC)
        full = await self._request(functions.messages.GetFullChatRequest(peer.chat_id))
        migrated = next(
            (
                getattr(chat, "migrated_to", None)
                for chat in full.chats
                if getattr(chat, "id", None) == peer.chat_id and getattr(chat, "migrated_to", None)
            ),
            None,
        )
        if migrated:
            new_id = -(10**12 + migrated.channel_id)
            await self.groups.migrate(crawl.chat_id, new_id, read_at)
            await self.groups.remember_peer(
                new_id, migrated.channel_id, "supergroup", migrated.access_hash
            )
            await self.service.enqueue(new_id)
            return await self.service.finish(crawl, "migrated", failed=True)
        participants = getattr(getattr(full, "full_chat", None), "participants", None)
        if (
            not isinstance(participants, types.ChatParticipants)
            or participants.chat_id != peer.chat_id
        ):
            return await self._ambiguous(crawl, "incomplete_basic_roster")
        ids = {getattr(item, "user_id", None) for item in participants.participants}
        ids = {identity for identity in ids if type(identity) is int and identity > 0}
        keep, skipped = self._users(full.users, ids)
        current = await self.service.page(
            crawl, keep, skipped, fence=fence, read_at=read_at, offset=len(ids), total=len(ids)
        )
        if current:
            return await self.service.finish(current, "basic_group_complete", converged=True)
        await self.service.pause(crawl, "not_admin", 300)

    async def _ambiguous(self, crawl, reason):
        if crawl.ambiguous_visits + 1 >= self.ambiguous_limit:
            return await self.service.finish(crawl, "ambiguous_error_exhausted", failed=True)
        return await self.service.pause(
            crawl, reason, min(3600, 30 * 2**crawl.ambiguous_visits), ambiguous=True
        )

    async def _loop(self):
        while True:
            try:
                crawl = await self.service.claim()
                if self.progress:
                    await self.progress.update(active=crawl is not None)
                if crawl:
                    await self.run(crawl)
                    if self.progress:
                        await self.progress.update()
                    continue
            except Exception as error:
                log.error({"event": "roster_worker_failed", **error_fields(error)})
            await asyncio.sleep(2)

    async def __aenter__(self):
        self.tasks = [asyncio.create_task(self._loop()) for _ in range(self.service.max_concurrent)]
        return self

    async def __aexit__(self, *args):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
