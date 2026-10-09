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

The bot runs directly on the host with `python main.py`. Docker Compose runs **only PostgreSQL and Redis**.

- Python **3.12 or newer**, with a virtual environment.
- Docker Engine/Desktop and Docker Compose v2 with support for `up --wait`.
- FFmpeg installed on the host.
- Node.js **22 or newer**, or Deno **2.3 or newer**, for YouTube's JavaScript challenges.
- A Telegram API ID/hash from [my.telegram.org](https://my.telegram.org), and a bot token from [BotFather](https://t.me/BotFather).
- Network access to Telegram and the source websites/CDNs.

## Installation and manual startup on Ubuntu

These commands assume an Ubuntu release whose Python is at least 3.12, and an already installed Docker Engine/Compose. Use `sudo` for package installation when not logged in as root.

```bash
apt-get update
apt-get install -y git python3 python3-venv ffmpeg curl ca-certificates
python3 --version
```

Install a compatible Node.js runtime if one is not already installed. This uses the [nvm project's installer](https://github.com/nvm-sh/nvm#installing-and-updating):

```bash
export NVM_DIR="$HOME/.nvm"
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.8/install.sh | bash
. "$NVM_DIR/nvm.sh"
nvm install 24
node --version
```

Clone and install the application:

```bash
git clone https://github.com/mahdiashtian/SaintCrispy.git
cd SaintCrispy
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
test -f .env || cp .env.example .env
nano .env
```

Set your credentials in `.env`. Keep password values single-quoted so dollar signs and comment characters remain literal:

```dotenv
API_ID=your_numeric_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
DEV_DB_PASSWORD='your_database_password'
POSTGRES_PORT=
REDIS_PORT=
```

If a database already exists, keep its current password. PostgreSQL applies `POSTGRES_PASSWORD` when initializing a new data directory; editing `.env` does not change a password stored in an existing database.

Configure the ports and start the databases, then launch the bot manually:

```bash
python tools/setup_services.py
python main.py
```

The setup command chooses a free localhost port for each blank port setting, then saves the exact `POSTGRES_PORT` and `REDIS_PORT` in `.env`. Subsequent runs keep those numbers. You can enter your own free port numbers before setup. Existing localhost connection URLs also supply their previous ports during migration. Docker binds both services to `127.0.0.1` using the saved ports.

The same setup writes explicit `DATABASE_URL` and `REDIS_URL` values containing `127.0.0.1` and those ports. PostgreSQL's Docker container reads `DEV_DB_PASSWORD` from `.env`; the URL password is percent-encoded for Python, including `@`, `/`, `#`, `?` and dollar signs. If the password is blank or still `YOUR_PASSWORD`, setup generates a random hex password. Setup waits for both database health checks and prints only their addresses, not credentials.

`main.py` reads the `.env` next to itself automatically. Values in that file take precedence over stale shell exports and are loaded without variable expansion. It runs with the repository as its working directory, so relative log/cookie paths remain consistent. The bot is ready to receive `/start` when the console emits `bot_ready`; earlier `runtime_started` only means that the process has begun initialization. Press Ctrl+C for normal cleanup.

Run the same foreground command in later sessions:

```bash
cd ~/SaintCrispy
. .venv/bin/activate
python main.py
```

The foreground bot follows the terminal session's lifetime. To keep a manually launched process through SSH disconnections, run it in a persistent terminal session or configure a process supervisor separately.

## Updating an existing server

From the existing repository, keeping its `.env` and Compose project name:

```bash
git pull --ff-only
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python tools/setup_services.py
python main.py
```

Stop the current native bot before replacing it. Setup uses project `saintcrispy` by default, keeps its PostgreSQL named volume, and removes obsolete bot/helper containers belonging to that same project. It runs only the two database services. No application image is built.

## Database service management

After initial setup has populated `.env`:

```bash
docker compose -p saintcrispy up -d --wait postgres redis
docker compose -p saintcrispy ps
docker compose -p saintcrispy logs --tail 100 postgres redis
docker compose -p saintcrispy port postgres 5432
docker compose -p saintcrispy port redis 6379
docker compose -p saintcrispy stop postgres redis
```

Internal container ports remain 5432 and 6379; the native Python bot uses the published localhost ports in `.env`. PostgreSQL data lives in `saintcrispy_postgres_data`. `docker compose -p saintcrispy down` preserves the volume; `down -v` deletes it. Redis is a disposable cache with persistence disabled. Back up PostgreSQL separately before upgrades.

After changing a port or password setting, rerun `python tools/setup_services.py` to refresh the explicit URLs and Compose configuration. Setup manages a local database named `downloader` and user `downloader`. For an externally managed database/cache, supply your own `DATABASE_URL`/`REDIS_URL` and run `python main.py` directly without the local setup command. An empty `REDIS_URL` disables Redis.

## Windows development

Install Python 3.12+, FFmpeg and a compatible Node.js runtime on Windows. Run in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-services.ps1
.\.venv\Scripts\python.exe main.py
```

Adjust the Python launcher version if needed. The setup wrapper uses Docker Desktop when available, otherwise Docker in WSL distribution `Ubuntu` with user `root`. Choose another installed distribution/user explicitly:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-services.ps1 -WslDistribution Ubuntu-24.04 -WslUser root
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

Windows setup uses Compose project `downloader-bot-dev` and delegates port/password/URL configuration to the same Python setup tool. The WSL user must be able to run Docker. A hidden keepalive helper keeps WSL available while the native Windows bot runs; stop it when finished:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\keep-wsl-alive.ps1 -Stop
```

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

Cookie-file paths are host paths. The account running `python main.py` needs read/write access to a YouTube cookie file. Keep cookies outside source control. An optional independently managed PO-token helper can be configured with a host-reachable `YOUTUBE_POT_BASE_URL`, such as `http://127.0.0.1:4416`; it is not started by the database Compose file. Player-client overrides and Instagram GraphQL document IDs are documented in `.env.example`.

## Data and concurrency model

A file's database key is `(site, content_id, quality, telegram_account_id)`. PostgreSQL upserts prevent duplicate rows. Content IDs are provider-owned: SoundCloud numeric track IDs, YouTube video IDs, Instagram media/story IDs, Pinterest pin/asset IDs, and XVideos/XNXX canonical video IDs. Album order and temporary CDN query strings do not determine asset identity.

`user_link_history` records accepted inspections and outcomes. `user_media_history` records successful deliveries. `media_catalog`/`media_aliases` rebuild menus containing already stored qualities after restart without recontacting immutable origins. SoundCloud's mutable permalink slugs are rediscovered after the short metadata TTL so an old slug cannot permanently identify a replacement track. Media endpoint URLs and account credentials are excluded from the durable catalog.

One coordinator process owns a bot account. A PostgreSQL advisory lock rejects a second coordinator for the same account. This is not a horizontally distributed worker system. Redis failure falls back to PostgreSQL; a database outage fails the request rather than recording fictitious success.

The default pipeline admits 1000 tasks and has been locally exercised with 1000 simultaneous HTTP downloads and real PostgreSQL/Redis. Telegram RPCs in that load test were simulated. This does not establish 1000 real Telegram sends or 1000 active FFmpeg processes. Shared upload parts have roughly 32 MiB of payload budget at the default window; HTTP/TLS, tasks, subprocesses and the database add memory overhead. Size your server, connection/file-descriptor limits and bandwidth accordingly.

Tasks and menus are held in memory. An interrupted transfer is not automatically resumed after a process crash; successfully persisted Telegram files can be reused after restart. Telegram publication and a database commit are separate operations, so an arbitrary crash between them cannot provide an unconditional exactly-once message guarantee.

## Performance logging on a server

Performance logging is enabled by default. Events are JSON Lines: one JSON object per line, with UTC timestamps, a process `session_id`, and a unique `transfer_id`. Output goes to stdout and `logs/performance.jsonl`. Serialization, writes and rotation run in a worker thread behind a bounded queue; transfers never wait for the log disk. Original URLs, signed CDN URLs, credentials, titles, user/chat IDs and raw exception messages are excluded. Content is correlated through a hash of its provider ID and selected quality.

| Setting | Default | Purpose |
| --- | --- | --- |
| `LOG_FILE` | `logs/performance.jsonl` | File destination; blank disables file output. |
| `LOG_MAX_MB` | `20` | Rotate each file near this many MiB; range 1-1024. |
| `LOG_BACKUP_COUNT` | `10` | Retained rotated files, plus the current file; range 1-100. |
| `LOG_QUEUE_SIZE` | `10000` | Bounded pending event count; range 100-100000. |
| `LOG_STDOUT` | `1` | Also emit JSON to stdout; set 0 to disable. |
| `METRICS_INTERVAL_SECONDS` | `30` | Resource/network summaries and active transfer progress; range 1-3600. |
| `METRICS_NETWORK_INTERFACE` | empty | Sample all non-loopback interfaces, or select an interface such as `eth0`. |

Defaults retain roughly 220 MiB of file logs. The oldest backup is removed during rotation. Logs are stored on the host in the repository's `logs/` directory. Use a shorter metrics interval, such as 1-5 seconds, during load measurements; a 30-second sample can miss a short CPU/RAM peak. A failed disk or full queue increments `log_write_errors`/`log_records_dropped` in subsequent summaries. Retained events can therefore be incomplete; cumulative in-memory counters continue advancing. Normal shutdown drains the queue with a bounded wait. A forced process kill cannot guarantee a final summary or every queued event. Linux SIGTERM and Ctrl+C perform normal cleanup. A separately configured supervisor should allow sufficient time for that cleanup.

The main events are:

- `inspection_finished`: metadata latency, provider/cache path, quality count and safe error details.
- `transfer_started` / `transfer_progress` / `transfer_finished`: provider, quality, content hash, method, exact final file size when known, elapsed time, bytes, current stages and final success/failure/cancellation.
- `external_fallback`: the reason Telegram URL fetching fell back to streaming.
- `batch_started` / `batch_finished`: a continuous wave from the first active transfer until no transfers remain. The result records N started/completed requests, peak concurrency, payload totals and the wave's wall time. Cache sends are counted as requests; deduplicated downloads still count their actual bytes only once.
- `metrics_interval`: active/peak transfers, counts and traffic since startup and in the latest interval, bytes/second, completions/second, recent latency p95, per-site outcomes, process/child RSS, process/system CPU, event-loop scheduling delay and log health. Latency p95 uses the latest 2048 completed transfers.
- `runtime_started`, `runtime_configured`, `startup_progress`, `bot_ready`, `runtime_stopped` and safe runtime/task failure events. Telegram connection retries and login cooldowns include their requested wait duration.
- `request_rejected`, admission failures and cancellation events: capacity/rate rejections, safe failures before a transfer, and accepted/protected Stop requests. Job reservation/running counts are included in interval summaries.

`stages_seconds` separates resource waits, resolution, source-size probing, Telegram URL fetching, streaming, Telegram cooldowns/publication and database/history writes. `download_seconds` spans source acquisition through valid EOF; `upload_seconds` spans the first part RPC through final acknowledgement/cleanup. Download and upload overlap, and include backpressure/waits within their spans. Do not add them to calculate elapsed time. `upload_rpc` is cumulative time across concurrent RPCs and may exceed wall time. External URL fetching exposes only `external_fetch` duration and final file size; Telegram does not expose separate download/upload timing. Reused files have no local media transfer, so both times are null.

Traffic fields have different scopes:

- `stream_read_bytes`: bytes read by the upload pipeline. For progressive media this is the HTTP body; for HLS/DASH this is FFmpeg's remuxed output, including its container overhead.
- `progressive_download_bytes` / `ffmpeg_output_bytes`: those two cases separately. FFmpeg's original segment traffic cannot be inferred from its output size.
- `upload_acked_bytes`: payload parts acknowledged by Telegram. `upload_attempt_bytes` also counts explicit repeated part RPCs. Neither includes TLS/MTProto/TCP overhead.
- `pipeline_payload_bytes`: stream reads plus attempted upload payload. This describes both sides of the application's pipeline; it is not a NIC byte count.
- `delivered_file_bytes`: logical file sizes delivered successfully, including external fetches and cache reuse. Cached files can have a large logical size while producing no local media payload traffic.
- `network_received_bytes` / `network_sent_bytes` / `network_traffic_bytes`: OS counters on the selected interfaces, including metadata requests, FFmpeg input, database/control traffic and protocol overhead. They are sampled session-wide, not attributed to individual transfers. In Docker they cover the container's network namespace; native deployment includes other traffic on the host's selected interfaces. Select the primary interface to avoid counting the same host traffic through multiple bridges/interfaces. `system.network_interfaces` and `network_available` identify the measurement scope. These counters do not measure traffic between Telegram's servers and an origin.

Inspect the live host log or summarize its retained rotated files:

```bash
tail -f logs/performance.jsonl
python tools/summarize_logs.py 'logs/performance.jsonl*'
python tools/summarize_logs.py 'logs/performance.jsonl*' --files 1000
python tools/summarize_logs.py 'logs/performance.jsonl*' --site youtube --since '2026-10-09T00:00:00Z'
```

The analyzer reports bytes/MiB, methods, outcomes, wall time, summed per-request/stage times and throughput. It deduplicates retained transfer IDs and skips malformed/truncated JSON lines. NIC/session summaries are shown separately and remain session-wide even when filtering transfer records. The first-N selection follows file modification order, then record order; timestamps require a timezone. Retention and dropped records limit what can be reconstructed. Transfer wall times use a monotonic clock; the analyzer's cross-request wall range uses UTC timestamps and can be affected by host clock corrections.

Exercise logging with a local load test before deployment:

```bash
python tools/load_test_transfers.py --requests 1000 --unique-downloads 1000 --all-new --file-mib 1 --synchronize-sources --no-memory-tracing --log-file logs/load-test.jsonl
python tools/summarize_logs.py 'logs/load-test.jsonl*' --files 1000
```

This benchmark downloads real localhost HTTP bodies while simulating Telegram. Its JSON events test the instrumentation and logging path; they do not establish production Telegram throughput.

## Project structure and provider boundaries

```text
main.py                    # Host entry point and automatic .env loading
compose.yaml               # PostgreSQL and Redis only
src/downloader_bot/
  __main__.py              # Composition and lifecycle
  config.py                # Environment configuration
  contracts.py             # Storage/delivery interfaces
  log_writer.py            # Bounded background JSON writer and rotation
  telemetry.py             # Transfer, batch and system measurements
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
scripts/                   # Windows development setup
tests/                     # Unit, architecture, real DB and FFmpeg tests
tools/                     # Database setup, diagnostics and local load tests
```

Each provider owns those six files. Providers do not import another provider, a shared concrete extractor or the generic transfer implementation. Shared models and the abstract contract are deliberately small. Similar extraction/HLS code is kept inside its owning provider because site behavior can diverge. DRY applies within each provider and to generic infrastructure; it does not override this isolation rule. Storage and delivery are injected through structural interfaces so the service can be tested independently.

To add a provider, create the same six files, subclass `Downloader`, implement `inspect` and `resolve`, and register its URL extractor, downloader and handler in the existing composition modules. Do not add a common concrete video extractor. Extend the architecture tests for the new site, add parser/downloader cases, and test its session isolation.

## Development and tests

```bash
python -m pip install -e '.[dev]'
python -m ruff check main.py src tests tools
python -m ruff format --check main.py src tests tools
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

- **Missing environment variables:** fill `.env` in the repository root and launch `python main.py`, which reads it automatically.
- **FFmpeg cannot start:** verify `ffmpeg -version` and `FFMPEG_PATH`; YouTube separate streams and HLS require FFmpeg.
- **YouTube login/bot challenge:** use an authorized session and an accepted network route. PO tokens do not grant access to private or unavailable content.
- **No available quality or HTTP 429:** source availability, authentication and rate limits apply. Provider cooldowns prevent repeated bursts against a rejected origin.
- **Telegram rejects an external URL:** the bot falls back to streaming when safe. An instant link from another downloader can be that service's proxy or merged output, not necessarily an origin URL usable by Telegram.
- **Another coordinator is running:** stop the existing process/container for that bot before starting a replacement.
- **Database connection fails:** run `python tools/setup_services.py` and check `docker compose -p saintcrispy ps`. Native URLs must use `127.0.0.1` and the saved host ports. A `gaierror` indicates name resolution failed; setup replaces Docker-only hostnames and safely encodes password characters. Keep an existing database's actual password.
- **No response to `/start`:** look for `bot_ready`, the latest `startup_progress` stage, safe error events, and Telegram retry/login-wait events in `logs/performance.jsonl`. `runtime_started` alone is not readiness.

Local `.env`, Telegram sessions, cookie files, runtime state, raw research and Persian reports are intentionally excluded from Git. The previous Persian README is preserved locally under `.local-docs/README.fa.md`; it is not part of the public repository.
