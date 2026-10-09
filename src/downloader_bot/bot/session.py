"""Telegram network and login recovery."""

import asyncio

from telethon import errors

from downloader_bot.services.observability import error_fields


async def sign_in_bot(client, bot_token: str, telemetry=None):
    """Honor Telegram's login cooldown without restarting or blocking the event loop."""
    while True:
        try:
            return await client.sign_in(bot_token=bot_token)
        except errors.FloodWaitError as error:
            if telemetry is not None:
                telemetry.emit("telegram_login_wait", wait_seconds=max(1, error.seconds))
            await asyncio.sleep(max(1, error.seconds))


async def connect_bot(client, telemetry=None):
    """Keep the process alive during network outages with a bounded async backoff."""
    delay = 2
    while True:
        try:
            await client.connect()
            return
        except (OSError, TimeoutError) as error:
            if telemetry is not None:
                telemetry.emit(
                    "telegram_connection_retry", wait_seconds=delay, **error_fields(error)
                )
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)


async def catch_up_bot(client, telemetry=None):
    """Recover missed updates after handlers exist; an unavailable catch-up is nonfatal."""
    try:
        async with asyncio.timeout(10):
            await client.catch_up()
    except (errors.RPCError, OSError, TimeoutError) as error:
        if telemetry is not None:
            telemetry.emit("telegram_catch_up_unavailable", **error_fields(error))
