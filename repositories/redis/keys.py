"""Central cache/admission key construction; no provider URL rules live here."""

import hashlib
import json


def media_key(namespace: str, site: str, content_id: str, quality: str) -> str:
    identity = json.dumps([site, content_id, quality], separators=(",", ":"))
    return namespace + hashlib.sha256(identity.encode()).hexdigest()


def admission_key(bot_id: int, user_id: int, scope: str, interval: int) -> str:
    return f"request:{bot_id}:{user_id}:{scope}:{interval}"


def object_key(bot_id, kind, identity):
    if kind not in {"group", "user", "setting"} or (
        type(identity) is not int
        if kind != "setting"
        else identity not in {"downloads_enabled", "lifecycle_notifications"}
    ):
        raise ValueError("Unknown object namespace")
    return f"cache:v3:{bot_id}:{kind}:{identity}"


def activity_key(bot_id, shard):
    return f"state:v1:{bot_id}:membership:{shard}"


def invalid_activity_key(bot_id, shard):
    return f"state:v1:{bot_id}:membership:invalid:{shard}"


def lifecycle_key(bot_id, shard, *, invalid=False):
    return f"state:v1:{bot_id}:lifecycle:{'invalid:' if invalid else ''}{shard}"
