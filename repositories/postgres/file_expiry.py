"""Bot-scoped file-message deadlines; retries never renew the original lifetime."""


class FileExpiryRepository:
    def __init__(self, pool, bot_id):
        self.pool = pool
        self.bot_id = bot_id

    async def add(self, chat_id, message_id, peer, delete_at):
        await self.pool.execute(
            """INSERT INTO file_message_expiry
              (telegram_account_id,chat_id,message_id,peer,delete_at,next_attempt_at)
              VALUES ($1,$2,$3,$4,$5,$5)
              ON CONFLICT (telegram_account_id,chat_id,message_id) DO NOTHING""",
            self.bot_id,
            chat_id,
            message_id,
            peer,
            delete_at,
        )

    async def due(self, limit=100):
        return await self.pool.fetch(
            """WITH pruned AS (
              DELETE FROM file_message_expiry WHERE (telegram_account_id,chat_id,message_id) IN
                (SELECT telegram_account_id,chat_id,message_id FROM file_message_expiry
                 WHERE telegram_account_id=$1
                 AND completed_at<clock_timestamp()-interval '2 days' LIMIT 100)
              ) SELECT chat_id,message_id,peer,delete_at,next_attempt_at,attempts
              FROM file_message_expiry WHERE telegram_account_id=$1
              AND completed_at IS NULL AND next_attempt_at<=clock_timestamp()
              ORDER BY next_attempt_at,chat_id,message_id LIMIT $2""",
            self.bot_id,
            limit,
        )

    async def finish(self, chat_id, message_id):
        await self.pool.execute(
            """UPDATE file_message_expiry SET completed_at=clock_timestamp()
              WHERE telegram_account_id=$1
              AND chat_id=$2 AND message_id=$3""",
            self.bot_id,
            chat_id,
            message_id,
        )

    async def defer(self, chat_id, message_id, seconds):
        await self.pool.execute(
            """UPDATE file_message_expiry SET attempts=attempts+1,
              next_attempt_at=clock_timestamp()+$4*interval '1 second'
              WHERE telegram_account_id=$1 AND chat_id=$2 AND message_id=$3
              AND completed_at IS NULL""",
            self.bot_id,
            chat_id,
            message_id,
            float(seconds),
        )
