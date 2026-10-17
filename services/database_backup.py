"""Complete PostgreSQL custom archives, streamed to private temporary files."""

import asyncio
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from schemas.media import DownloadError


def valid_archive(path):
    with path.open("rb") as archive:
        return archive.read(5) == b"PGDMP" and path.stat().st_size > 5


class DatabaseBackup:
    def __init__(self, database_url, *, timeout=300):
        self.database_url = database_url
        self.timeout = timeout
        self.slots = asyncio.Semaphore(1)

    def command(self):
        url = urlsplit(self.database_url)
        if url.scheme not in {"postgres", "postgresql"} or not url.path.strip("/"):
            raise DownloadError("اتصال PostgreSQL برای بک‌آپ کامل آماده نیست.")
        environment = os.environ.copy()
        # Credentials are passed only through the child environment, never argv/logs.
        environment["PGPASSWORD"] = unquote(url.password or "")
        query = parse_qs(url.query)
        for key in ("sslmode", "sslrootcert", "sslcert", "sslkey", "sslcrl"):
            if key in query:
                environment["PG" + key.upper()] = query[key][-1]
        arguments = [
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--no-password",
            f"--username={unquote(url.username or '')}",
            f"--dbname={unquote(url.path.lstrip('/'))}",
        ]
        if executable := shutil.which("pg_dump"):
            return [
                executable,
                *arguments,
                f"--host={url.hostname or 'localhost'}",
                f"--port={url.port or 5432}",
            ], environment
        # The supported Compose installation already contains a matching pg_dump.
        # Never silently dump that container for a different/remote DATABASE_URL.
        if (
            url.hostname in {"127.0.0.1", "localhost", "::1"}
            and str(url.port or 5432) == os.environ.get("POSTGRES_PORT")
            and (docker := shutil.which("docker"))
        ):
            root = Path(__file__).resolve().parents[1]
            return [
                docker,
                "compose",
                "--project-directory",
                str(root),
                "-f",
                str(root / "compose.yaml"),
                "-p",
                os.environ.get("COMPOSE_PROJECT_NAME", "saintcrispy"),
                "exec",
                "-T",
                "postgres",
                "pg_dump",
                *arguments,
            ], environment
        raise DownloadError(
            "برای بک‌آپ کامل، ابزار pg_dump هم‌نسخهٔ PostgreSQL را روی میزبان نصب کن "
            "یا سرویس PostgreSQL همین نصب Compose را در دسترس قرار بده."
        )

    @asynccontextmanager
    async def create(self):
        async with self.slots:
            command, environment = self.command()
            descriptor, filename = await asyncio.to_thread(
                tempfile.mkstemp, prefix="saintcrispy-full-", suffix=".dump"
            )
            path = Path(filename)
            process = None
            try:
                with os.fdopen(descriptor, "wb") as output:
                    try:
                        async with asyncio.timeout(self.timeout):
                            process = await asyncio.create_subprocess_exec(
                                *command,
                                stdout=output,
                                stderr=asyncio.subprocess.DEVNULL,
                                env=environment,
                            )
                            code = await process.wait()
                    except (OSError, TimeoutError) as error:
                        raise DownloadError(
                            "بک‌آپ کامل ساخته نشد؛ دسترسی و نسخهٔ pg_dump و اتصال PostgreSQL را بررسی کن."
                        ) from error
                    finally:
                        if process is not None and process.returncode is None:
                            process.kill()
                            await process.wait()
                valid = await asyncio.to_thread(valid_archive, path)
                if code != 0 or not valid:
                    raise DownloadError("بک‌آپ کامل ساخته نشد؛ هیچ فایل ناقصی ارسال نشد.")
                yield path
            finally:
                await asyncio.to_thread(path.unlink, missing_ok=True)
