"""Bounded Redis pools; cache and state can use separate endpoints."""

from redis.asyncio import BlockingConnectionPool, Redis


def create_redis(url: str) -> Redis:
    pool = BlockingConnectionPool.from_url(
        url,
        timeout=2,
        socket_connect_timeout=2,
        socket_timeout=2,
        socket_keepalive=True,
        health_check_interval=30,
        max_connections=16,
    )
    return Redis.from_pool(pool)
