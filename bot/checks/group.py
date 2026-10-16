"""Interpret definite Telegram standing without inventing users or groups."""

from telethon import types


def saved_input_peer(saved):
    """Restore addressing only; saved access never proves current membership."""
    if saved.chat_kind == "group":
        return types.InputPeerChat(saved.raw_id)
    if saved.chat_kind == "supergroup" and saved.access_hash:
        return types.InputPeerChannel(saved.raw_id, saved.access_hash)
    raise ValueError("Missing verified group peer")


def human_id(value):
    if isinstance(value, types.PeerUser):
        value = value.user_id
    return value if type(value) is int and value > 0 else None


def group_kind(entity):
    if isinstance(entity, (types.Chat, types.ChatForbidden)):
        return "group"
    if isinstance(entity, (types.Channel, types.ChannelForbidden)) and entity.megagroup:
        return "supergroup"
    return None


def participant_standing(participant):
    if participant is None or isinstance(participant, types.ChannelParticipantLeft):
        return False, False, False
    if isinstance(participant, types.ChannelParticipantBanned):
        present = not (participant.left or participant.banned_rights.view_messages)
        return (
            present,
            False,
            present
            and not (
                participant.banned_rights.send_messages or participant.banned_rights.send_plain
            ),
        )
    if isinstance(
        participant,
        (
            types.ChannelParticipantAdmin,
            types.ChannelParticipantCreator,
            types.ChatParticipantAdmin,
            types.ChatParticipantCreator,
        ),
    ):
        return True, True, True
    if isinstance(
        participant, (types.ChannelParticipant, types.ChannelParticipantSelf, types.ChatParticipant)
    ):
        return True, False, None
    return None, None, None


def entity_can_send(entity, admin):
    if admin:
        return True
    if getattr(entity, "min", False):
        return None
    rights = getattr(entity, "banned_rights", None) or getattr(
        entity, "default_banned_rights", None
    )
    if rights is not None:
        return not (rights.view_messages or rights.send_messages or rights.send_plain)
    return None
