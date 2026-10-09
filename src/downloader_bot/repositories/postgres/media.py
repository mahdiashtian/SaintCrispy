"""SQL repositories own persistence; handlers never execute queries."""

import hashlib
import json
from dataclasses import asdict

from downloader_bot.db.postgres.engine import migrate
from downloader_bot.schemas.media import Media, Quality, TelegramFile


class PostgresMediaRepository:
    def __init__(self, pool, bot_id: int):
        self.pool = pool
        self.bot_id = bot_id

    async def initialize(self) -> None:
        await migrate(self.pool)

    async def get(self, site: str, content_id: str, quality: str) -> TelegramFile | None:
        row = await self.pool.fetchrow(
            """SELECT document_id, access_hash, file_reference, origin_peer, message_id, size_bytes
            FROM media_files WHERE site=$1 AND content_id=$2 AND quality=$3
            AND telegram_account_id=$4""",
            site,
            content_id,
            quality,
            self.bot_id,
        )
        if row is None:
            # Negative results are not cached. Keep a failed key dirty if stale data exists.
            return None
        result = TelegramFile(**dict(row))
        return result

    async def save(self, site: str, content_id: str, quality: str, file: TelegramFile) -> None:
        await self.pool.execute(
            """INSERT INTO media_files
            (site, content_id, quality, telegram_account_id, document_id,
             access_hash, file_reference, origin_peer, message_id, size_bytes)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (site, content_id, quality, telegram_account_id) DO UPDATE SET
              document_id=EXCLUDED.document_id, access_hash=EXCLUDED.access_hash,
              file_reference=EXCLUDED.file_reference, origin_peer=EXCLUDED.origin_peer,
              message_id=EXCLUDED.message_id, size_bytes=EXCLUDED.size_bytes, updated_at=now()""",
            site,
            content_id,
            quality,
            self.bot_id,
            file.document_id,
            file.access_hash,
            file.file_reference,
            file.origin_peer,
            file.message_id,
            file.size_bytes,
        )

    async def accounts(self, site: str) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT id, label, credential_env, subscription FROM site_accounts
            WHERE site=$1 AND enabled AND (expires_at IS NULL OR expires_at > now())
              AND (cooldown_until IS NULL OR cooldown_until <= now())
            ORDER BY priority DESC, id""",
            site,
        )
        return [dict(row) for row in rows]

    async def record_link_view(self, user_id: int, url: str) -> str:
        identity = hashlib.sha256(url.encode()).hexdigest()
        await self.pool.execute(
            """INSERT INTO user_link_history
            (telegram_account_id, user_id, url_hash, input_url)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (telegram_account_id, user_id, url_hash) DO UPDATE SET
              view_count=user_link_history.view_count+1, last_seen_at=now(), status='pending'""",
            self.bot_id,
            user_id,
            identity,
            url,
        )
        return identity

    async def remember_media(self, aliases: tuple[str, ...], media: Media) -> None:
        # Keep menu metadata only. Signed URLs, cookies and account credentials never
        # enter the persistent catalog, whose qualities are usable only with saved files.
        data = asdict(media)
        data.pop("authorization")
        data.pop("account_id")
        for quality in data["qualities"]:
            quality["endpoint"] = ""
            quality["fallback_endpoint"] = None
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """INSERT INTO media_catalog (telegram_account_id,site,content_id,metadata)
                VALUES ($1,$2,$3,$4::jsonb)
                ON CONFLICT (telegram_account_id,site,content_id) DO UPDATE SET
                  metadata=EXCLUDED.metadata, updated_at=now()""",
                self.bot_id,
                media.site,
                media.content_id,
                json.dumps(data),
            )
            for alias in set(aliases):
                await connection.execute(
                    """INSERT INTO media_aliases (telegram_account_id,alias,site,content_id)
                    VALUES ($1,$2,$3,$4)
                    ON CONFLICT (telegram_account_id,alias) DO UPDATE SET
                      site=EXCLUDED.site, content_id=EXCLUDED.content_id""",
                    self.bot_id,
                    alias,
                    media.site,
                    media.content_id,
                )

    async def known_media(self, alias: str) -> Media | None:
        row = await self.pool.fetchrow(
            """SELECT c.metadata, ARRAY(
                SELECT quality FROM media_files f WHERE f.telegram_account_id=c.telegram_account_id
                AND f.site=c.site AND f.content_id=c.content_id) AS stored_qualities
            FROM media_catalog c JOIN media_aliases a
              ON (a.telegram_account_id,a.site,a.content_id) =
                 (c.telegram_account_id,c.site,c.content_id)
            WHERE a.telegram_account_id=$1 AND a.alias=$2""",
            self.bot_id,
            alias,
        )
        if row is None or not row["stored_qualities"]:
            return None
        data = json.loads(row["metadata"])
        data["qualities"] = tuple(
            Quality(**item) for item in data["qualities"] if item["key"] in row["stored_qualities"]
        )
        return Media(**data) if data["qualities"] else None

    async def finish_link_view(self, user_id: int, identity: str, media=None) -> None:
        await self.pool.execute(
            """UPDATE user_link_history SET status=$4,
            site=COALESCE($5,site), content_id=COALESCE($6,content_id),
            canonical_url=COALESCE($7,canonical_url)
            WHERE telegram_account_id=$1 AND user_id=$2 AND url_hash=$3""",
            self.bot_id,
            user_id,
            identity,
            "inspected" if media else "failed",
            media.site if media else None,
            media.content_id if media else None,
            media.page_url if media else None,
        )

    async def record_delivery(self, user_id: int, media, quality, method: str) -> None:
        await self.pool.execute(
            """INSERT INTO user_media_history
            (telegram_account_id, user_id, site, content_id, quality, last_method)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (telegram_account_id, user_id, site, content_id, quality) DO UPDATE SET
              delivery_count=user_media_history.delivery_count+1,
              last_method=EXCLUDED.last_method, last_delivered_at=now()""",
            self.bot_id,
            user_id,
            media.site,
            media.content_id,
            quality.key,
            method,
        )

    async def claim_request(self, user_id: int, scope: str, interval: int) -> int:
        granted = await self.pool.fetchval(
            """INSERT INTO request_limits (telegram_account_id, user_id, scope, requested_at)
            VALUES ($1,$2,$3,clock_timestamp())
            ON CONFLICT (telegram_account_id, user_id, scope) DO UPDATE
            SET requested_at=clock_timestamp()
            WHERE request_limits.requested_at <= clock_timestamp() - $4 * interval '1 second'
            RETURNING requested_at""",
            self.bot_id,
            user_id,
            scope,
            interval,
        )
        if granted is not None:
            remaining = 0
        else:
            seconds = await self.pool.fetchval(
                """SELECT GREATEST(1, CEIL(EXTRACT(EPOCH FROM
                requested_at + $4 * interval '1 second' - clock_timestamp())))
                FROM request_limits WHERE telegram_account_id=$1 AND user_id=$2 AND scope=$3""",
                self.bot_id,
                user_id,
                scope,
                interval,
            )
            remaining = int(seconds or 1)
        return remaining
