"""Record real humans on all group messages and complementary raw membership updates."""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from types import SimpleNamespace

from telethon import errors, types, utils

from bot.checks.group import group_kind, human_id, participant_standing
from schemas.group import GroupChange
from schemas.membership import MembershipEvent

from .groups import event_time

log = logging.getLogger("activity")


class MembershipTracker:
    def __init__(self, client, groups, tracker, memberships, bot_id):
        self.client, self.groups, self.tracker, self.memberships, self.bot_id = (
            client,
            groups,
            tracker,
            memberships,
            bot_id,
        )
        self.slots = asyncio.Semaphore(8)
        self.profiles = getattr(memberships, "profile_seen", OrderedDict())

    async def observe_private(self, event):
        user_id = getattr(event, "sender_id", None)
        if (
            type(user_id) is not int
            or not 0 < user_id < 2**63
            or getattr(event, "chat_id", None) != user_id
            or self.profiles.get(user_id, 0) > time.monotonic()
        ):
            return
        user = await self.human(event.sender_id, event)
        if user is None:
            return
        await self.memberships.profile(user.id, user.username, user.first_name, user.last_name)
        self.profiles[user.id] = time.monotonic() + 60
        self.profiles.move_to_end(user.id)
        if len(self.profiles) > 10000:
            self.profiles.popitem(last=False)

    async def human(self, user_id, event=None):
        if type(user_id) is not int or not 0 < user_id < 2**63 or user_id == self.bot_id:
            return None
        entity = getattr(event, "sender", None)
        if entity is not None and not isinstance(entity, types.User):
            return None
        if entity is not None and entity.id != user_id:
            entity = None
        if entity is None:
            for item in getattr(event, "_entities", {}).values():
                if isinstance(item, types.User) and item.id == user_id:
                    entity = item
                    break
        if entity is None:
            try:
                async with asyncio.timeout(3), self.slots:
                    resolver = getattr(event, "get_sender", None)
                    entity = await resolver() if callable(resolver) else None
                    if entity is None:
                        entity = await self.client.get_entity(user_id)
            except (errors.RPCError, ValueError, OSError, TimeoutError, AttributeError):
                return None
        return (
            entity
            if isinstance(entity, types.User)
            and entity.id == user_id
            and not entity.bot
            and not entity.deleted
            else None
        )

    async def observe(self, event):
        if not getattr(event, "is_group", False):
            return
        user = await self.human(getattr(event, "sender_id", None), event)
        if user is None:
            return
        group = await self.known_group(event.chat_id)
        if group is None:
            if not await self.ensure_group(event.chat_id, event):
                return
            group = await self.known_group(event.chat_id)
        await self.memberships.observe(
            MembershipEvent(
                event.chat_id,
                user.id,
                f"message:{event.id}",
                "message",
                event_time(getattr(event, "date", None)),
                event.id,
                user.username,
                user.first_name,
                user.last_name,
                observed_admin=getattr(group, "is_admin", None),
            )
        )

    async def handle_update(self, update):
        if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            await self.service_update(update)
            return
        if isinstance(update, types.UpdateChannel):
            return
        user_id = getattr(update, "user_id", None)
        if user_id == self.bot_id:
            return
        if isinstance(update, (types.UpdateChannelParticipant, types.UpdateChatParticipant)):
            chat_id = (
                -update.chat_id
                if isinstance(update, types.UpdateChatParticipant)
                else utils.get_peer_id(types.PeerChannel(update.channel_id))
            )
            active, _, _ = participant_standing(update.new_participant)
            previous, _, _ = participant_standing(update.prev_participant)
            if active is None:
                return
            if previous == active:
                return
            sequence = update.qts
        elif isinstance(
            update, (types.UpdateChatParticipantAdd, types.UpdateChatParticipantDelete)
        ):
            chat_id = -update.chat_id
            active = isinstance(update, types.UpdateChatParticipantAdd)
            sequence = update.version
        else:
            return
        user = await self.member_user(chat_id, user_id, update, leaving=not active)
        if user is None:
            return
        if active and not await self.ensure_group(chat_id, update):
            return
        key = hashlib.sha256(bytes(update)).hexdigest()
        await self.memberships.observe(
            MembershipEvent(
                chat_id,
                user_id,
                "participant:" + key,
                "joined" if active else "left",
                event_time(getattr(update, "date", None)),
                sequence,
                user.username,
                user.first_name,
                user.last_name,
                "participant"
                if isinstance(update, (types.UpdateChannelParticipant, types.UpdateChatParticipant))
                else "basic",
            )
        )

    async def member_user(self, chat_id, identity, update, *, leaving=False):
        if leaving:
            if type(identity) is not int or identity <= 0 or identity == self.bot_id:
                return None
            # SQL validates known humans; an unknown departure only records a fence.
            # Do not resolve a departed user or require SQL before durable queuing.
            return SimpleNamespace(id=identity, username=None, first_name=None, last_name=None)
        return await self.human(identity, update)

    async def known_group(self, chat_id):
        try:
            return await self.groups.get(chat_id)
        except Exception as error:
            if not (
                isinstance(error, (OSError, TimeoutError))
                or type(error).__module__.startswith("asyncpg")
            ):
                raise
            log.warning({"event": "membership_parent_lookup_failed", "chat_id": chat_id})
            return None

    async def ensure_group(self, chat_id, update):
        if await self.known_group(chat_id) is not None:
            return True
        entity = getattr(update, "chat", None)
        if entity is None:
            entity = await self.tracker._entity(chat_id, update)
        if entity is None:
            peer = getattr(getattr(update, "message", None), "peer_id", None)
            basic = (isinstance(peer, types.PeerChat) and -peer.chat_id == chat_id) or (
                isinstance(
                    update, (types.UpdateChatParticipantAdd, types.UpdateChatParticipantDelete)
                )
                and -update.chat_id == chat_id
            )
            if basic:
                key = hashlib.sha256(bytes(update)).hexdigest()
                await self.groups.record(
                    GroupChange(
                        chat_id,
                        "observed",
                        "member-parent:" + key,
                        event_time(getattr(update, "date", None)),
                        "group",
                    )
                )
                return True
        if not group_kind(entity) or utils.get_peer_id(entity) != chat_id:
            return False
        await self.tracker.observe(
            SimpleNamespace(
                is_group=True,
                chat_id=chat_id,
                chat=entity,
                id=getattr(update, "id", 0),
                date=event_time(getattr(update, "date", None)),
            ),
            force=True,
        )
        # observe either commits or durably defers this verified group. Submit the
        # member too; the service retains it until its parent transaction commits.
        return True

    async def service_update(self, update):
        message = update.message
        if not isinstance(message, types.MessageService) or not isinstance(
            message.peer_id, (types.PeerChat, types.PeerChannel)
        ):
            return
        action = message.action
        if isinstance(action, (types.MessageActionChatAddUser, types.MessageActionChatCreate)):
            users, kind = action.users, "joined"
        elif isinstance(action, types.MessageActionChatDeleteUser):
            users, kind = [action.user_id], "left"
        elif isinstance(
            action, (types.MessageActionChatJoinedByLink, types.MessageActionChatJoinedByRequest)
        ):
            users, kind = [human_id(message.from_id)], "joined"
        else:
            return
        chat_id = utils.get_peer_id(message.peer_id)
        if kind != "left" and not await self.ensure_group(chat_id, update):
            return
        for identity in dict.fromkeys(users):
            user = await self.member_user(chat_id, identity, update, leaving=kind == "left")
            if user is None:
                continue
            await self.memberships.observe(
                MembershipEvent(
                    chat_id,
                    user.id,
                    f"service:{message.id}:{user.id}:{kind}",
                    kind,
                    event_time(message.date),
                    message.id,
                    user.username,
                    user.first_name,
                    user.last_name,
                    "service",
                )
            )
