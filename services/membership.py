"""Immediate membership barriers, batched activity and SQL-before-ACK recovery."""

import json
import logging
import time
import uuid
from collections import OrderedDict

from core.diagnostics import error_fields
from repositories.interfaces.control import ActivityStream, MembershipRepository
from schemas.membership import MembershipEvent

log = logging.getLogger("activity")


class MembershipService:
    def __init__(
        self, repository: MembershipRepository, uow_factory, stream: ActivityStream | None = None
    ):
        self.repository, self.uow_factory, self.stream = repository, uow_factory, stream
        self.consumer = uuid.uuid4().hex
        self.active = OrderedDict()

    async def profile(self, user_id, username, first_name, last_name):
        if (
            type(user_id) is not int
            or not 0 < user_id < 2**63
            or any(
                value is not None and (not isinstance(value, str) or len(value) > 256)
                for value in (username, first_name, last_name)
            )
        ):
            raise ValueError("Invalid human profile")
        async with self.uow_factory() as uow:
            await uow.memberships.profile(user_id, username, first_name, last_name)

    async def persist(self, event):
        async with self.uow_factory() as uow:
            result = await uow.memberships.apply(event)
            active = await uow.memberships.known_active(event.chat_id, event.user_id)
        self.remember(event, active)
        return result

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
        if (
            event.kind != "message"
            or self.stream is None
            or (
                self.active.get((event.chat_id, event.user_id), 0) <= time.monotonic()
                and not await self.repository.known_active(event.chat_id, event.user_id)
            )
        ):
            return await self.persist(event)
        self.remember(event, True)
        try:
            if await self.stream.append(event):
                return True
        except Exception as error:
            log.warning({"event": "membership_stream_fallback", **error_fields(error)})
        return await self.persist(event)

    async def flush(self):
        if self.stream is None:
            return 0
        applied = 0
        for shard in range(self.stream.shards):
            rows = await self.stream.batch(shard, self.consumer)
            valid = []
            for identity, fields in rows:
                try:
                    raw = fields.get(b"event", fields.get("event"))
                    if raw is None or len(raw) > 8192:
                        raise ValueError("Invalid activity record")
                    event = MembershipEvent.load(json.loads(raw))
                    if self.stream.shard(event.chat_id) != shard:
                        raise ValueError("Wrong activity shard")
                    valid.append((identity, event))
                except (ValueError, TypeError, KeyError, UnicodeError):
                    await self.stream.quarantine(shard, identity)
                    await self.stream.acknowledge(shard, [identity])
                    log.warning({"event": "membership_activity_quarantined", "shard": shard})
            if not valid:
                continue
            async with self.uow_factory() as uow:
                for _, event in valid:
                    applied += bool(await uow.memberships.apply(event))
            await self.stream.acknowledge(shard, [identity for identity, _ in valid])
        return applied
