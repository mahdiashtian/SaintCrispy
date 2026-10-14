def has_user_context(event) -> bool:
    user_id, chat_id = getattr(event, "sender_id", None), getattr(event, "chat_id", None)
    return type(user_id) is int and user_id > 0 and type(chat_id) is int and chat_id != 0
