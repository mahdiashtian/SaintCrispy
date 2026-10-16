"""One versioned publication budget and FloodWait pause per Telegram bot."""


class TelegramRateRepository:
    def __init__(self, redis, bot_id):
        self.redis, self.key = redis, f"state:v1:{bot_id}:telegram-rate"

    async def reserve(self, interval):
        script = """local t=redis.call('TIME');local now=t[1]*1000+math.floor(t[2]/1000)
        local ready=math.max(tonumber(redis.call('HGET',KEYS[1],'next') or 0),tonumber(redis.call('HGET',KEYS[1],'pause') or 0))
        if ready>now then return ready-now end
        redis.call('HSET',KEYS[1],'next',now+tonumber(ARGV[1]));redis.call('PEXPIRE',KEYS[1],86400000)
        return 0"""
        return (await self.redis.eval(script, 1, self.key, interval * 1000)) / 1000

    async def pause(self, seconds):
        script = """local t=redis.call('TIME');local until_at=t[1]*1000+math.floor(t[2]/1000)+tonumber(ARGV[1])
        redis.call('HSET',KEYS[1],'pause',math.max(until_at,tonumber(redis.call('HGET',KEYS[1],'pause') or 0)))
        redis.call('PEXPIRE',KEYS[1],172800000)"""
        await self.redis.eval(script, 1, self.key, seconds * 1000)
