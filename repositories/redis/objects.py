"""Strict evictable object cache. Authorization and decisions use fresh SQL."""

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime

from redis.exceptions import RedisError, ResponseError

from core.diagnostics import error_fields
from core.locks import KeyedLocks
from schemas.admin import AdminUser, SettingSnapshot
from schemas.group import GroupSnapshot

from .keys import object_key

log = logging.getLogger("system")


class ObjectCache:
    def __init__(self, redis, bot_id, ttl=300):
        self.redis, self.bot_id, self.ttl = redis, bot_id, ttl
        self.retry_at = 0.0
        self.locks = KeyedLocks()
        self.dirty = OrderedDict()
        self.slots = asyncio.Semaphore(16)
        self.versions = OrderedDict()

    async def read(self, kind, identity, loader):
        key = object_key(self.bot_id, kind, identity)
        if self.redis is None or time.monotonic() < self.retry_at:
            return await loader()
        async with self.locks.hold(key):
            repair = False
            try:
                async with self.slots:
                    raw = None if key in self.dirty else await self.redis.get(key)
                if raw is not None:
                    return self.decode(raw, kind, identity)
            except ResponseError as error:
                if "WRONGTYPE" not in str(error):
                    self.retry_at = time.monotonic() + 5
                    return await loader()
                repair = True
            except RedisError as error:
                self.retry_at = time.monotonic() + 5
                log.warning({"event": "object_cache_read_failed", **error_fields(error)})
                return await loader()
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                repair = True
            version = self.versions.get(key, 0)
            result = await loader()
            if version == self.versions.get(key, 0):
                await self.put(kind, identity, result, advance=False, repair=repair)
            return result

    def decode(self, raw, kind, identity):
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or data.get("kind") != kind
            or data.get("identity") != identity
            or type(data.get("identity")) is not type(identity)
            or data.get("bot_id") != self.bot_id
            or type(data.get("version")) is not int
            or type(data.get("bot_id")) is not int
            or set(data) != {"version", "kind", "identity", "bot_id", "value"}
        ):
            raise ValueError("Invalid cache envelope")
        value = data["value"]
        if value is None:
            return None
        if kind == "user":
            if (
                not isinstance(value, dict)
                or set(value) != set(AdminUser.__dataclass_fields__)
                or type(value["user_id"]) is not int
                or value["user_id"] != identity
            ):
                raise ValueError("Invalid user snapshot")
            for key in ("is_started", "is_admin", "is_banned", "is_blocked"):
                if type(value[key]) is not bool:
                    raise ValueError("Invalid user flag")
            if value["account_kind"] not in {"real", "bot", "deleted", "unknown"}:
                raise ValueError("Invalid account kind")
            for key in ("username", "first_name"):
                if value[key] is not None and not isinstance(value[key], str):
                    raise ValueError("Invalid user name")
            value["last_seen_at"] = datetime.fromisoformat(value["last_seen_at"])
            if value["last_seen_at"].tzinfo is None:
                raise ValueError("Invalid user time")
            return AdminUser(**value)
        if kind == "setting":
            if (
                not isinstance(value, dict)
                or set(value) != {"key", "value"}
                or value["key"] != identity
                or type(value["value"]) is not bool
            ):
                raise ValueError("Invalid setting snapshot")
            return SettingSnapshot(**value)
        if kind == "group":
            if not isinstance(value, dict) or set(value) != set(GroupSnapshot.__dataclass_fields__):
                raise ValueError("Incomplete group snapshot")
            if (
                type(value["chat_id"]) is not int
                or value["chat_id"] != identity
                or value["chat_kind"] not in {"group", "supergroup"}
            ):
                raise ValueError("Wrong group identity")
            for field in ("is_active", "is_admin", "can_send"):
                if value[field] is not None and type(value[field]) is not bool:
                    raise ValueError("Invalid group flag")
            for field in (
                "added_by_user_id",
                "owner_id",
                "member_count",
                "migrated_to",
                "links_per_minute",
            ):
                if value[field] is not None and type(value[field]) is not int:
                    raise ValueError("Invalid group number")
            if not isinstance(value["admin_ids"], list) or any(
                type(i) is not int or i <= 0 for i in value["admin_ids"]
            ):
                raise ValueError("Invalid administrators")
            for field in ("title", "username", "link"):
                if value[field] is not None and not isinstance(value[field], str):
                    raise ValueError("Invalid group text")
            for field in (
                "added_at",
                "readded_at",
                "removed_at",
                "admin_since",
                "last_seen_at",
                "last_event_at",
                "last_activity_at",
            ):
                value[field] = (
                    datetime.fromisoformat(value[field]) if value[field] is not None else None
                )
                if value[field] is not None and value[field].tzinfo is None:
                    raise ValueError("Invalid group time")
            if value["last_seen_at"] is None or value["last_event_at"] is None:
                raise ValueError("Missing group time")
            if type(value["revision"]) is not int or value["revision"] < 0:
                raise ValueError("Invalid group revision")
            if value["member_count"] is not None and value["member_count"] < 0:
                raise ValueError("Invalid member count")
            if value["links_per_minute"] is not None and not 1 <= value["links_per_minute"] <= 1000:
                raise ValueError("Invalid group quota")
            if any(
                value[key] is not None and value[key] <= 0
                for key in ("owner_id", "added_by_user_id")
            ) or (value["migrated_to"] is not None and value["migrated_to"] >= 0):
                raise ValueError("Invalid group relationship")
            value["admin_ids"] = tuple(value["admin_ids"])
            return GroupSnapshot(**value)
        raise ValueError("Unknown object kind")

    async def put(self, kind, identity, value, *, advance=True, repair=False):
        key = object_key(self.bot_id, kind, identity)
        self.mark_dirty(key, advance=advance)
        if self.redis is None or time.monotonic() < self.retry_at:
            return
        data = asdict(value) if value is not None else None
        raw = json.dumps(
            {
                "version": 1,
                "kind": kind,
                "identity": identity,
                "bot_id": self.bot_id,
                "value": data,
            },
            default=lambda value: value.isoformat(),
        )
        try:
            async with self.slots:
                script = """local old=nil
                if redis.call('TYPE',KEYS[1]).ok=='string' then
                    local ok,obj=pcall(cjson.decode,redis.call('GET',KEYS[1]))
                    if ok then old=obj end
                end
                local incoming=cjson.decode(ARGV[1])
                if ARGV[3]=='0' and type(old)=='table' and old.bot_id==incoming.bot_id and
                    old.kind==incoming.kind and old.identity==incoming.identity and
                    incoming.kind=='group' and type(old.value)=='table' and
                    type(incoming.value)=='table' and type(old.value.revision)=='number'
                    and old.value.revision>incoming.value.revision then return 0 end
                redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2]);return 1"""
                await self.redis.eval(script, 1, key, raw, self.ttl, int(repair))
            self.dirty.pop(key, None)
        except RedisError as error:
            self.mark_dirty(key)
            self.retry_at = time.monotonic() + 5
            log.warning({"event": "object_cache_write_failed", **error_fields(error)})

    def mark_dirty(self, key, *, advance=True):
        if advance:
            self.versions[key] = self.versions.get(key, 0) + 1
            self.versions.move_to_end(key)
            if len(self.versions) > 10000:
                self.versions.popitem(last=False)
        self.dirty[key] = True
        self.dirty.move_to_end(key)
        if len(self.dirty) > 10000:
            self.dirty.popitem(last=False)

    async def invalidate(self, kind, identity):
        key = object_key(self.bot_id, kind, identity)
        self.mark_dirty(key)
        if self.redis is None:
            return
        try:
            await self.redis.delete(key)
            self.dirty.pop(key, None)
        except RedisError:
            self.retry_at = time.monotonic() + 5
