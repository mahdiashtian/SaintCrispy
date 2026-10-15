"""Receive self-membership, admin and migration evidence independently of links."""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from datetime import UTC, datetime

from telethon import errors, types, utils

from bot.checks.group import entity_can_send, group_kind, human_id, participant_standing
from core.diagnostics import error_fields
from schemas.group import GroupChange

log = logging.getLogger("activity")
GROUP_UPDATES = (
    types.UpdateChannelParticipant,
    types.UpdateChatParticipantAdd,
    types.UpdateChatParticipantDelete,
    types.UpdateChatParticipantAdmin,
    types.UpdateNewMessage,
    types.UpdateNewChannelMessage,
    types.UpdateChannel,
)


def event_time(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return datetime.now(UTC)


class GroupTracker:
    def __init__(self, client, service, bot_id):
        self.client, self.service, self.bot_id = client, service, bot_id
        self._seen = OrderedDict()
        self._locks = tuple(asyncio.Lock() for _ in range(32))
        self._slots = asyncio.Semaphore(8)

    async def _entity(self, chat_id, update=None):
        for entity in getattr(update, "_entities", {}).values():
            if utils.get_peer_id(entity) == chat_id:
                return entity
        try:
            async with asyncio.timeout(3), self._slots:
                return await self.client.get_entity(chat_id)
        except (errors.RPCError, ValueError, OSError, TimeoutError):
            return None

    async def observe(self, event):
        if not getattr(event, "is_group", False):
            return
        chat_id = event.chat_id
        async with asyncio.timeout(5):
            async with self._locks[hash(chat_id) % len(self._locks)]:
                if self._seen.get(chat_id, 0) > time.monotonic():
                    return
                entity = getattr(event, "chat", None)
                if entity is None:
                    entity = await self._entity(chat_id)
                kind = group_kind(entity)
                if not kind:
                    return
                known = await self.service.get(chat_id)
                if (
                    known is not None
                    and known["is_active"] is False
                    and not known["migrated_to"]
                    and event_time(getattr(event, "date", None)) > known["last_event_at"]
                ):
                    await self._refresh_entity(entity, chat_id)
                admin = None
                if not getattr(entity, "min", False) and isinstance(
                    entity, (types.Chat, types.Channel)
                ):
                    admin = bool(entity.creator or entity.admin_rights)
                await self.service.record(
                    GroupChange(
                        chat_id,
                        "observed",
                        f"message:{event.id}",
                        event_time(getattr(event, "date", None)),
                        kind,
                        active=not bool(getattr(entity, "left", False)),
                        admin=admin,
                        can_send=entity_can_send(entity, admin),
                        title=getattr(entity, "title", None),
                        username=getattr(entity, "username", None),
                        stream="observation",
                        priority=0,
                    )
                )
                self._seen[chat_id] = time.monotonic() + 60
                self._seen.move_to_end(chat_id)
                if len(self._seen) > 2048:
                    self._seen.popitem(last=False)

    async def handle_update(self, update):
        if isinstance(update, types.UpdateChannel):
            await self.refresh(update)
            return
        if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            message = update.message
            if not isinstance(message, types.MessageService):
                return
            action, chat_id = message.action, utils.get_peer_id(message.peer_id)
            when = event_time(message.date)
            if isinstance(action, types.MessageActionChatMigrateTo):
                await self.service.migrate(
                    chat_id, utils.get_peer_id(types.PeerChannel(action.channel_id)), when
                )
                self._seen.pop(chat_id, None)
                return
            if isinstance(action, types.MessageActionChannelMigrateFrom):
                await self.service.migrate(-action.chat_id, chat_id, when)
                self._seen.pop(chat_id, None)
                return
            if isinstance(action, (types.MessageActionChatAddUser, types.MessageActionChatCreate)):
                if self.bot_id not in action.users:
                    return
                active, admin, can_send, verb = True, None, None, "added"
            elif (
                isinstance(action, types.MessageActionChatDeleteUser)
                and action.user_id == self.bot_id
            ):
                active, admin, can_send, verb = False, False, False, "removed"
            else:
                return
            actor = human_id(message.from_id)
            stream, sequence, priority = "service", message.id, 1
        else:
            if update.user_id != self.bot_id:
                return
            if isinstance(update, types.UpdateChannelParticipant):
                chat_id = utils.get_peer_id(types.PeerChannel(update.channel_id))
                active, admin, can_send = participant_standing(update.new_participant)
                previous, was_admin, _ = participant_standing(update.prev_participant)
                if active is None:
                    return
                verb = (
                    "removed"
                    if not active
                    else "added"
                    if not previous
                    else "promoted"
                    if admin and not was_admin
                    else "demoted"
                    if was_admin and not admin
                    else "permissions_changed"
                )
                actor = human_id(update.actor_id)
                stream, sequence, priority = "participant", update.qts, 2
            else:
                chat_id = -update.chat_id
                if isinstance(update, types.UpdateChatParticipantAdmin):
                    active, admin, can_send = True, update.is_admin, None
                    verb = "promoted" if admin else "demoted"
                elif isinstance(update, types.UpdateChatParticipantAdd):
                    active, admin, can_send, verb = True, None, None, "added"
                else:
                    active, admin, can_send, verb = False, False, False, "removed"
                actor = human_id(getattr(update, "inviter_id", None))
                stream, sequence, priority = "basic", update.version, 2
            when = event_time(getattr(update, "date", None))
        entity = await self._entity(chat_id, update)
        kind = group_kind(entity)
        if kind is None:
            known = await self.service.get(chat_id)
            kind = known["chat_kind"] if known else None
            if isinstance(
                getattr(getattr(update, "message", None), "peer_id", None), types.PeerChat
            ):
                kind = "group"
            if kind is None and isinstance(
                update,
                (
                    types.UpdateChatParticipantAdd,
                    types.UpdateChatParticipantDelete,
                    types.UpdateChatParticipantAdmin,
                ),
            ):
                kind = "group"
        if kind is None:
            log.warning({"event": "group_lifecycle_unverified_peer", "chat_id": chat_id})
            return
        key = hashlib.sha256(bytes(update)).hexdigest()
        await self.service.record(
            GroupChange(
                chat_id,
                verb,
                key,
                when,
                kind,
                active,
                admin,
                can_send,
                actor,
                getattr(entity, "title", None),
                getattr(entity, "username", None),
                stream,
                sequence,
                priority,
            )
        )
        self._seen.pop(chat_id, None)

    async def refresh(self, update):
        """Only an accepted permission response can change an uncertain standing."""
        chat_id = utils.get_peer_id(types.PeerChannel(update.channel_id))
        async with asyncio.timeout(7):
            async with self._locks[hash(chat_id) % len(self._locks)]:
                entity = await self._entity(chat_id, update)
                await self._refresh_entity(entity, chat_id)

    async def _refresh_entity(self, entity, chat_id):
        started = datetime.now(UTC)
        when = started.replace(microsecond=0)
        kind = group_kind(entity)
        if not kind:
            return
        try:
            async with asyncio.timeout(3), self._slots:
                permission = await self.client.get_permissions(entity, self.bot_id)
        except errors.UserNotParticipantError:
            active, admin, can_send = False, False, False
        except (errors.RPCError, ValueError, OSError, TimeoutError) as error:
            log.warning(
                {"event": "group_standing_unknown", "chat_id": chat_id, **error_fields(error)}
            )
            return
        else:
            active, admin, can_send = participant_standing(permission.participant)
            if active is None:
                return
            if can_send is None:
                can_send = entity_can_send(entity, admin)
        await self.service.record(
            GroupChange(
                chat_id,
                "standing_refreshed",
                f"probe:{started.isoformat()}",
                when,
                kind,
                active=active,
                admin=admin,
                can_send=can_send,
                title=entity.title,
                username=getattr(entity, "username", None),
                priority=0,
            )
        )
        self._seen.pop(chat_id, None)
