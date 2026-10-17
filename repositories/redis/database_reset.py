"""Delete only known application namespaces for bot IDs in a SQL reset manifest."""

import uuid


async def persist_deletions(client):
    persistence = await client.info("persistence")
    if persistence.get("aof_enabled"):
        for _ in range(3):
            local, _replicas = await client.execute_command("WAITAOF", 1, 0, 1000)
            if local == 1:
                return
        raise TimeoutError("Redis did not confirm durable database reset deletion")
    # An older RDB snapshot must not restore erased queues after a Redis restart.
    if not await client.save():
        raise RuntimeError("Redis did not save database reset deletion")


class ResetRedis:
    def __init__(self, cache=None, state=None):
        self.clients = {"cache": cache, "state": state}

    def required(self):
        return {key: client is not None for key, client in self.clients.items()}

    async def verify(self, required):
        for key, needed in required.items():
            if not needed:
                continue
            client = self.clients[key]
            if client is None:
                raise RuntimeError(
                    f"Restore the configured {key} Redis before completing database reset"
                )
            await client.ping()

    async def purge(self, manifest):
        if (
            manifest.get("version") != 1
            or not isinstance(manifest.get("bot_ids"), list)
            or not manifest["bot_ids"]
            or any(type(value) is not int or not 0 < value < 2**63 for value in manifest["bot_ids"])
            or set(manifest.get("redis_required", {})) != {"cache", "state"}
            or any(type(value) is not bool for value in manifest["redis_required"].values())
        ):
            raise ValueError("Invalid database reset manifest")
        await self.verify(manifest["redis_required"])
        seen = set()
        for client in self.clients.values():
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            # WAITAOF covers writes on this connection, including a recovery retry
            # whose deleted keys no longer exist but may not have been fsynced.
            async with client.client() as pinned:
                for bot_id in manifest["bot_ids"]:
                    for pattern in (
                        f"cache:v3:{bot_id}:*",
                        f"state:v1:{bot_id}:*",
                        f"media:{bot_id}:*",
                        f"request:{bot_id}:*",
                    ):
                        batch = []
                        async for key in pinned.scan_iter(match=pattern, count=200):
                            batch.append(key)
                            if len(batch) == 200:
                                await pinned.unlink(*batch)
                                batch.clear()
                        if batch:
                            await pinned.unlink(*batch)
                fence = f"state:v1:{manifest['bot_ids'][0]}:reset-sync:{uuid.uuid4().hex}"
                await pinned.set(fence, "1", ex=60)
                await pinned.delete(fence)
                await persist_deletions(pinned)
