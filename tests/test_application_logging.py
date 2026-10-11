"""Activity correlation and safe library diagnostics use separate bounded streams."""

import json
import logging
from types import SimpleNamespace

from downloader_bot.core.log_writer import DailyFileHandler, JsonLogWriter
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


def test_structured_logs_redact_nested_credentials_and_signed_urls():
    records = []
    writer = SimpleNamespace(emit=records.append)
    handler = SafeQueueHandler(dict.fromkeys(("system", "telegram", "activity"), writer))
    handler.emit(
        logging.makeLogRecord(
            {
                "name": "activity.menu",
                "msg": {
                    "event": "failed",
                    "details": {"cookie": "SECRET", "proxy": "http://SECRET@host"},
                    "source": "https://cdn.example/path?token=SECRET",
                    "error_code": "instagram_empty_media",
                },
            }
        )
    )
    assert "SECRET" not in json.dumps(records)
    assert records[0]["error_code"] == "instagram_empty_media"


def test_dated_logs_rotate_on_day_and_size_and_keep_bounded_archives(tmp_path):
    from datetime import date

    path = tmp_path / "system.log"
    handler = DailyFileHandler(path, maxBytes=100, backupCount=2, encoding="utf-8")
    handler.handle(logging.makeLogRecord({"msg": "day one"}))
    handler.day = date(2000, 1, 1)
    handler.handle(logging.makeLogRecord({"msg": "day two"}))
    assert list(tmp_path.glob("system.log.2000-01-01.*"))
    for _ in range(5):
        handler.handle(logging.makeLogRecord({"msg": "x" * 60}))
    handler.close()
    assert len(list(tmp_path.glob("system.log.*"))) == 2


def test_application_log_is_readable_and_preserves_persian(tmp_path):
    path = tmp_path / "activity.log"
    writer = JsonLogWriter(str(path), stdout=False, readable=True, daily=True)
    writer.start()
    writer.emit(
        {
            "timestamp": "2026-10-12T01:52:00.123+00:00",
            "level": "INFO",
            "logger": "activity",
            "event": "home_action",
            "action": "راهنما",
        }
    )
    writer.close()
    line = path.read_text(encoding="utf-8")
    assert "01:52:00.123 | INFO" in line and "راهنما" in line and "home_action" in line
    assert writer.dropped == writer.write_errors == 0
