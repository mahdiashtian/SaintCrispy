"""User, conversation and menu persistence, scoped to the Telegram bot."""

import json

from repositories.metadata import public_media as public_media
from schemas.media import Media, Quality


def read_media(raw: str | dict) -> Media:
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    data["qualities"] = tuple(Quality(**item) for item in data["qualities"])
    return Media(**data)


class WorkflowRepository:
    def __init__(self, pool, bot_id: int):
        self.pool = pool
        self.bot_id = bot_id

    async def touch_user(
        self, user_id: int, *, started: bool = False, start_source="plain", start_chat_id=None
    ) -> None:
        await self.pool.execute(
            """INSERT INTO bot_users (telegram_account_id,user_id,is_started,started_at,start_source,start_chat_id)
            VALUES ($1,$2,$3,CASE WHEN $3 THEN now() ELSE NULL END,CASE WHEN $3 THEN $4::text ELSE NULL END,CASE WHEN $3 THEN $5::bigint ELSE NULL END)
            ON CONFLICT (telegram_account_id,user_id) DO UPDATE SET
            is_started=bot_users.is_started OR EXCLUDED.is_started,
            started_at=CASE WHEN $3 THEN COALESCE(bot_users.started_at,now()) ELSE bot_users.started_at END,
            start_source=CASE WHEN $3 AND NOT bot_users.is_started THEN $4 ELSE bot_users.start_source END,
            start_chat_id=CASE WHEN $3 AND NOT bot_users.is_started THEN $5 ELSE bot_users.start_chat_id END,
            last_seen_at=now(),updated_at=now()""",
            self.bot_id,
            user_id,
            started,
            start_source,
            start_chat_id,
        )
        from repositories.postgres.user_tombstone import remove_tombstoned

        await remove_tombstoned(self.pool, self.bot_id, [user_id])

    async def get_state(self, user_id: int, chat_id: int):
        return await self.pool.fetchrow(
            """SELECT state,data,revision FROM bot_conversations
            WHERE telegram_account_id=$1 AND user_id=$2 AND chat_id=$3""",
            self.bot_id,
            user_id,
            chat_id,
        )

    async def claim_link(self, user_id, chat_id, request_id, interval) -> int:
        """Admit, register the user and own INSPECTING in one SQL transaction."""
        remaining = await self.pool.fetchval(
            """WITH granted AS (
              INSERT INTO request_limits (telegram_account_id,user_id,scope,requested_at)
              VALUES ($1,$2,'inspect',clock_timestamp())
              ON CONFLICT (telegram_account_id,user_id,scope) DO UPDATE
              SET requested_at=clock_timestamp()
              WHERE request_limits.requested_at <= clock_timestamp() - $5::int * interval '1 second'
              RETURNING requested_at
            ), touched AS (
              INSERT INTO bot_users (telegram_account_id,user_id)
              SELECT $1,$2 FROM granted
              ON CONFLICT (telegram_account_id,user_id) DO UPDATE SET last_seen_at=now(),updated_at=now()
            ), owned AS (
              INSERT INTO bot_conversations (telegram_account_id,user_id,chat_id,state,data)
              SELECT $1,$2,$3,'INSPECTING',jsonb_build_object('request_id',$4::text) FROM granted
              ON CONFLICT (telegram_account_id,user_id,chat_id) DO UPDATE SET
                state=EXCLUDED.state,data=EXCLUDED.data,
                revision=bot_conversations.revision+1,updated_at=now()
            ) SELECT CASE WHEN EXISTS (SELECT 1 FROM granted) THEN 0 ELSE
              COALESCE((SELECT GREATEST(1,CEIL(EXTRACT(EPOCH FROM
                requested_at + $5::int * interval '1 second' - clock_timestamp())))::int
                FROM request_limits WHERE telegram_account_id=$1 AND user_id=$2 AND scope='inspect'),
                $5::int) END""",
            self.bot_id,
            user_id,
            chat_id,
            request_id,
            interval,
        )
        # An older statement snapshot after a conflict must never turn denial into a grant.
        from repositories.postgres.user_tombstone import remove_tombstoned

        if await remove_tombstoned(self.pool, self.bot_id, [user_id]):
            return int(interval)
        return int(remaining)

    async def record_admitted_link(self, user_id, chat_id, request_id):
        """Called only inside the transaction that granted the rolling quota."""
        await self.pool.execute(
            """WITH touched AS (
              INSERT INTO bot_users (telegram_account_id,user_id) VALUES ($1,$2)
              ON CONFLICT (telegram_account_id,user_id) DO UPDATE SET last_seen_at=now(),updated_at=now()
            ) INSERT INTO bot_conversations (telegram_account_id,user_id,chat_id,state,data)
              VALUES ($1,$2,$3,'INSPECTING',jsonb_build_object('request_id',$4::text))
              ON CONFLICT (telegram_account_id,user_id,chat_id) DO UPDATE SET
              state=EXCLUDED.state,data=EXCLUDED.data,
              revision=bot_conversations.revision+1,updated_at=now()""",
            self.bot_id,
            user_id,
            chat_id,
            request_id,
        )
        from repositories.postgres.user_tombstone import remove_tombstoned
        from schemas.media import DownloadError

        if await remove_tombstoned(self.pool, self.bot_id, [user_id]):
            raise DownloadError("این حساب مجاز نیست.", code="user_unavailable")

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

    async def finish_inspection(self, user_id, chat_id, request_id, *, link_identity=None):
        await self.pool.execute(
            """WITH failed AS (UPDATE bot_conversations SET state='FAILED',data='{}',
            revision=revision+1,updated_at=now() WHERE telegram_account_id=$1
            AND user_id=$2 AND chat_id=$3 AND data->>'request_id'=$4 AND state='INSPECTING'
            RETURNING user_id) UPDATE user_link_history SET status='failed'
            WHERE telegram_account_id=$1 AND user_id=$2 AND url_hash=$5 AND status='pending'
            AND EXISTS (SELECT 1 FROM failed)""",
            self.bot_id,
            user_id,
            chat_id,
            request_id,
            link_identity,
        )

    async def update_data(self, user_id: int, chat_id: int, data: dict) -> None:
        await self.pool.execute(
            """INSERT INTO bot_conversations
            (telegram_account_id,user_id,chat_id,state,data) VALUES ($1,$2,$3,'MAIN',$4::jsonb)
            ON CONFLICT (telegram_account_id,user_id,chat_id) DO UPDATE SET
              data=bot_conversations.data || EXCLUDED.data,
              revision=bot_conversations.revision+1,updated_at=now()""",
            self.bot_id,
            user_id,
            chat_id,
            json.dumps(data),
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

    async def save_menu_and_choose(
        self,
        token,
        owner_id,
        chat_id,
        media,
        message_id=None,
        *,
        link_identity=None,
        request_id=None,
    ) -> None:
        await self.pool.execute(
            """WITH saved AS (
              INSERT INTO download_menus
              (telegram_account_id,token,user_id,chat_id,metadata,message_id)
              VALUES ($1,$2,$3,$4,$5::jsonb,$6) RETURNING token
            ), viewed AS (UPDATE user_link_history SET status='inspected',
              site=$5::jsonb->>'site',content_id=$5::jsonb->>'content_id',
              canonical_url=$5::jsonb->>'page_url'
              WHERE telegram_account_id=$1 AND user_id=$3 AND url_hash=$7)
            INSERT INTO bot_conversations (telegram_account_id,user_id,chat_id,state,data)
              SELECT $1,$3,$4,'CHOOSING_QUALITY',jsonb_build_object('menu_token',token) FROM saved
              ON CONFLICT (telegram_account_id,user_id,chat_id) DO UPDATE SET
                state=EXCLUDED.state,data=EXCLUDED.data,
                revision=bot_conversations.revision+1,updated_at=now()
              WHERE $8::text IS NULL OR bot_conversations.data->>'request_id'=$8""",
            self.bot_id,
            token,
            owner_id,
            chat_id,
            json.dumps(public_media(media)),
            message_id,
            link_identity,
            request_id,
        )
