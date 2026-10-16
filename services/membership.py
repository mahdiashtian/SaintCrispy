"""Immediate membership barriers, batched activity and SQL-before-ACK recovery."""

import json
import logging
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime

from core.diagnostics import error_fields
from repositories.interfaces.control import ActivityStream, MembershipRepository
from schemas.membership import HumanProfile, MembershipEvent

log = logging.getLogger("activity")


class MembershipService:
    def __init__(
        self, repository: MembershipRepository, uow_factory, stream: ActivityStream | None = None
    ):
        self.repository, self.uow_factory, self.stream = repository, uow_factory, stream
        self.consumer = uuid.uuid4().hex
        self.active = OrderedDict()
        self.profile_seen = OrderedDict()
        self.recover_groups = None
        self.lifecycle_stream = None

    async def parent_pending(self, chat_id):
        stream = self.lifecycle_stream
        return bool(
            stream is not None and await stream.redis.xlen(stream.key(stream.shard(chat_id)))
        )

    async def known_member(self, chat_id, user_id):
        return await self.repository.known_member(chat_id, user_id)

    async def departure(self, chat_id, user_id, occurred_at, event_key):
        async with self.uow_factory() as uow:
            await uow.memberships.departure(chat_id, user_id, occurred_at, event_key)

    async def refresh_profile(
        self, user_id, username, first_name, last_name, *, deleted=False, bot=False
    ):
        async with self.uow_factory() as uow:
            await uow.memberships.refresh_profile(
                user_id, username, first_name, last_name, deleted=deleted, bot=bot
            )

    async def purge_invalid(self, user_id, *, deleted=False, bot=False, root_id=None):
        if not (deleted or bot) or user_id == root_id:
            return False
        async with self.uow_factory() as uow:
            return await uow.memberships.purge_invalid(user_id, deleted=deleted, bot=bot)

    async def profile(self, user_id, username, first_name, last_name):
        event = HumanProfile(user_id, username, first_name, last_name, datetime.now(UTC))
        try:
            async with self.uow_factory() as uow:
                await self.apply_profile(uow, event)
        except Exception as error:
            return await self.defer(event, error)
        return True

    async def apply_profile(self, uow, event):
        await uow.memberships.profile(
            event.user_id,
            event.username,
            event.first_name,
            event.last_name,
            occurred_at=event.occurred_at,
        )

    async def persist(self, event):
        try:
            async with self.uow_factory() as uow:
                if event.kind == "left":
                    await uow.memberships.departure(
                        event.chat_id, event.user_id, event.occurred_at, event.event_key
                    )
                result = await uow.memberships.apply(event)
                active = await uow.memberships.known_active(event.chat_id, event.user_id)
                missing = (
                    self.lifecycle_stream is not None
                    and not result
                    and await uow.groups.get(event.chat_id) is None
                )
        except Exception as error:
            return await self.defer(event, error)
        if missing and await self.parent_pending(event.chat_id):
            return await self.defer(event, OSError("Group creation is pending"))
        self.remember(event, active)
        return result

    async def defer(self, event, error):
        if self.stream is None or not (
            isinstance(error, (OSError, TimeoutError))
            or type(error).__module__.startswith("asyncpg")
        ):
            raise error
        if isinstance(event, MembershipEvent):
            self.remember(event, False)
        if not await self.stream.append(event):
            raise error
        log.error(
            {
                "event": "membership_sql_deferred",
                "chat_id": event.chat_id,
                "kind": event.kind,
                **error_fields(error),
            }
        )
        return False

    def remember(self, event, active):
        key = (event.chat_id, event.user_id)
        if active:
            self.active[key] = time.monotonic() + 60
            self.active.move_to_end(key)
            if len(self.active) > 100000:
                self.active.popitem(last=False)
        else:
            self.active.pop(key, None)

    async def observe(self, event):
        if event.kind != "message" or self.stream is None:
            return await self.persist(event)
        if self.active.get((event.chat_id, event.user_id), 0) <= time.monotonic():
            try:
                active = await self.repository.known_active(event.chat_id, event.user_id)
            except Exception as error:
                return await self.defer(event, error)
            if not active:
                return await self.persist(event)
        self.remember(event, True)
        try:
            if await self.stream.append(event):
                return True
        except Exception as error:
            log.warning({"event": "membership_stream_fallback", **error_fields(error)})
        return await self.persist(event)

    async def apply_batch(self, uow, valid):
        profiles = [
            (identity, event) for identity, event in valid if isinstance(event, HumanProfile)
        ]
        valid = [
            (identity, event) for identity, event in valid if isinstance(event, MembershipEvent)
        ]
        for _, event in profiles:
            await self.apply_profile(uow, event)
        profile_ids = [identity for identity, _ in profiles]
        batched = await uow.memberships.apply_messages([event for _, event in valid])
        if batched is not None:
            return len(profiles) + batched, profile_ids + [identity for identity, _ in valid]
        changed_count, acknowledged = len(profiles), profile_ids
        for identity, event in valid:
            if event.kind == "left":
                await uow.memberships.departure(
                    event.chat_id, event.user_id, event.occurred_at, event.event_key
                )
            changed = await uow.memberships.apply(event)
            if (
                not changed
                and self.lifecycle_stream is not None
                and await uow.groups.get(event.chat_id) is None
                and await self.parent_pending(event.chat_id)
            ):
                continue
            changed_count += bool(changed)
            acknowledged.append(identity)
        return changed_count, acknowledged

    async def flush(self):
        if self.stream is None:
            return 0
        if self.recover_groups is not None:
            await self.recover_groups()
        applied = 0
        for shard in range(self.stream.shards):
            rows = await self.stream.batch(shard, self.consumer)
            valid = []
            for identity, fields in rows:
                try:
                    raw = fields.get(b"event", fields.get("event"))
                    if raw is None or len(raw) > 8192:
                        raise ValueError("Invalid activity record")
                    data = json.loads(raw)
                    schema = (
                        HumanProfile
                        if isinstance(data, dict) and data.get("kind") == "profile"
                        else MembershipEvent
                    )
                    event = schema.load(data)
                    if self.stream.shard(event.chat_id) != shard:
                        raise ValueError("Wrong activity shard")
                    valid.append((identity, event))
                except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                    await self.stream.quarantine(shard, identity)
                    await self.stream.acknowledge(shard, [identity])
                    log.warning({"event": "membership_activity_quarantined", "shard": shard})
            if not valid:
                continue
            async with self.uow_factory() as uow:
                changed, acknowledged = await self.apply_batch(uow, valid)
                applied += changed
            await self.stream.acknowledge(shard, acknowledged)
        return applied
