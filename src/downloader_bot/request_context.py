from contextlib import contextmanager
from contextvars import ContextVar

# Context is copied into each asyncio task, including the transfer task started by a callback.
# Provider implementations remain unaware of Telegram users and history storage.
request_user: ContextVar[int | None] = ContextVar("request_user", default=None)


@contextmanager
def user_request(user_id: int):
    token = request_user.set(user_id)
    try:
        yield
    finally:
        request_user.reset(token)
