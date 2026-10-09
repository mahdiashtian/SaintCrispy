"""User, conversation and menu persistence, scoped to the Telegram bot."""

import json
from dataclasses import asdict

from downloader_bot.schemas.media import Media, Quality


def public_media(media: Media) -> dict:
    data = asdict(media)
    data.pop("authorization")
    data["requires_refresh"] = True
    data["thumbnail"] = None
    # The account identifier is a local label, never an authentication secret.
    for quality in data["qualities"]:
        quality["endpoint"] = ""
        quality["fallback_endpoint"] = None
    return data


def read_media(raw: str | dict) -> Media:
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    data["qualities"] = tuple(Quality(**item) for item in data["qualities"])
    return Media(**data)


class WorkflowRepository:
    def __init__(self, pool, bot_id: int):
        self.pool = pool
        self.bot_id = bot_id

    async def touch_user(self, user_id: int, *, started: bool = False) -> None:
        await self.pool.execute(
            """INSERT INTO bot_users (telegram_account_id,user_id,is_started)
            VALUES ($1,$2,$3) ON CONFLICT (telegram_account_id,user_id) DO UPDATE SET
            is_started=bot_users.is_started OR EXCLUDED.is_started, last_seen_at=now()""",
            self.bot_id,
            user_id,
            started,
        )

    async def get_state(self, user_id: int, chat_id: int):
        return await self.pool.fetchrow(
            """SELECT state,data,revision FROM bot_conversations
            WHERE telegram_account_id=$1 AND user_id=$2 AND chat_id=$3""",
            self.bot_id,
            user_id,
            chat_id,
        )

    async def set_state(self, user_id: int, chat_id: int, state: str, data: dict) -> None:
        await self.pool.execute(
            """INSERT INTO bot_conversations
            (telegram_account_id,user_id,chat_id,state,data) VALUES ($1,$2,$3,$4,$5::jsonb)
            ON CONFLICT (telegram_account_id,user_id,chat_id) DO UPDATE SET
            state=EXCLUDED.state,data=EXCLUDED.data,
            revision=bot_conversations.revision+1,updated_at=now()""",
            self.bot_id,
            user_id,
            chat_id,
            state,
            json.dumps(data),
        )

    async def finish_transfer(
        self, user_id: int, chat_id: int, transfer_id: str, state: str
    ) -> None:
        # A previous transfer finishing cannot overwrite a newer link's state.
        await self.pool.execute(
            """UPDATE bot_conversations SET state=$5,data='{}',
            revision=revision+1,updated_at=now() WHERE telegram_account_id=$1
            AND user_id=$2 AND chat_id=$3 AND data->>'transfer_id'=$4""",
            self.bot_id,
            user_id,
            chat_id,
            transfer_id,
            state,
        )

    async def finish_inspection(self, user_id, chat_id, request_id):
        await self.pool.execute(
            """UPDATE bot_conversations SET state='FAILED',data='{}',
            revision=revision+1,updated_at=now() WHERE telegram_account_id=$1
            AND user_id=$2 AND chat_id=$3 AND data->>'request_id'=$4 AND state='INSPECTING'""",
            self.bot_id,
            user_id,
            chat_id,
            request_id,
        )

    async def recover(self) -> int:
        result = await self.pool.execute(
            """UPDATE bot_conversations SET state='INTERRUPTED',
            data='{}',revision=revision+1,updated_at=now()
            WHERE telegram_account_id=$1 AND state IN ('INSPECTING','TRANSFERRING')""",
            self.bot_id,
        )
        return int(result.split()[-1])

    async def save_menu(self, token, owner_id, chat_id, media, message_id=None) -> None:
        await self.pool.execute(
            """INSERT INTO download_menus
            (telegram_account_id,token,user_id,chat_id,metadata,message_id)
            VALUES ($1,$2,$3,$4,$5::jsonb,$6)""",
            self.bot_id,
            token,
            owner_id,
            chat_id,
            json.dumps(public_media(media)),
            message_id,
        )

    async def get_menu(self, token, owner_id, chat_id):
        return await self.pool.fetchrow(
            """SELECT metadata,message_id FROM download_menus
            WHERE telegram_account_id=$1 AND token=$2 AND user_id=$3 AND chat_id=$4""",
            self.bot_id,
            token,
            owner_id,
            chat_id,
        )
