"""Central cache/admission key construction; no provider URL rules live here."""

import hashlib
import json


def media_key(namespace: str, site: str, content_id: str, quality: str) -> str:
    identity = json.dumps([site, content_id, quality], separators=(",", ":"))
    return namespace + hashlib.sha256(identity.encode()).hexdigest()


def admission_key(bot_id: int, user_id: int, scope: str, interval: int) -> str:
    return f"request:{bot_id}:{user_id}:{scope}:{interval}"
