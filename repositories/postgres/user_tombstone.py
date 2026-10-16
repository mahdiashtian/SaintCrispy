"""Definitive Telegram account evidence survives deletion and stale replay."""


async def remove_tombstoned(connection, bot_id, identities):
    rows = await connection.fetch(
        """DELETE FROM bot_users u USING user_account_tombstones t
    WHERE u.telegram_account_id=$1 AND u.user_id=ANY($2::bigint[]) AND NOT u.is_admin
    AND t.telegram_account_id=u.telegram_account_id AND t.user_id=u.user_id RETURNING u.user_id""",
        bot_id,
        list(identities),
    )
    return {row["user_id"] for row in rows}
