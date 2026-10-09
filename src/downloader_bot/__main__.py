import asyncio
import json
import signal
import sys
from datetime import UTC, datetime

from downloader_bot.container import Container
from downloader_bot.core.config import Settings
from downloader_bot.services.observability import error_fields


async def main() -> None:
    async with Container(Settings.from_environment()) as container:
        await container.client.run_until_disconnected()


async def run_application():
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    signal_installed = False
    try:
        loop.add_signal_handler(
            signal.SIGTERM, lambda: task.cancel() if not task.cancelling() else None
        )
        signal_installed = True
    except NotImplementedError:
        # asyncio's runner still handles Ctrl+C on Windows.
        pass
    try:
        await main()
    finally:
        if signal_installed:
            loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    try:
        asyncio.run(run_application())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as error:
        print(
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "event": "startup_or_runtime_failure",
                    **error_fields(error),
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
