"""Bounded, side-effect-free current-self membership queries."""

import asyncio

from telethon import errors, functions, types, utils

from bot.checks.group import entity_can_send, group_kind, participant_standing
from schemas.group import GroupStanding, Standing


def participant_result(participant, entity) -> GroupStanding:
    # An absent field in a successful lookup is not an explicit leave update.
    if participant is None:
        return GroupStanding(Standing.UNKNOWN, reason="incomplete_response")
    active, admin, can_send = participant_standing(participant)
    if active is None:
        return GroupStanding(Standing.UNKNOWN, reason="unknown_participant")
    if not active:
        return GroupStanding(Standing.GONE, False)
    if can_send is None:
        can_send = entity_can_send(entity, admin)
    return GroupStanding(Standing.ADMIN if admin else Standing.MEMBER, can_send)


class GroupProbe:
    """Never write storage or interpret lookup/network failures as a removal."""

    def __init__(self, client, bot_id: int, *, timeout=3, concurrency=8):
        self.client, self.bot_id = client, bot_id
        self.timeout = timeout
        self._slots = asyncio.Semaphore(concurrency)

    async def check(self, entity, chat_id: int) -> GroupStanding:
        if (
            type(chat_id) is not int
            or chat_id >= 0
            or group_kind(entity) is None
            or utils.get_peer_id(entity) != chat_id
        ):
            return GroupStanding(Standing.UNKNOWN, reason="unverified_group")
        if isinstance(entity, types.ChatForbidden):
            return GroupStanding(Standing.GONE, False)
        if isinstance(entity, types.ChannelForbidden):
            return GroupStanding(Standing.UNKNOWN, reason="channel_inaccessible")
        if not callable(getattr(self.client, "get_permissions", None)):
            return GroupStanding(Standing.UNKNOWN, reason="lookup_unavailable")
        try:
            async with asyncio.timeout(self.timeout), self._slots:
                try:
                    permission = await self.client.get_permissions(entity, self.bot_id)
                except ValueError:
                    return await self._self_participant(entity)
                result = participant_result(getattr(permission, "participant", None), entity)
                if result.standing is Standing.UNKNOWN:
                    return await self._self_participant(entity)
                return result
        except errors.UserNotParticipantError:
            return GroupStanding(Standing.GONE, False)
        except (errors.RPCError, ValueError, OSError, TimeoutError) as error:
            return GroupStanding(Standing.UNKNOWN, reason=type(error).__name__)

    async def _self_participant(self, entity) -> GroupStanding:
        # InputPeerSelf avoids requiring a cached input user for the bot itself.
        # A missing access hash is never replaced by a fabricated zero hash.
        if not callable(self.client):
            return GroupStanding(Standing.UNKNOWN, reason="incomplete_response")
        if isinstance(entity, types.Channel):
            if entity.min or not entity.access_hash:
                return GroupStanding(Standing.UNKNOWN, reason="missing_access_hash")
            result = await self.client(
                functions.channels.GetParticipantRequest(
                    utils.get_input_channel(entity), types.InputPeerSelf()
                )
            )
            return participant_result(getattr(result, "participant", None), entity)
        if isinstance(entity, types.Chat):
            result = await self.client(functions.messages.GetFullChatRequest(entity.id))
            roster = getattr(getattr(result, "full_chat", None), "participants", None)
            if not isinstance(roster, types.ChatParticipants) or roster.chat_id != entity.id:
                return GroupStanding(Standing.UNKNOWN, reason="incomplete_roster")
            for participant in roster.participants:
                if participant.user_id == self.bot_id:
                    return participant_result(participant, entity)
            return GroupStanding(Standing.GONE, False)
        return GroupStanding(Standing.UNKNOWN, reason="incomplete_response")
