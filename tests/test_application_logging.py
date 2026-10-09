"""Activity correlation and safe library diagnostics use separate bounded streams."""

import json
import logging
from types import SimpleNamespace

from downloader_bot.core.logging_config import SafeQueueHandler
from downloader_bot.core.request_context import user_request


def test_logging_routes_activity_and_telegram_without_raw_library_secrets():
    outputs = {name: [] for name in ("system", "telegram", "activity")}
    writers = {name: SimpleNamespace(emit=records.append) for name, records in outputs.items()}
    handler = SafeQueueHandler(writers)
    with user_request(10, 20, "request-id"):
        handler.emit(
            logging.makeLogRecord(
                {"name": "activity", "levelname": "INFO", "msg": {"event": "link_received"}}
            )
        )
        handler.emit(
            logging.makeLogRecord(
                {
                    "name": "telethon.network",
                    "levelname": "ERROR",
                    "msg": "https://botTOKEN/path?cookie=SECRET",
                }
            )
        )
    assert outputs["activity"][0]["user_id"] == 10
    assert outputs["activity"][0]["chat_id"] == 20
    assert outputs["activity"][0]["request_id"] == "request-id"
    assert outputs["telegram"] == outputs["system"]
    assert "TOKEN" not in json.dumps(outputs) and "SECRET" not in json.dumps(outputs)
    assert outputs["system"][0]["event"] == "library_log"
