"""Receive self-membership, admin and migration evidence independently of links."""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from datetime import UTC, datetime

from telethon import errors, types, utils

from bot.checks.group import (
    entity_can_send,
    group_kind,
    human_id,
    participant_standing,
    saved_input_peer,
)
from bot.checks.probe import GroupProbe
from core.locks import KeyedLocks
from schemas.group import GroupChange, GroupStanding, Standing

log = logging.getLogger("activity")
GROUP_UPDATES = (
    types.UpdateChannelParticipant,
    types.UpdateChatParticipant,
    types.UpdateChatParticipants,
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
    def __init__(self, client, service, bot_id, *, probe=None, settle_attempts=3, settle_pause=1.5):
        self.client, self.service, self.bot_id = client, service, bot_id
        self.probe = probe or GroupProbe(client, bot_id)
        self._seen = OrderedDict()
        self._checked = OrderedDict()
        self._locks = KeyedLocks()
        self._slots = asyncio.Semaphore(8)
        self.settle_attempts, self.settle_pause = settle_attempts, settle_pause

    async def _entity(self, chat_id, update=None):
        for entity in getattr(update, "_entities", {}).values():
            if utils.get_peer_id(entity) == chat_id:
                return entity
        try:
            async with asyncio.timeout(3), self._slots:
                return await self.client.get_entity(chat_id)
        except (errors.RPCError, ValueError, OSError, TimeoutError):
            if callable(getattr(self.service, "peer", None)):
                try:
                    saved = await self.service.peer(chat_id)
                except Exception as error:
                    if not (
                        isinstance(error, (OSError, TimeoutError))
                        or type(error).__module__.startswith("asyncpg")
                    ):
                        raise
                    saved = None
                if saved is not None:
                    try:
                        async with asyncio.timeout(3), self._slots:
                            return await self.client.get_entity(saved_input_peer(saved))
                    except (errors.RPCError, ValueError, OSError, TimeoutError):
                        pass
            return None

    async def remember(self, entity, chat_id):
        if group_kind(entity) and callable(getattr(self.service, "remember_peer", None)):
            await self.service.remember_peer(
                chat_id, entity.id, group_kind(entity), getattr(entity, "access_hash", None)
            )

    async def settled(self, entity, chat_id):
        report = await self.probe.check(entity, chat_id)
        for _ in range(max(0, self.settle_attempts - 1)):
            if report.standing is not Standing.MEMBER:
                break
            await asyncio.sleep(self.settle_pause)
            report = await self.probe.check(entity, chat_id)
        return report

    async def health(self, chat_id):
        async with self._locks.hold(str(chat_id)):
            entity = await self._entity(chat_id)
            if entity is None:
                return GroupStanding(Standing.UNKNOWN, reason="unresolved")
            report = await self.probe.check(entity, chat_id)
            if report.standing is not Standing.UNKNOWN:
                result = await self._refresh_entity(entity, chat_id, verdict=report)
                if result is None:
                    return GroupStanding(Standing.UNKNOWN, reason="persist_deferred")
            return report

    async def observe(self, event, *, force=False):
        if not getattr(event, "is_group", False):
            return
        chat_id = event.chat_id
        async with asyncio.timeout(5):
            async with self._locks.hold(str(chat_id)):
                if not force and self._seen.get(chat_id, 0) > time.monotonic():
                    return
                entity = getattr(event, "chat", None)
                if entity is None:
                    entity = await self._entity(chat_id)
                kind = group_kind(entity)
                if not kind or utils.get_peer_id(entity) != chat_id:
                    return
                admin = None
                if not getattr(entity, "min", False) and isinstance(
                    entity, (types.Chat, types.Channel)
                ):
                    admin = bool(entity.creator or entity.admin_rights)
                result = await self.service.record(
                    GroupChange(
                        chat_id,
                        "observed",
                        f"message:{event.id}",
                        event_time(getattr(event, "date", None)),
                        kind,
                        active=(
                            None
                            if isinstance(entity, (types.ChatForbidden, types.ChannelForbidden))
                            else not bool(getattr(entity, "left", False))
                        ),
                        admin=admin,
                        can_send=entity_can_send(entity, admin),
                        title=getattr(entity, "title", None),
                        username=getattr(entity, "username", None),
                        stream="observation",
                        priority=0,
                    )
                )
                if result is None:
                    return
                # Persistence must precede SQL reads, live probes and metadata.
                known = await self.service.get(chat_id)
                if (
                    known is not None
                    and not known.migrated_to
                    and known.is_admin is not True
                    and self._checked.get(chat_id, 0) <= time.monotonic()
                    and event_time(getattr(event, "date", None)) >= known.last_event_at
                ):
                    refreshed = await self._refresh_entity(entity, chat_id)
                    if refreshed is not None:
                        self._checked[chat_id] = time.monotonic() + 300
                        self._checked.move_to_end(chat_id)
                        if len(self._checked) > 2048:
                            self._checked.popitem(last=False)
                self._seen[chat_id] = time.monotonic() + 60
                await self.remember(entity, chat_id)
                self._seen.move_to_end(chat_id)
                if len(self._seen) > 2048:
                    self._seen.popitem(last=False)

    async def handle_update(self, update):
        if isinstance(update, types.UpdateChannel):
            await self.refresh(update)
            return
        if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            chat_id = utils.get_peer_id(update.message.peer_id)
        elif isinstance(update, types.UpdateChannelParticipant):
            chat_id = utils.get_peer_id(types.PeerChannel(update.channel_id))
        elif isinstance(update, types.UpdateChatParticipants):
            chat_id = -update.participants.chat_id
        else:
            chat_id = -update.chat_id
        async with self._locks.hold(str(chat_id)):
            await self._handle_update(update)

    async def _handle_update(self, update):
        if isinstance(update, types.UpdateChatParticipants):
            roster = update.participants
            if not isinstance(roster, types.ChatParticipants):
                return
            participant = next(
                (item for item in roster.participants if item.user_id == self.bot_id), None
            )
            active, admin, can_send = participant_standing(participant)
            entity = await self._entity(-roster.chat_id, update)
            await self.service.record(
                GroupChange(
                    -roster.chat_id,
                    "permissions_changed",
                    hashlib.sha256(bytes(update)).hexdigest(),
                    event_time(None),
                    "group",
                    active=active,
                    admin=admin,
                    can_send=can_send,
                    title=getattr(entity, "title", None),
                    stream="basic",
                    sequence=roster.version,
                    priority=2,
                )
            )
            self._seen.pop(-roster.chat_id, None)
            self._checked.pop(-roster.chat_id, None)
            if admin and entity is not None:
                await self.remember(entity, -roster.chat_id)
                await self.snapshot(entity, -roster.chat_id)
            return
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
                isinstance(
                    action,
                    (types.MessageActionChatJoinedByLink, types.MessageActionChatJoinedByRequest),
                )
                and human_id(message.from_id) == self.bot_id
            ):
                active, admin, can_send, verb = True, None, None, "added"
            elif (
                isinstance(action, types.MessageActionChatDeleteUser)
                and action.user_id == self.bot_id
            ):
                active, admin, can_send, verb = False, False, False, "removed"
            else:
                return
            actor = human_id(message.from_id)
            if isinstance(action, types.MessageActionChatJoinedByLink):
                actor = human_id(action.inviter_id)
            if actor == self.bot_id:
                actor = None
            stream, sequence, priority = "service", message.id, 1
        else:
            if update.user_id != self.bot_id:
                return
            if isinstance(update, (types.UpdateChannelParticipant, types.UpdateChatParticipant)):
                basic = isinstance(update, types.UpdateChatParticipant)
                chat_id = (
                    -update.chat_id
                    if basic
                    else utils.get_peer_id(types.PeerChannel(update.channel_id))
                )
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
                if actor == self.bot_id:
                    actor = None
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
                    types.UpdateChatParticipant,
                ),
            ):
                kind = "group"
            if kind is None:
                known = await self.service.get(chat_id)
                kind = known.chat_kind if known else None
        if kind is None:
            log.warning({"event": "group_lifecycle_unverified_peer", "chat_id": chat_id})
            return
        if (active is False or verb == "demoted") and entity is not None:
            # Replayed explicit exits cannot override current, confirmed membership.
            verdict = await self.probe.check(entity, chat_id)
            if verdict.standing is Standing.ADMIN or (verb != "demoted" and verdict.active is True):
                await self._refresh_entity(entity, chat_id, verdict=verdict)
                log.info({"event": "group_stale_exit_ignored", "chat_id": chat_id})
                return
            if verb == "demoted" and verdict.standing is Standing.GONE:
                active = None
        if verb == "added" and entity is not None:
            # Only a confirmed live ADMIN may strengthen an uncertain add event.
            verdict = await self.settled(entity, chat_id)
            if verdict.standing is Standing.ADMIN:
                active, admin, can_send = True, True, verdict.can_send
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
        self._checked.pop(chat_id, None)
        log.info(
            {
                "event": "group_raw_lifecycle",
                "chat_id": chat_id,
                "update_type": type(update).__name__,
                "kind": verb,
            }
        )
        if verb in {"added", "promoted"} and entity is not None:
            await self.remember(entity, chat_id)
            await self.snapshot(entity, chat_id)

    async def refresh(self, update):
        """Only an accepted permission response can change an uncertain standing."""
        chat_id = utils.get_peer_id(types.PeerChannel(update.channel_id))
        async with asyncio.timeout(7):
            async with self._locks.hold(str(chat_id)):
                started = datetime.now(UTC)
                entity = await self._entity(chat_id, update)
                verdict = await self.settled(entity, chat_id) if entity is not None else None
                await self._refresh_entity(
                    entity,
                    chat_id,
                    verdict=verdict,
                    change_kind="permissions_changed",
                    observed_at=started,
                )

    async def _refresh_entity(
        self, entity, chat_id, *, verdict=None, change_kind="standing_refreshed", observed_at=None
    ):
        started = observed_at or datetime.now(UTC)
        when = started
        kind = group_kind(entity)
        if not kind:
            return
        verdict = verdict or await self.probe.check(entity, chat_id)
        if verdict.standing is Standing.UNKNOWN:
            log.warning(
                {"event": "group_standing_unknown", "chat_id": chat_id, "reason": verdict.reason}
            )
            return
        result = await self.service.record(
            GroupChange(
                chat_id,
                change_kind,
                f"probe:{started.isoformat()}",
                when,
                kind,
                active=verdict.active,
                admin=verdict.admin,
                can_send=verdict.can_send,
                title=entity.title,
                username=getattr(entity, "username", None),
                priority=0,
            )
        )
        self._seen.pop(chat_id, None)
        if isinstance(result, dict) and result.get("transition") and result.get("is_admin") is True:
            await self.remember(entity, chat_id)
            await self.snapshot(entity, chat_id)
        return result

    async def snapshot(self, entity, chat_id):
        if not callable(getattr(self.service, "metadata", None)) or not callable(
            getattr(self.client, "get_participants", None)
        ):
            return
        try:
            async with asyncio.timeout(5), self._slots:
                users = await self.client.get_participants(
                    entity, filter=types.ChannelParticipantsAdmins(), limit=200
                )
            admins = []
            owner = None
            for user in users:
                if not isinstance(user, types.User) or user.bot or user.deleted:
                    continue
                if isinstance(
                    getattr(user, "participant", None),
                    (types.ChannelParticipantCreator, types.ChatParticipantCreator),
                ):
                    owner = user.id
                else:
                    admins.append(user.id)
            count = getattr(entity, "participants_count", None)
            await self.service.metadata(
                chat_id,
                owner,
                admins,
                count,
                f"https://t.me/{entity.username}" if getattr(entity, "username", None) else None,
            )
        except (errors.RPCError, OSError, ValueError, TimeoutError) as error:
            log.warning(
                {
                    "event": "group_metadata_unavailable",
                    "chat_id": chat_id,
                    "error_type": type(error).__name__,
                }
            )

    async def send_failure(self, chat_id, error):
        known = await self.service.get(chat_id)
        if known is None:
            return
        name = type(error).__name__
        if name in {"ChatForbiddenError", "UserNotParticipantError"}:
            await self.service.record(
                GroupChange(
                    chat_id,
                    "removed",
                    f"send-gone:{datetime.now(UTC).isoformat()}",
                    datetime.now(UTC),
                    known.chat_kind,
                    active=False,
                    admin=False,
                    can_send=False,
                )
            )
            return
        if name in {
            "ChatWriteForbiddenError",
            "ChatSendPlainForbiddenError",
            "ChatAdminRequiredError",
            "ChatRestrictedError",
        }:
            await self.service.record(
                GroupChange(
                    chat_id,
                    "send_restricted",
                    f"send:{datetime.now(UTC).isoformat()}",
                    datetime.now(UTC),
                    known.chat_kind,
                    can_send=False,
                )
            )
            return
        if name in {"ChannelPrivateError", "ChannelInvalidError"}:
            entity = await self._entity(chat_id)
            if entity is not None:
                await self._refresh_entity(entity, chat_id)
