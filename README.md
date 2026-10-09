# SaintCrispy

An asynchronous Telegram media downloader built with Python, Telethon, PostgreSQL and Redis. Each website has an independent implementation with its own client, parser, downloader, handler and URL rules.

Supported providers: **SoundCloud, YouTube, Instagram, Pinterest, XVideos and XNXX**. Available media, qualities and account requirements depend on the source website. The bot interface is currently Persian; developer documentation and code comments are English.

## How it works

1. A user sends a supported media URL and receives a quality menu.
2. The bot identifies the content using the provider's stable ID and the selected quality.
3. If that file already exists in Telegram, the saved document reference is reused.
4. Otherwise, a complete progressive URL is offered to Telegram first. The fetched document is checked before publication when its size is known or a size limit is configured.
5. If URL fetching fails, times out before publication, or returns a different size, the original source is streamed into Telegram uploads. HLS and separate audio/video streams are remuxed through FFmpeg with codec copy.
6. Successful file references and per-user link/delivery history are persisted in PostgreSQL. Redis accelerates file lookups and rate-limit rejections.

The transfer pipeline does not store complete media files on local disk. Unknown-length uploads use bounded buffers and backpressure. Concurrent requests for the same content and quality share one producer; recipient sends happen outside the creation lock.

## Requirements

For the recommended Docker deployment:

- Docker Engine or Docker Desktop with Docker Compose v2.
- A Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org), and a bot token created through [BotFather](https://t.me/BotFather).
- Network access to Telegram and the original websites/CDNs.

Docker includes Python 3.12, Node.js and FFmpeg. For a native installation, install Python **3.12 or newer**, FFmpeg, and Node.js **22 or newer** or Deno **2.3 or newer** for YouTube's JavaScript challenges. PostgreSQL and Redis can run in Docker or be managed separately. Redis is optional for native deployments; PostgreSQL is required.

## Quick start with Docker

Clone the repository:

```bash
git clone https://github.com/mahdiashtian/SaintCrispy.git
cd SaintCrispy
cp .env.example .env
```

Edit `.env` and fill in:

```dotenv
API_ID=your_numeric_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
DEV_DB_PASSWORD=replace_with_a_long_random_hex_password
```

Use a long alphanumeric/hex value for `DEV_DB_PASSWORD`: Compose interpolates it into the internal PostgreSQL URL. You can generate a value with `openssl rand -hex 32`. Keep the resulting `.env` private.

Start the application:

```bash
docker compose -p saintcrispy --profile bot up -d --build
```

Compose starts PostgreSQL and Redis, waits for their health checks, then starts the bot. It supplies the internal database/cache addresses and `FFMPEG_PATH=ffmpeg`; the host-side `DATABASE_URL`, `REDIS_URL` and `FFMPEG_PATH` entries may remain empty for this deployment.

Inspect or stop services:

```bash
docker compose -p saintcrispy --profile bot ps
docker compose -p saintcrispy logs --tail 100 -f bot
docker compose -p saintcrispy --profile bot stop bot
docker compose -p saintcrispy --profile bot down
```

PostgreSQL data lives in the project's named Docker volume. `down` keeps that volume; `down -v` deletes it. Keep the same Compose project name to retain the same database. Redis is a disposable cache and does not persist its contents.

After pulling updates, rebuild the bot with:

```bash
git pull --ff-only
docker compose -p saintcrispy --profile bot up -d --build bot
```

Database schema additions are applied at startup. Back up PostgreSQL separately before upgrades; copying the source folder does not back up its Docker volume.

## Native installation on Linux/macOS

Create a virtual environment and install the package:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Install FFmpeg and Node.js with your system's package manager. If using the included Compose services, start them and discover their randomly assigned localhost ports:

```bash
docker compose -p saintcrispy up -d postgres redis
docker compose -p saintcrispy port postgres 5432
docker compose -p saintcrispy port redis 6379
```

Set `DATABASE_URL` to `postgresql://downloader:PASSWORD@127.0.0.1:POSTGRES_PORT/downloader` and `REDIS_URL` to `redis://127.0.0.1:REDIS_PORT/0`. Use the password in `.env`; URL-encode special characters when constructing a connection URL for an external database. An empty `REDIS_URL` disables Redis.

The Python entry point reads process environment variables. It does not automatically read `.env`. For a trusted, shell-compatible `.env`, export the values before starting:

```bash
set -a
. ./.env
set +a
python -m downloader_bot
```

Use `FFMPEG_PATH=ffmpeg` for a binary on `PATH`, or set its absolute path. Stop any existing bot coordinator before starting another copy for the same bot account.

## Native installation on Windows

From PowerShell in the repository folder:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
# Edit .env and set API_ID, API_HASH and BOT_TOKEN.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-services.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

Python 3.12+ is supported; adjust the launcher version to match your installation. The setup script starts only PostgreSQL/Redis, generates a database password if needed, discovers their host ports, and writes the connection URLs to `.env`. It finds FFmpeg on `PATH` or through the installed development dependency instead of assuming a fixed binary version.

If Docker Desktop is unavailable, the setup script can use Docker inside WSL. Its default distribution is `Ubuntu-24.04`; select your installed distribution explicitly when different:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-services.ps1 -WslDistribution Ubuntu
```

Docker must be installed and accessible to your WSL user. The helper keeps that distribution alive while this project's services are needed. After stopping the services, stop only this project's helper with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keep-wsl-alive.ps1 -Stop
```

Execution-policy bypass above applies only to that PowerShell process. The scripts load `.env` before starting the asynchronous Python process.

## Configuration

`.env.example` lists all supported settings. Key operational settings are:

| Setting | Default | Purpose |
| --- | --- | --- |
| `MAX_CONCURRENT_REQUESTS` | `1000` | Maximum reserved/running transfer tasks; excess requests are rejected. |
| `TRANSFER_CONCURRENCY` | `1000` | Maximum producers transferring new files. |
| `CACHED_TRANSFER_CONCURRENCY` | `1000` | Maximum concurrent sends of saved references. |
| `USER_REQUEST_INTERVAL_SECONDS` | `60` | One accepted link per user per interval across all sites/chats. Quality selection has a separate stage limit. |
| `MAX_FILE_SIZE_MB` | `1024` | Per-file cap; one MB here is 1024 x 1024 bytes. Valid range: 1-1999. |
| `TRANSFER_TIMEOUT_SECONDS` | `3600` | Total transfer deadline, including waits for resources. |
| `METADATA_CONCURRENCY` | `8` | Concurrent inspections/source resolutions. |
| `REMUX_CONCURRENCY` | `2` | Concurrent FFmpeg processes; can be raised up to request capacity if resources permit. |
| `UPLOAD_PARALLELISM` | `4` | Maximum in-flight upload parts per file, range 1-8. |
| `UPLOAD_INFLIGHT_PARTS` | `64` | Shared window for parts being filled or uploaded, range 1-1024. |
| `TELEGRAM_SENDS_PER_SECOND` | `0` | Optional artificial send pacing; zero disables it. Actual FloodWait is always honored. |
| `MEDIA_STORAGE_CHAT` | empty | Optional storage channel accessible to the bot. Prefer a public `@username` or a peer the bot can resolve. |
| `FFMPEG_PATH` | `ffmpeg` | FFmpeg executable; blank also uses `PATH`. |

`/start` and the Stop button do not consume a link allowance. An accepted request consumes its allowance even if it later fails or is cancelled. A capacity rejection does not consume the transfer-stage allowance. Cancellation cleans up local streams/processes; it cannot guarantee cancellation inside Telegram's servers. Publication and persistence are protected from user-initiated Stop.

### Optional provider accounts

- **SoundCloud:** set `SOUNDCLOUD_OAUTH_TOKEN` for an authorized account. Original/HQ availability depends on the track and that account's permissions. Opus/MP3/AAC options reflect actual complete sources; preview-only media is excluded.
- **Instagram:** set `INSTAGRAM_COOKIE` to a single-line HTTP Cookie header from an authorized session. A specific story requires an authorized account. Private, removed or restricted media may remain unavailable.
- **YouTube:** `YOUTUBE_COOKIES_FILE` is an optional writable Netscape cookie-file path, `YOUTUBE_PROXY` is an optional HTTP proxy, and `YOUTUBE_JS_RUNTIME` selects Node/Deno. Empty proxy means direct egress for extraction and transfer. Cookies/tokens do not guarantee that the current network is accepted by YouTube.
- **Multiple accounts:** the `site_accounts` table stores account labels and environment-variable names, not credentials. Add rows for `soundcloud`, `instagram` or `youtube`, then define the referenced credential variables in the bot's environment. Each account has a separate HTTP/cookie session or YouTube cookie-file lock.

For YouTube cookies in Docker, mount the cookie file with a Compose override at the path named by `YOUTUBE_COOKIES_FILE`. Ensure the container's `bot` user can read/write it; never copy it into the image or commit it.

An optional local PO-token helper is provided by the `youtube-pot` profile:

```bash
docker compose -p saintcrispy --profile youtube-pot up -d youtube-pot
```

Set `YOUTUBE_POT_BASE_URL=http://youtube-pot:4416` for the Docker bot and rebuild/recreate the bot after changing configuration. The helper is internal and does not proxy media. Player-client overrides and Instagram GraphQL document IDs are documented in `.env.example`; leave the maintained defaults unless a provider change requires an override.

## Data and concurrency model

A file's database key is `(site, content_id, quality, telegram_account_id)`. PostgreSQL upserts prevent duplicate rows. Content IDs are provider-owned: SoundCloud numeric track IDs, YouTube video IDs, Instagram media/story IDs, Pinterest pin/asset IDs, and XVideos/XNXX canonical video IDs. Album order and temporary CDN query strings do not determine asset identity.

`user_link_history` records accepted inspections and outcomes. `user_media_history` records successful deliveries. `media_catalog`/`media_aliases` rebuild menus containing already stored qualities after restart without recontacting immutable origins. SoundCloud's mutable permalink slugs are rediscovered after the short metadata TTL so an old slug cannot permanently identify a replacement track. Media endpoint URLs and account credentials are excluded from the durable catalog.

One coordinator process owns a bot account. A PostgreSQL advisory lock rejects a second coordinator for the same account. This is not a horizontally distributed worker system. Redis failure falls back to PostgreSQL; a database outage fails the request rather than recording fictitious success.

The default pipeline admits 1000 tasks and has been locally exercised with 1000 simultaneous HTTP downloads and real PostgreSQL/Redis. Telegram RPCs in that load test were simulated. This does not establish 1000 real Telegram sends or 1000 active FFmpeg processes. Shared upload parts have roughly 32 MiB of payload budget at the default window; HTTP/TLS, tasks, subprocesses and the database add memory overhead. Size your server, connection/file-descriptor limits and bandwidth accordingly.

Tasks and menus are held in memory. An interrupted transfer is not automatically resumed after a process crash; successfully persisted Telegram files can be reused after restart. Telegram publication and a database commit are separate operations, so an arbitrary crash between them cannot provide an unconditional exactly-once message guarantee.

## Project structure and provider boundaries

```text
src/downloader_bot/
  __main__.py              # Composition and lifecycle
  config.py                # Environment configuration
  contracts.py             # Storage/delivery interfaces
  database.py              # PostgreSQL and Redis repository
  service.py               # Inspection, deduplication and delivery orchestration
  streaming.py             # Generic byte transfer and FFmpeg processes
  telegram.py              # Telegram URL fetching, upload and reuse
  handlers/                # Generic start, quality, stop and rate limiting
  downloaders/
    base.py                # inspect/resolve contract
    router.py              # Provider dispatch
    <site>/
      __init__.py
      client.py            # Provider HTTP/session/authentication
      parser.py            # Provider extraction and format/HLS discovery
      downloader.py        # Direct inspect/resolve implementation
      handler.py           # Provider link/menu handler
      urls.py              # Provider URL rules
scripts/                    # Windows development setup
tests/                     # Unit, architecture, real DB and FFmpeg tests
tools/                     # Optional diagnostics and local load tests
```

Each provider owns those six files. Providers do not import another provider, a shared concrete extractor or the generic transfer implementation. Shared models and the abstract contract are deliberately small. Similar extraction/HLS code is kept inside its owning provider because site behavior can diverge. DRY applies within each provider and to generic infrastructure; it does not override this isolation rule. Storage and delivery are injected through structural interfaces so the service can be tested independently.

To add a provider, create the same six files, subclass `Downloader`, implement `inspect` and `resolve`, and register its URL extractor, downloader and handler in the existing composition modules. Do not add a common concrete video extractor. Extend the architecture tests for the new site, add parser/downloader cases, and test its session isolation.

## Development and tests

```bash
python -m pip install -e '.[dev]'
python -m ruff check src tests tools
python -m ruff format --check src tests tools
python -m pytest -q
```

Tests mock Telegram and origin APIs. Some FFmpeg tests generate local fixture files; this does not change the runtime no-complete-media-file transfer path. PostgreSQL integration tests need `TEST_DATABASE_URL`; cache integration tests also need `TEST_REDIS_URL`. Without the required variables, those cases are skipped:

```bash
export TEST_DATABASE_URL='postgresql://downloader:PASSWORD@127.0.0.1:POSTGRES_PORT/downloader'
export TEST_REDIS_URL='redis://127.0.0.1:REDIS_PORT/0'
export IMAGEIO_FFMPEG_EXE=ffmpeg
python -m pytest -q
```

Use a dedicated test database; integration tests create and remove their own schemas. GitHub Actions runs lint, formatting, architecture/language checks and the complete suite with PostgreSQL/Redis services and FFmpeg, without real bot credentials.

Optional diagnostics:

```bash
python tools/audit_providers.py
python tools/audit_providers.py --sites pinterest --telegram --stream-mismatches
python tools/load_test_transfers.py --requests 1000 --unique-downloads 1000 --all-new --provider-mix --synchronize-sources --user-history --database --file-mib 1 --global-parts 64
```

Live provider probes use the current network and optional account settings. `--telegram` registers media with Telegram but does not send chat messages. The local load test uses real HTTP and, with `--database`, the configured database/cache, while simulating Telegram. `--provider-mix` cycles site labels; it does not run real provider extraction. The benchmark removes only rows and cache keys for its private test account.

## Troubleshooting

- **Missing environment variables:** export the configured values before native startup, or use `scripts/run.ps1`. Docker loads `.env` itself.
- **FFmpeg cannot start:** verify `ffmpeg -version` and `FFMPEG_PATH`; YouTube separate streams and HLS require FFmpeg.
- **YouTube login/bot challenge:** use an authorized session and an accepted network route. PO tokens do not grant access to private or unavailable content.
- **No available quality or HTTP 429:** source availability, authentication and rate limits apply. Provider cooldowns prevent repeated bursts against a rejected origin.
- **Telegram rejects an external URL:** the bot falls back to streaming when safe. An instant link from another downloader can be that service's proxy or merged output, not necessarily an origin URL usable by Telegram.
- **Another coordinator is running:** stop the existing process/container for that bot before starting a replacement.
- **Database connection fails:** check service health, the password and the current published ports. Docker-native bot connections use `postgres`/`redis`, not Windows host ports.

Local `.env`, Telegram sessions, cookie files, runtime state, raw research and Persian reports are intentionally excluded from Git. The previous Persian README is preserved locally under `.local-docs/README.fa.md`; it is not part of the public repository.
