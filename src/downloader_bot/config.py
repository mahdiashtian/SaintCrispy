import os
from dataclasses import dataclass, field


class ConfigurationError(RuntimeError):
    """Identify setting failures without including values in diagnostics."""

    def __init__(self, message: str, *, code: str, fields: tuple[str, ...]):
        super().__init__(message)
        self.code = code
        self.fields = fields


def integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise ConfigurationError(
            f"{name} must be an integer", code="invalid_integer", fields=(name,)
        ) from error
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{name} must be between {minimum} and {maximum}",
            code="out_of_range",
            fields=(name,),
        )
    return value


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str = field(repr=False)
    bot_token: str = field(repr=False)
    database_url: str = field(repr=False)
    redis_url: str | None = field(repr=False)
    ffmpeg: str
    storage_chat: str | None
    concurrency: int
    max_requests: int = 1000
    cached_concurrency: int = 1000
    request_interval: int = 60
    max_file_bytes: int = 1024 * 1024 * 1024
    metadata_concurrency: int = 8
    remux_concurrency: int = 2
    upload_parallelism: int = 4
    upload_inflight_parts: int = 64
    transfer_timeout: int = 3600
    sends_per_second: int = 0
    log_file: str | None = "logs/performance.jsonl"
    log_max_bytes: int = 20 * 1024 * 1024
    log_backups: int = 10
    log_queue_size: int = 10000
    log_stdout: bool = True
    metrics_interval: int = 30
    metrics_network_interface: str = ""

    @classmethod
    def from_environment(cls):
        required = ("API_ID", "API_HASH", "BOT_TOKEN", "DATABASE_URL")
        missing = tuple(name for name in required if not os.environ.get(name, "").strip())
        if missing:
            raise ConfigurationError(
                "Missing environment variables: " + ", ".join(missing),
                code="missing_required_settings",
                fields=missing,
            )
        requests = integer_setting("MAX_CONCURRENT_REQUESTS", 1000, 1, 10000)
        return cls(
            api_id=integer_setting("API_ID", 0, 1, 2**31 - 1),
            api_hash=os.environ["API_HASH"],
            bot_token=os.environ["BOT_TOKEN"],
            database_url=os.environ["DATABASE_URL"],
            redis_url=os.environ.get("REDIS_URL") or None,
            ffmpeg=os.environ.get("FFMPEG_PATH") or "ffmpeg",
            storage_chat=os.environ.get("MEDIA_STORAGE_CHAT") or None,
            concurrency=integer_setting("TRANSFER_CONCURRENCY", requests, 1, requests),
            max_requests=requests,
            cached_concurrency=integer_setting(
                "CACHED_TRANSFER_CONCURRENCY", requests, 1, requests
            ),
            request_interval=integer_setting("USER_REQUEST_INTERVAL_SECONDS", 60, 1, 86400),
            # Reserve a final 512 KiB part below the uploader's 4000-part bound.
            max_file_bytes=integer_setting("MAX_FILE_SIZE_MB", 1024, 1, 1999) * 1024 * 1024,
            metadata_concurrency=integer_setting("METADATA_CONCURRENCY", 8, 1, 64),
            remux_concurrency=integer_setting("REMUX_CONCURRENCY", 2, 1, requests),
            upload_parallelism=integer_setting("UPLOAD_PARALLELISM", 4, 1, 8),
            upload_inflight_parts=integer_setting("UPLOAD_INFLIGHT_PARTS", 64, 1, 1024),
            transfer_timeout=integer_setting("TRANSFER_TIMEOUT_SECONDS", 3600, 60, 86400),
            sends_per_second=integer_setting("TELEGRAM_SENDS_PER_SECOND", 0, 0, 1000),
            log_file=os.environ.get("LOG_FILE", "logs/performance.jsonl") or None,
            log_max_bytes=integer_setting("LOG_MAX_MB", 20, 1, 1024) * 1024**2,
            log_backups=integer_setting("LOG_BACKUP_COUNT", 10, 1, 100),
            log_queue_size=integer_setting("LOG_QUEUE_SIZE", 10000, 100, 100000),
            log_stdout=bool(integer_setting("LOG_STDOUT", 1, 0, 1)),
            metrics_interval=integer_setting("METRICS_INTERVAL_SECONDS", 30, 1, 3600),
            metrics_network_interface=os.environ.get("METRICS_NETWORK_INTERFACE", ""),
        )
