"""Credential-safe Telegram error classification for dispatch boundaries."""

from telethon import errors


def telegram_failure(error: errors.RPCError) -> dict:
    if isinstance(error, (errors.FloodWaitError, errors.SlowModeWaitError)):
        return {"reason": "rate_limit", "retry_after_seconds": max(1, error.seconds)}
    if isinstance(error, (errors.UserIsBlockedError, errors.InputUserDeactivatedError)):
        return {"reason": "recipient_unavailable"}
    return {"reason": "rpc_failure"}
