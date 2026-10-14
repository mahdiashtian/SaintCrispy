"""Apply the existing additive PostgreSQL migration chain before starting the bot."""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import ConfigurationError, connection_urls  # noqa: E402
from core.diagnostics import error_fields  # noqa: E402
from db.postgres.engine import create_pool, migrate  # noqa: E402


async def migrate_database() -> None:
    url, _ = connection_urls(os.environ)
    if not url:
        raise ConfigurationError(
            "DATABASE_URL or native database ports are required",
            code="missing_required_settings",
            fields=("DATABASE_URL",),
        )
    async with create_pool(url, max_size=2) as pool:
        await migrate(pool)
        versions = await pool.fetchval("SELECT count(*) FROM app_schema_migrations")
    print(json.dumps({"event": "migrations_applied", "versions": versions}))


def main() -> int:
    load_dotenv(ROOT / ".env", override=True, interpolate=False, encoding="utf-8-sig")
    try:
        asyncio.run(migrate_database())
    except Exception as error:
        print(json.dumps({"event": "migration_failed", **error_fields(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
