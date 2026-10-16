"""Record real humans on all group messages and complementary raw membership updates."""

import asyncio
import hashlib
import time
from collections import OrderedDict
from types import SimpleNamespace

from telethon import errors, types, utils

from bot.checks.group import group_kind, human_id, participant_standing
from schemas.membership import MembershipEvent

from .groups import event_time


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
        self.profiles = OrderedDict()

    async def observe_private(self, event):
        if not isinstance(getattr(event, "sender", None), types.User):
            return
        if (
            getattr(event, "chat_id", None) != getattr(event, "sender_id", None)
            or self.profiles.get(event.sender_id, 0) > time.monotonic()
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
        if type(user_id) is not int or user_id <= 0 or user_id == self.bot_id:
            return None
        entity = getattr(event, "sender", None)
        if entity is None:
            for item in getattr(event, "_entities", {}).values():
                if isinstance(item, types.User) and item.id == user_id:
                    entity = item
                    break
        if entity is None:
            try:
                async with asyncio.timeout(3), self.slots:
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
        if user is None or await self.groups.get(event.chat_id) is None:
            return
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
        if isinstance(update, types.UpdateChannelParticipant):
            chat_id = utils.get_peer_id(types.PeerChannel(update.channel_id))
            active, _, _ = participant_standing(update.new_participant)
            if active is None:
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
        user = await self.human(user_id, update)
        if user is None:
            return
        if not await self.ensure_group(chat_id, update):
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
                "participant" if isinstance(update, types.UpdateChannelParticipant) else "basic",
            )
        )

    async def ensure_group(self, chat_id, update):
        if await self.groups.get(chat_id) is not None:
            return True
        entity = await self.tracker._entity(chat_id, update)
        if not group_kind(entity):
            return False
        await self.tracker.observe(
            SimpleNamespace(
                is_group=True,
                chat_id=chat_id,
                chat=entity,
                id=0,
                date=event_time(getattr(update, "date", None)),
            )
        )
        return await self.groups.get(chat_id) is not None

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
        if not await self.ensure_group(chat_id, update):
            return
        for identity in dict.fromkeys(users):
            user = await self.human(identity, update)
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
