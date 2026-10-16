"""Normalized source-compatible channel/watch storage on a private connection."""

import json


class ChannelRepository:
    def __init__(self, connection, bot_id):
        self.connection, self.bot_id = connection, bot_id

    async def get(self):
        snapshot = await self.connection.fetchrow(
            """SELECT (SELECT COALESCE(jsonb_agg(c ORDER BY id),'[]') FROM channels c WHERE telegram_account_id=$1) channels,
            (SELECT COALESCE(jsonb_agg(w ORDER BY chat_id),'[]') FROM group_watches w WHERE telegram_account_id=$1) watches""",
            self.bot_id,
        )
        rows, watches = json.loads(snapshot["channels"]), json.loads(snapshot["watches"])

        def channel(row):
            return {
                "id": row["channel_telegram_id"],
                "link": row["channel_link"],
                "title": row["channel_title"] or "",
            }

        return {
            "forced_channels": [channel(row) for row in rows if row["is_mandatory"]],
            "archive": next((channel(row) for row in rows if row["is_archive"]), None),
            "storage": next((channel(row) for row in rows if row["is_storage"]), None),
            "watches": [
                {
                    "chat_id": row["chat_id"],
                    "channel": channel(row),
                    "kinds": row["kinds"],
                }
                for row in watches
            ],
        }

    async def replace(self, value):
        # The caller holds the account-scoped configuration advisory lock.
        previous = await self.get()
        combined = {}
        for flag, items in (
            ("mandatory", value.get("forced_channels", [])),
            ("archive", [value.get("archive")]),
            ("storage", [value.get("storage")]),
        ):
            for item in items:
                if item:
                    row = combined.setdefault(
                        item["id"], {**item, "mandatory": False, "archive": False, "storage": False}
                    )
                    row[flag] = True
        if any(
            value.get(key) != previous[key] for key in ("forced_channels", "archive", "storage")
        ):
            await self.connection.execute(
                "UPDATE channels SET is_archive=FALSE,is_storage=FALSE WHERE telegram_account_id=$1",
                self.bot_id,
            )
            await self.connection.executemany(
                """INSERT INTO channels(telegram_account_id,channel_telegram_id,channel_link,channel_title,is_mandatory,is_archive,is_storage)
            VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(telegram_account_id,channel_telegram_id) DO UPDATE SET
            channel_link=EXCLUDED.channel_link,channel_title=EXCLUDED.channel_title,is_mandatory=EXCLUDED.is_mandatory,is_archive=EXCLUDED.is_archive,is_storage=EXCLUDED.is_storage""",
                [
                    (
                        self.bot_id,
                        row["id"],
                        row["link"],
                        row.get("title"),
                        row["mandatory"],
                        row["archive"],
                        row["storage"],
                    )
                    for row in combined.values()
                ],
            )
            await self.connection.execute(
                "DELETE FROM channels WHERE telegram_account_id=$1 AND NOT(channel_telegram_id=ANY($2::bigint[]))",
                self.bot_id,
                list(combined),
            )
        watches = value.get("watches", [])
        before = {row["chat_id"]: row for row in previous["watches"]}
        changed = []
        for watch in watches:
            if before.get(watch["chat_id"]) == watch:
                continue
            row = watch["channel"]
            changed.append(
                (
                    self.bot_id,
                    watch["chat_id"],
                    row["id"],
                    row["link"],
                    row.get("title"),
                    json.dumps(watch["kinds"]),
                )
            )
        if changed:
            await self.connection.executemany(
                """INSERT INTO group_watches(telegram_account_id,chat_id,channel_telegram_id,channel_link,channel_title,kinds)
            VALUES($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT(telegram_account_id,chat_id) DO UPDATE SET channel_telegram_id=EXCLUDED.channel_telegram_id,
            channel_link=EXCLUDED.channel_link,channel_title=EXCLUDED.channel_title,kinds=EXCLUDED.kinds,updated_at=now()""",
                changed,
            )
        if set(before) != {row["chat_id"] for row in watches}:
            await self.connection.execute(
                "DELETE FROM group_watches WHERE telegram_account_id=$1 AND NOT(chat_id=ANY($2::bigint[]))",
                self.bot_id,
                [row["chat_id"] for row in watches],
            )
