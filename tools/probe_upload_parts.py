import asyncio
import json
import os

from telethon import TelegramClient, functions, helpers
from telethon.sessions import MemorySession


async def main():
    client = TelegramClient(
        MemorySession(),
        int(os.environ["API_ID"]),
        os.environ["API_HASH"],
        request_retries=2,
        connection_retries=1,
        flood_sleep_threshold=0,
    )
    try:
        await client.connect()
        await client.sign_in(bot_token=os.environ["BOT_TOKEN"])
        for total in (1, -1):
            request = functions.upload.SaveBigFilePartRequest(
                helpers.generate_random_long(),
                0,
                total,
                b"x" * (512 * 1024),
            )
            try:
                async with asyncio.timeout(25):
                    result = await client(request)
                print(json.dumps({"total_parts": total, "result": result}), flush=True)
            except Exception as error:
                print(
                    json.dumps({"total_parts": total, "error_type": type(error).__name__}),
                    flush=True,
                )
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
