from contextlib import contextmanager
from contextvars import ContextVar

# Context is copied into each asyncio task, including the transfer task started by a callback.
# Provider implementations remain unaware of Telegram users and history storage.
request_user: ContextVar[int | None] = ContextVar("request_user", default=None)
request_chat: ContextVar[int | None] = ContextVar("request_chat", default=None)
request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
request_message: ContextVar[int | None] = ContextVar("request_message", default=None)


@contextmanager
def user_request(
    user_id: int,
    chat_id: int | None = None,
    correlation_id: str | None = None,
    message_id: int | None = None,
):
    token = request_user.set(user_id)
    chat_token = request_chat.set(chat_id)
    id_token = request_id.set(correlation_id)
    message_token = request_message.set(message_id)
    try:
        yield
    finally:
        request_user.reset(token)
        request_chat.reset(chat_token)
        request_id.reset(id_token)
        request_message.reset(message_token)
