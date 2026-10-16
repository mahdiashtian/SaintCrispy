"""Recoverable activity streams. Never trim unacknowledged source events."""

import json

from redis.exceptions import ResponseError

from .keys import activity_key, invalid_activity_key, lifecycle_key


class ActivityStream:
    topic = "membership"

    def __init__(
        self, redis, bot_id, shards=16, batch_size=2000, reclaim_ms=120000, capacity=100000
    ):
        self.redis, self.bot_id, self.shards = redis, bot_id, shards
        self.batch_size, self.reclaim_ms, self.capacity = batch_size, reclaim_ms, capacity
        self.group = "membership-workers-v1"

    def shard(self, chat_id):
        return abs(chat_id) % self.shards

    def key(self, shard):
        return activity_key(self.bot_id, shard)

    def invalid_key(self, shard):
        return invalid_activity_key(self.bot_id, shard)

    async def append(self, event):
        key = self.key(self.shard(event.chat_id))
        script = """if redis.call('XLEN',KEYS[1]) >= tonumber(ARGV[1]) then return false end
        return redis.call('XADD',KEYS[1],'*','event',ARGV[2])"""
        return await self.redis.eval(script, 1, key, self.capacity, json.dumps(event.dump()))

    async def batch(self, shard, consumer):
        key = self.key(shard)
        try:
            await self.redis.xgroup_create(key, self.group, id="0-0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise
        reclaimed = await self.redis.xautoclaim(
            key, self.group, consumer, self.reclaim_ms, "0-0", count=self.batch_size
        )
        rows = list(reclaimed[1])
        if len(rows) < self.batch_size:
            new = await self.redis.xreadgroup(
                self.group, consumer, {key: ">"}, count=self.batch_size - len(rows)
            )
            for _, items in new:
                rows.extend(items)
        return rows

    async def acknowledge(self, shard, ids):
        if not ids:
            return
        key = self.key(shard)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(key, self.group, *ids)
            pipe.xdel(key, *ids)
            await pipe.execute()

    async def quarantine(self, shard, identity):
        key = self.invalid_key(shard)
        # Do not retain raw poison payloads, which need not obey our no-content contract.
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xadd(
                key,
                {"source_id": identity, "reason": f"invalid_{self.topic}_event"},
                maxlen=1000,
                approximate=False,
            )
            pipe.expire(key, 7 * 86400)
            await pipe.execute()


class LifecycleStream(ActivityStream):
    topic = "lifecycle"

    def key(self, shard):
        return lifecycle_key(self.bot_id, shard)

    def invalid_key(self, shard):
        return lifecycle_key(self.bot_id, shard, invalid=True)
