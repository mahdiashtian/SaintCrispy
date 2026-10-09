"""Measure URL registration and a bounded Telegram reference read without sending.

Supply the Telegram test credentials through environment variables. This tool
does not download the media to the bot, print signed URLs, or persist sessions.
"""

import argparse
import asyncio
import json
import os
import time
from urllib.parse import urlsplit

from telethon import TelegramClient, errors, functions, types
from telethon.sessions import MemorySession


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="+", help="Public URLs of permitted test media files")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--expected-size", type=int)
    args = parser.parse_args()
    if any(not url.startswith("https://") for url in args.url):
        parser.error("Use an HTTPS test URL")
    if args.expected_size is not None and len(args.url) != 1:
        parser.error("--expected-size requires exactly one URL")
    client = TelegramClient(
        MemorySession(),
        int(os.environ["API_ID"]),
        os.environ["API_HASH"],
        request_retries=1,
        connection_retries=1,
        flood_sleep_threshold=0,
        receive_updates=False,
    )
    try:
        async with asyncio.timeout(60):
            await client.connect()
            await client.sign_in(bot_token=os.environ["BOT_TOKEN"])
    except Exception as error:
        result = {"failed_stage": "login", "error_type": type(error).__name__}
        if isinstance(error, errors.FloodWaitError):
            result["wait_seconds"] = error.seconds
        print(json.dumps(result), flush=True)
        await client.disconnect()
        raise SystemExit(1) from None
    failed = False
    try:
        for index, url in enumerate(args.url, 1):
            result = await probe(client, url, args.timeout, args.expected_size)
            result.update(source_index=index, source_host=urlsplit(url).hostname)
            print(json.dumps(result), flush=True)
            failed |= "error_type" in result
            if result.get("error_type") == "FloodWaitError":
                break
    finally:
        await client.disconnect()
    if failed:
        raise SystemExit(1)


async def probe(client, url: str, max_seconds: float, expected_size: int | None) -> dict:
    stage = "external_url"
    started = time.perf_counter()
    result = {"messages_sent": 0, "local_media_download": False}
    try:
        async with asyncio.timeout(max_seconds):
            registered = await client(
                functions.messages.UploadMediaRequest(
                    peer=types.InputPeerSelf(),
                    media=types.InputMediaDocumentExternal(url),
                )
            )
            elapsed = time.perf_counter() - started
            document = getattr(registered, "document", None)
            if document is None:
                raise RuntimeError("Telegram did not return a document")
            result.update(
                external_accepted=True,
                external_seconds=round(elapsed, 3),
                document_size=document.size,
                document_mime=document.mime_type,
                reference_present=bool(document.file_reference),
            )
            if expected_size is not None:
                result["expected_size_matches"] = document.size == expected_size
            stage = "reference_read"
            started = time.perf_counter()
            iterator = client.iter_download(document, request_size=4096, limit=1)
            received = 0
            try:
                async for chunk in iterator:
                    received += len(chunk)
            finally:
                await iterator.close()
            result.update(
                reference_read_seconds=round(time.perf_counter() - started, 3),
                reference_read_bytes=received,
                reference_read_ok=received > 0,
            )
    except Exception as error:
        # Never log exception repr: it may contain a signed URL or credentials.
        result.update(
            failed_stage=stage,
            error_type=type(error).__name__,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )
        if isinstance(error, errors.FloodWaitError):
            result["wait_seconds"] = error.seconds
    return result


if __name__ == "__main__":
    asyncio.run(main())
