"""Strict cache payload validation; invalid cache objects fall back to SQL."""

import json


def read_object(raw: bytes | str, expected_fields: set[str]) -> dict:
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != expected_fields:
        raise ValueError("Invalid cache object fields")
    return data
