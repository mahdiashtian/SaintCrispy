"""Credential-safe exception identities shared by logging and telemetry."""

import traceback
from collections import deque
from pathlib import Path

from downloader_bot.core.config import ConfigurationError
from downloader_bot.schemas.media import DownloadError


def error_fields(error: BaseException) -> dict:
    """Exception text, request objects, URLs and source lines can contain credentials."""
    frames = deque(traceback.walk_tb(error.__traceback__), maxlen=8)
    result = {
        "error_type": type(error).__name__,
        "error_stack": [
            {
                "file": Path(frame.f_code.co_filename).name,
                "line": line,
                "function": frame.f_code.co_name,
            }
            for frame, line in frames
        ],
    }
    if isinstance(error, ConfigurationError):
        result["configuration_error"] = error.code
        result["configuration_fields"] = list(error.fields)
    if isinstance(error, DownloadError) and error.code:
        result["error_code"] = error.code
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None) or getattr(error, "status", None)
    if isinstance(status, int):
        result["http_status"] = status
    return result
