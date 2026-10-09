import asyncio
from types import SimpleNamespace

import pytest
from telethon import errors

from downloader_bot.bot.session import connect_bot, sign_in_bot


async def test_login_retries_only_after_the_server_requested_wait(monkeypatch):
    waits, calls = [], []

    async def sleep(seconds):
        waits.append(seconds)

    async def sign_in(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise errors.FloodWaitError(request=None, capture=37)
        return "authorized"

    monkeypatch.setattr(asyncio, "sleep", sleep)
    result = await sign_in_bot(SimpleNamespace(sign_in=sign_in), "test-token")
    assert result == "authorized"
    assert waits == [37] and calls == [{"bot_token": "test-token"}] * 2


async def test_login_wait_is_cancelled_without_another_authorization_request(monkeypatch):
    waiting, calls = asyncio.Event(), []

    async def sleep(seconds):
        waiting.set()
        await asyncio.Future()

    async def sign_in(**kwargs):
        calls.append(kwargs)
        raise errors.FloodWaitError(request=None, capture=300)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(sign_in_bot(SimpleNamespace(sign_in=sign_in), "test-token"))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1


async def test_connection_retries_with_bounded_async_backoff(monkeypatch):
    waits, calls = [], []

    async def sleep(seconds):
        waits.append(seconds)

    async def connect():
        calls.append(1)
        if len(calls) < 8:
            raise ConnectionError("offline")

    monkeypatch.setattr(asyncio, "sleep", sleep)
    await connect_bot(SimpleNamespace(connect=connect))
    assert waits == [2, 4, 8, 16, 30, 30, 30]


async def test_network_backoff_can_be_cancelled_without_another_connection(monkeypatch):
    waiting = asyncio.Event()
    calls = []

    async def sleep(seconds):
        waiting.set()
        await asyncio.Future()

    async def connect():
        calls.append(1)
        raise TimeoutError()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(connect_bot(SimpleNamespace(connect=connect)))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [1]


async def test_sigterm_cleans_up_the_application_and_removes_its_handler(monkeypatch):
    from downloader_bot import __main__ as application

    callbacks, removed = [], []
    entered, cleaned = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda _, callback: callbacks.append(callback))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))

    async def main():
        try:
            entered.set()
            await asyncio.Future()
        finally:
            cleaned.set()

    monkeypatch.setattr(application, "main", main)
    task = asyncio.create_task(application.run_application())
    await entered.wait()
    callbacks[0]()
    callbacks[0]()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set() and len(removed) == 1
