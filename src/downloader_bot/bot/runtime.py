"""Recover a native coordinator after transient infrastructure disconnections."""

import asyncio
import json
from datetime import UTC, datetime

import asyncpg

from downloader_bot.core.diagnostics import error_fields

TRANSIENT_FAILURES = (
    OSError,
    TimeoutError,
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
)


async def run_with_recovery(container_factory) -> None:
    delay = 2
    while True:
        fields = {}
        try:
            async with container_factory() as container:
                await container.client.run_until_disconnected()
                delay = 2
        except TRANSIENT_FAILURES as error:
            fields = error_fields(error)
        # The prior container has completely cleaned up before a replacement starts.
        print(
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "event": "coordinator_restarting",
                    "wait_seconds": delay,
                    **fields,
                }
            ),
            flush=True,
        )
        await asyncio.sleep(delay)
        delay = min(30, delay * 2)
