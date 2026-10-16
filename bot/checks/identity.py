from telethon import types


def has_user_context(event) -> bool:
    user_id, chat_id = getattr(event, "sender_id", None), getattr(event, "chat_id", None)
    sender = getattr(event, "sender", None)
    if isinstance(sender, types.User) and (sender.id != user_id or sender.bot or sender.deleted):
        return False
    if isinstance(sender, (types.Chat, types.ChatForbidden, types.Channel, types.ChannelForbidden)):
        return False
    broadcast = getattr(event, "is_channel", False) and getattr(event, "is_group", None) is False
    return (
        type(user_id) is int
        and 0 < user_id < 2**63
        and type(chat_id) is int
        and -(2**63) < chat_id < 2**63
        and chat_id != 0
        and not broadcast
    )
