# Deployment, logging and recovery

The October 16 source-parity follow-up adds migrations 11–12, resumable member crawling,
normalized channel/watch data, daily history and the Couplyo administration flow.
The prior migration 10 added separate Redis
STATE and operational jobs. Follow the stopped-bot backup/upgrade procedure below;
existing data and migration history are preserved. [Database ownership](DATABASE.md) lists every table, key, index and
migration. `python scripts/export_schema.py --check` verifies the offline reference
SQL; it never connects to the installed database or substitutes for migration.

The Python bot and Node runtime run on the host. Docker runs PostgreSQL, Redis CACHE and Redis STATE only. Existing ports and password stay in `.env`; Python derives connection URLs from those values.

## Administration upgrade (revision 10)

This same procedure upgrades an existing installation through revision **12**.
Create a fresh pre-upgrade backup; never overwrite your only tested backup.
New roster settings default to enabled
with one worker, three passes, 400 requests and a one-second minimum page delay.
Review [source parity and limits](COUPLYO_PARITY.md) before enabling more workers.

Stop the existing Python receiver before upgrading. Keep its current `.env`,
PostgreSQL password, Compose project name, session and named volumes. Create a
database backup on the server (Linux shell), then check its archive listing:

```bash
cd ~/SaintCrispy
mkdir -p .runtime/backups
chmod 700 .runtime/backups
db_backup_path="$(mktemp .runtime/backups/before-revision-12-XXXXXXXX.dump)"
docker compose -p saintcrispy exec -T postgres pg_dump -U downloader -d downloader -Fc > "$db_backup_path"
docker compose -p saintcrispy exec -T postgres pg_restore --list < "$db_backup_path" > "$db_backup_path.list"
chmod 600 "$db_backup_path" "$db_backup_path.list"
```

An archive listing detects a corrupt format, not a successful restoration. Verify
restoration to an isolated database before relying on the backup; keep an off-host
copy. Do not run `down -v`, delete migration entries or execute the empty-schema
reference SQL against the installed database.

```bash
git pull --ff-only
source .venv/bin/activate
python -m pip install -e .
python tools/setup_services.py
docker compose -p saintcrispy ps
python scripts/migrate.py
python main.py
```

Setup keeps the existing SQL/cache ports and password, selects and saves a distinct
`REDIS_STATE_PORT` if missing, and writes `REDIS_STATE_URL` to `.env`. Python derives
the URL from the saved port at startup. A port already in `.env` stays fixed. If
manually choosing it, use an unused port different from SQL and CACHE, for example:

```dotenv
REDIS_STATE_PORT=6380
MAIN_ADMIN_ID=YOUR_NUMERIC_TELEGRAM_USER_ID
CACHE_TTL_SECONDS=300
ACTIVITY_FLUSH_SECONDS=45
```

`MAIN_ADMIN_ID` must be your positive numeric Telegram user ID, not a bot ID,
username, phone number or group ID. Replace the placeholder before startup. After
`bot_ready`, open the bot's **private chat** and send `/admin`. The main admin can
grant delegated read/health access from the panel. Role-changing, ban, broadcast,
CSV, settings and leave operations remain restricted to the configured main admin.

To start/check just the new persistent STATE service after configuration:

```bash
docker compose --env-file .env -p saintcrispy up -d --wait redis-state
docker compose -p saintcrispy exec -T redis-state redis-cli ping
docker compose -p saintcrispy port redis-state 6379
docker compose -p saintcrispy logs --tail 100 redis-state
```

Expected ping is `PONG`. All published services bind to `127.0.0.1`. No inbound
firewall port is needed for the panel or STATE. `redis` remains evictable CACHE;
`redis-state` uses its own `redis_state_data` volume, AOF every second and
`noeviction`. Do not point STATE at the CACHE server. Optional
`REDIS_CACHE_MAXMEMORY` (default `512mb`) and `REDIS_STATE_MAXMEMORY` (default `1gb`)
control their Docker memory budgets; restart those services after changing them.
Monitor memory/stream backlog; a full STATE falls back to SQL activity rather than
trimming uncommitted events. Blank STATE URL **and** port disable batching and use
immediate SQL activity. The Python bot stays on the host and is restarted after
environment changes. No Node container or additional Python dependency is added.

Check lifecycle audit/history, SQL job counters, `admin_job_failed`,
`outbox_delivery_failed`, `outbox_outcome`, membership quarantine/worker errors and
cache fallback logs when validating the rollout. Group UNKNOWN checks must not
change groups to removed. The membership page lists observed humans, not a complete
Telegram roster. See [administration contracts and limits](ADMINISTRATION.md).

## Upgrade an existing Linux installation

Stop the currently running Python process through its terminal or supervisor. Keep `.env`, the existing database volumes, cookies and session files.

Normal code updates and bot restarts preserve PostgreSQL file references, user history and saved quality menus. Keep the same Compose project name (default `saintcrispy`) and PostgreSQL volume; switching project names attaches a different volume. Never use `docker compose down -v` or delete the database volume during an upgrade. Redis is a disposable cache and repopulates from PostgreSQL. Telegram files remain on Telegram; temporary preparation files are cleaned up after their transfer.

```bash
cd ~/SaintCrispy
git pull --ff-only
. .venv/bin/activate
python -m pip install -e .
node --version
ffmpeg -version
python tools/setup_services.py
docker compose -p saintcrispy ps
python scripts/migrate.py
python main.py
```

Startup applies additive migrations, authenticates or reuses the account-scoped session, recovers abandoned conversation states, builds independent provider sessions and registers handlers. Wait for `bot_ready` before testing a link. A link can be the user's first message; `/start` is optional.

For YouTube/Instagram failures, run `python tools/diagnose_video_access.py --network`
in the same virtual environment as the bot. `deployment_issues` reports stale pinned
packages or an unavailable selected JavaScript runtime. Git pulls do not reinstall
Python packages; use `python -m pip install --upgrade -e .`. A version-manager Node
installation must be on the supervisor's PATH, or `YOUTUBE_JS_RUNTIME` must specify
its absolute executable path. Header-only connectivity does not prove extraction;
use `--live` once after a meaningful setup change to verify full origin streams.
See the dated [access investigation](YOUTUBE_INSTAGRAM_ACCESS.md) for safe report
interpretation and the unresolved session/route conditions.

## Replaying download performance locally

Native split progressive YouTube tracks use 8 MiB HTTP requests and one local
codec-copy mux. Disk staging/read batches are 256 KiB. The guest converter ignores
ranges and keeps one capped GET. No concurrency setting needs to increase.

To compare the old and new split-track pipelines using an authorized complete
local MP4 (at most 128 MiB), without origin requests or Telegram access:

```bash
python tools/benchmark_video_pipeline.py /path/to/complete.mp4 --report .runtime/video-replay.json
python tools/benchmark_video_pipeline.py /path/to/complete.mp4 --large-request-mib-per-second 4 --report .runtime/video-throttled-replay.json
```

FFmpeg and ffprobe must be on PATH, or specified with `--ffmpeg` and `--ffprobe`.
The first command has no rate cap; the second explicitly simulates throttling of
requests larger than 10 MB. Request latency defaults to 40 ms and is adjustable
through `--request-delay-ms`. Reports include full duration, bytes, range size,
HTTP request count and stage times. These are loopback replays with simulated
Telegram acknowledgments, not production bandwidth tests. A favorable replay does
not prove a live relay speedup. See [dated evidence](YOUTUBE_INSTAGRAM_ACCESS.md#october-15-complete-file-performance-comparison)
for positive and negative results, including the still limited live guest relay.

## Optional guest network profiles

As of October 17, YouTube is available to all users, including when `MAIN_ADMIN_ID`
is empty. Start/sites/help menus reflect that availability. This supersedes the
temporary October 15 owner-only policy. Normal user/group quotas, maintenance,
bans and quality-button ownership still apply. `MAIN_ADMIN_ID` controls private
installation administration and lifecycle reports.

YouTube's anonymous converter route is enabled through
`YOUTUBE_RESOLVER_URL=https://cnv.cx`; set it empty to disable it. With default
guest settings, exact watch-page metadata reporting guest playability allows this
verified route before launching native extraction. Explicit player-client/PO-token
settings and account pools retain native-first inspection. No account cookies,
API credentials or login are sent. The resolver receives the canonical video URL.
Metadata comes first from the requested YouTube watch page, whose embedded player
must match the exact ID and provide a valid title, author and duration. Missing
origin metadata permits a bounded exact-ID search fallback; a search miss alone
does not prove that the video is unavailable. Download destinations are
restricted to the verified HTTPS service domain and exact video ID. Clear complete
H264/audio profiles from that exact playable page can certify a converter-reported
actual rendition for the menu. The converter's filename alone is insufficient:
its height must match one unambiguous origin size and not exceed the request.
Private/login/live responses, DRM, missing or truncated format durations and
inconsistent metadata cannot use this optimization. Those cases retain bounded
file sampling. Converted MP3 is labeled separately from native original audio.

Catalog-backed source candidates remain internal and unverified. Selection must
sample the chosen MP4/MP3 before returning any source to transfer infrastructure;
dimensions, H264 and audio must match. It reuses a still-fresh candidate to avoid
another conversion, or converts only the selected rendition after expiry. Full
transfer integrity/preparation remains mandatory. A failed selection clears both
snapshots and requires byte-based discovery for that identity for 60 seconds;
this bounded fallback record holds at most 128 identities. Other renditions and
providers retain their discovery paths, admission limits and session boundaries.

The service's signed links last approximately one minute. The provider retains a
source snapshot for at most 15 seconds and regenerates expired selections
before transfer. Concurrent inspection/refresh for the same identity coalesces
under bounded locks; conversion concurrency is two. Native HTTP 429 stops public
fallback, and public 429 starts a 60-second cooldown. No retry loop attempts to
turn application concurrency into unlimited public-service throughput. Disabled
or authenticated clients never send their URLs through this public path.

Complete verified guest quality catalogs have a separate bounded 128-entry cache.
`YOUTUBE_CATALOG_TTL_SECONDS` defaults to 300, accepts 0-900 and does not extend
signed URL validity. Zero retains only the existing 15-second menu snapshot. A
partial/failed discovery uses 15 seconds regardless of the longer setting. Source
refresh for one quality cannot create or replace a complete catalog. After source
expiry, inspection can return all verified choices without four new conversions;
selection obtains and validates only the requested source. Failed selection
refresh clears both caches so the next inspection rediscovers available choices.
Catalog expiry is fixed from discovery, not extended on every cache hit. Restart
clears this provider-local cache. Native/authenticated extraction is unchanged.

Instagram quality discovery stops once a complete MP4 header supplies dimensions
and duration. If payload precedes the index, it reads a 64 KiB suffix and expands
once to the existing 1 MiB bound only if needed. Within an inspection/selection,
equal samples (including shared DASH audio) coalesce under provider-owned locks
and a 4 MiB/128-result cache. The cache is discarded before the next operation;
selected source dimensions/audio and full-transfer integrity remain checked.
Unrelated account/post refreshes no longer share a network lock. Quality menus
send text/buttons without waiting for Telegram to fetch an external thumbnail;
final-file artwork is unchanged.

`INSTAGRAM_NATIVE_TIMEOUT_SECONDS=8` bounds guest native public-post work before
the anonymous fallback; accepted range 1-35. It does not shorten authenticated or
story inspection. Restore 35 if a healthy native route needs more time. After
verified fallback, only that exact identity prefers it for five minutes; other
posts still discover native renditions. Preference is capped at 128 entries, does
not extend source URL validity and clears on failure. Failure permits native
recovery once; rate limits stop alternatives. Queue waits count against the
existing HTTP/resolver deadlines. No public-service quota is implied.

Safe Instagram logs separate `instagram_native_attempt` budget/elapsed time,
resolver attempt stage/elapsed time, and `quality_menu_shown.elapsed_seconds` after
the menu sends. `instagram_probe_batch` records `uncached_samples`,
`sample_cache_hits`, `returned_sample_bytes` and `cached_sample_bytes` without
URLs or cookies. Returned sample bytes exclude headers, native buffering and any
earlier failed transport; they are not total interface traffic. See the
[dated replay and live limitations](YOUTUBE_INSTAGRAM_ACCESS.md#october-15-instagram-quality-menu-latency).

The YouTube handler starts inspection immediately. If it is still pending after
150 ms, it sends a bounded acknowledgment and later edits that reply into a text
menu or safe error. Fast cache hits send one menu directly. Menus no longer wait
for an external thumbnail fetch; final-file thumbnail handling is unchanged.
Inspection tasks are canceled/awaited on handler failure or shutdown. Activity
events `youtube_link_acknowledged` and `quality_menu_shown` include separate
`elapsed_seconds`; `youtube_catalog_reused` identifies complete-catalog reuse.
Measure these separately from network download/upload. An acknowledgment is not
a quality choice, extraction success or a production response-time guarantee.

Guest native extraction has an eight-second budget before public fallback
(`YOUTUBE_NATIVE_TIMEOUT_SECONDS`, accepted range 1-45). Increase it if healthy
native extraction on your connection needs more time. The initial origin metadata
preflight counts against that budget. Origin reads have a three-second deadline
including capacity waits and stop at 4 MiB or a complete player object. Four
transport slots, 32 fixed locks and a 128-entry LRU coalesce concurrent duplicates;
positive/negative metadata live for 60/30 seconds. Origin 429 establishes a
60-second cooldown checked again after capacity acquisition to stop queued bursts.
The search fallback has its own three-second deadline. With a public resolver,
one rejected native strategy is enough to switch routes; disabling the resolver
retains the alternate-player retry. After successful guest recovery, the provider
prefers that accepted public route for five minutes across requests. A public
failure clears the preference and allows native recovery; native account pools
retain their own failover path, and HTTP 429 still stops alternatives.

Metadata and a fresh anonymous request nonce are fetched concurrently. If either
fails, the unfinished sibling is canceled and awaited rather than delaying the
error until a slow nonce request completes. Origin metadata uses its own anonymous
browser transport, without the public service's headers or any account jar. Safe
`youtube_origin_metadata` / `youtube_origin_metadata_failed` events record elapsed
time, availability or error/status without URL values. The same
nonce was verified for all four conversions of one video; it is never reused
across identities or persisted. Conversion calls remain capped at two, while CDN
header probes run independently and stop when the complete required header is
available. Safe `youtube_public_response` and `youtube_public_probe` events record
operation/quality, status/sample bytes and elapsed time, without URLs or keys.
`youtube_public_extracted.deferred_probes` counts catalog-backed candidates whose
file samples will run on selection. A value of zero means ordinary byte-based
discovery was needed; do not expect the catalog-backed speedup in that case.
`youtube_public_quality_unavailable.http_status` distinguishes converter/CDN HTTP
errors without disclosing a signed URL or error response body. Discovery skips
unused file streams; it does not raise the two-conversion limit or guarantee a
public-service quota or CDN throughput.

An existing verified header sample now carries its size-probe result into delivery.
A known size is enforced; an unknown size uses the byte-capped local stream,
without repeating HEAD/Range requests or asking Telegram to fetch an unlimited
unknown-size URL. Nonstreamable progressive videos reuse that one response for
preparation instead of reopening it through a second network/FFmpeg pass.

Download reaching 100% before upload starts is expected for fragmented MP4 that
needs a classic seekable index. Preparation must finish first. The previous
1 MiB inspection bound rejected the reported long video after successful remuxing;
prepared-file inspection/integrity now allow a finite 16 MiB metadata bound.
Thumbnail capture includes that validated index plus a bounded frame prefix.
Ordinary network header inspection retains its 1 MiB bound. Actual remux work
holds a process slot; upload releases that slot for other preparations. Temporary
workspaces are removed on success, failure and cancellation. These temporary
full-file workspaces require sufficient host disk space for active videos.

See the [unindexed-video diagnosis and timings](YOUTUBE_INSTAGRAM_ACCESS.md#october-15-unindexed-video-metadata-and-menu-latency)
for actual workstation measurements and their production/Telegram limits.

Instagram's anonymous public-post fallback is enabled in application composition
by default. `INSTAGRAM_RESOLVER_URL=https://dwnld.nichind.dev` selects the tested
public Cobalt-compatible service; set the variable to an empty value to disable it,
or to your own compatible HTTPS endpoint. Only the canonical public URL is sent,
without account cookies. Third-party availability and quotas remain external
dependencies. Native HTTP 429 stops fallback; resolver 429 has a cooldown.

The reported reel passed complete native-project verification through this
fallback, including its original CDN, actual dimensions, 11.7-second duration,
required audio and the exact historical file hash. This fallback exposes one
verified progressive MP4 when native discovery is refused. It does not represent
a full native quality catalog or support arbitrary carousel responses. The first
callback reuses its short validated snapshot and still checks the CDN.

When that CDN's TCP connection fails, the provider can recover with HTTP/3 and
public DoH; the selected profile is carried into full progressive download and
size probing. It does not change host DNS, database ports or account sessions.
The curl transfer session has a bounded Python receive window and early-close
cancellation. No account cookie jar is copied to it.

Leave these settings empty/default on connections that already work. The October
15 workstation investigation found non-global system DNS answers and a separate
Instagram TCP/TLS failure. These opt-in settings address those transport conditions
without account login or changing the operating system's DNS configuration:

```dotenv
# HTTPS JSON DNS endpoint used by the isolated extractor and async transfer tunnel.
YOUTUBE_DOH_URL=https://dns.google/resolve

# Native libcurl DoH uses a DNS wire-format endpoint, not the JSON endpoint above.
INSTAGRAM_DOH_URL=https://dns.google/dns-query
INSTAGRAM_HTTP_VERSION=3
```

HTTP/3 requires working UDP/443 and support in the installed `curl-cffi` binary.
For native origin requests, `INSTAGRAM_HTTP_VERSION=3` forces it; `auto` preserves
the normal browser transport. Resolver CDN recovery can select it independently
after a failed TCP probe. HTTP/3 through an HTTP proxy depends on transport support;
the verified workstation profile used no proxy. Both Instagram options remain
inside Instagram's independent session. Account credentials are not introduced by
these options. The current recovery task is anonymous only.

`YOUTUBE_PROXY`, when configured, takes precedence over YouTube DoH. Without an
explicit proxy, the optional tunnel listens on `127.0.0.1` with an OS-selected port,
owned and closed by Python. It carries encrypted TLS to permitted YouTube/CDN
hosts only; it is not a public proxy or an additional Docker service. No fixed
origin IPs or Node container are required. PostgreSQL/Redis retain their existing
`.env` ports and passwords. DoH cache TTLs are capped, duplicate lookups coalesce
and failed lookups have a short cooldown; capacity remains bounded.

After changing the local `.env`, stop the old Python coordinator and run:

```bash
python tools/diagnose_video_access.py --network
python tools/diagnose_video_access.py --live
python main.py
```

The diagnostic reports profile-presence booleans without printing endpoint values,
credentials or signed URLs. The network check establishes only receipt of headers;
the live check attempts complete downloads and may fail at origin access. These
settings produced complete transfers of the reported YouTube video/audio from
accepted sources, but subsequent fresh native extraction was challenged. The later
anonymous resolver routes recovered fresh YouTube downloads and the exact Instagram
reel as described above.
Do not deploy these as a claimed universal login/403 fix or repeatedly run live tests under unchanged
conditions. See the [dated evidence](YOUTUBE_INSTAGRAM_ACCESS.md#anonymous-github-library-and-transport-comparison-october-15).

## Reference-layout and runtime recovery

The reference-layout upgrade moves application imports to root packages. Reinstall
the editable project after pulling so `core`, `db`, `schemas`, `repositories`,
`services`, `bot` and `container` are registered together with the provider package.
`scripts/migrate.py` uses the same idempotent migration ledger as startup and
needs only database configuration. It does not reset volumes or require Telegram
credentials. Existing ports, passwords, file references, menus, histories and
account-scoped session paths remain valid. Details are in
[the reference mapping](REFERENCE_MAPPING.md).

Missed-update recovery is requested after handler registration. A transient database/network startup failure or loss of the coordinator's database connection triggers a fresh, fully cleaned-up runtime with async backoff from 2 to 30 seconds. Invalid settings, wrong credentials and competing coordinators remain explicit startup failures. Ctrl+C/SIGTERM cancels recovery and performs shutdown. An unavailable database/network still prevents serving requests while it is down.

`SESSION_NAME` defaults to `.runtime/sessions/saintcrispy`. The bot appends its numeric
account ID. `CUSTOM_EMOJI_SET=IconsEmoji` also loads the thirteen curated packs in
the [premium emoji catalog](PREMIUM_EMOJI.md). Loading happens in the background
with bounded requests; a missing pack does not block readiness. Comma-separated
names/addemoji links can add operator preferences; empty disables custom emojis.
Permission errors fall back to Unicode for replies and edits. The start text uses
the actual username returned by Telegram, not a copied example handle.

Stop only database containers when necessary:

```bash
docker compose -p saintcrispy stop postgres redis
```

Bring them back with the configured ports/password:

```bash
docker compose -p saintcrispy up -d --wait postgres redis
```

Database volumes must remain intact during upgrades. Before a production upgrade, take a private PostgreSQL backup using the existing container credentials:

```bash
mkdir -p .runtime/backups
docker compose -p saintcrispy exec -T postgres \
  pg_dump -U downloader -d downloader -Fc > .runtime/backups/before-upgrade.dump
```

## Video playback while downloading

MP4 video messages include actual dimensions, duration, codec, audio presence and `supports_streaming` through Telethon 1.45.0. A complete classic MP4 index must precede the media. A front `moov` containing fragment defaults (`mvex`) or empty sample tables is insufficient for this guarantee. The bounded header inspector distinguishes these layouts. A ready progressive MP4 is uploaded unchanged.

For HLS/DASH video, fragmented MP4 or MP4 with its index at the end, the bot fully receives the selected source and validates its completeness before starting the upload. Within the existing FFmpeg process limit, a local codec-copy pass creates a regular MP4 with `-movflags +faststart`. Both video and required audio remain subject to duration and integrity checks; no re-encoding changes the selected quality. This replaces the previous fragmented-pipe preparation.

Direct URL registration remains preferred. Before publishing an MP4, its Telegram document must have usable video attributes and a thumbnail, and up to 1 MiB of its Telegram file header is inspected for a complete classic index. A verified old video with unknown `video_faststart` is checked once in the same way. Compatible files retain their document reference; fragmented or unsuitable files are replaced only after a successful new transfer. Migration 6 stores this nullable status in PostgreSQL and the SQL-first Redis cache. Existing files, users, conversations, quality menus and history are preserved.

Temporary staging uses the OS temporary directory (`TMPDIR` on Linux or `TEMP` on Windows). Provide disk space for up to `2 * REMUX_CONCURRENCY * MAX_FILE_SIZE_MB` MiB during preparation: one input and one output per active conversion. Defaults require roughly 4 GiB of temporary disk capacity at the configured 1 GiB file cap and two remux slots. The input is deleted before upload; the output is deleted after upload, failure or cancellation. The process slot stays held during upload to bound retained files. Only small chunks and bounded metadata/thumbnail buffers occupy RAM. Directory creation, reads, writes, stat and cleanup run in I/O workers; FFmpeg uses async subprocess APIs. Progress shows completed download, file preparation, then upload percentages.

Tests now use a seekable HTTP range input, matching the initialization pattern in [Telegram Desktop's streaming reader](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/media/streaming/media_streaming_file.cpp), which runs `avformat_find_stream_info` and seeks before declaring the file ready. With two-thirds of a neutral 30-second video's bytes inaccessible, the previous fragmented output requested future fragments and produced no frames. Uploaded classic MP4 from progressive, fragmented, DASH and HLS inputs decoded five seconds of video and audio under the same restriction. The earlier one-frame test on a nonseekable pipe did not establish this behavior. See the official [video attributes](https://core.telegram.org/constructor/documentAttributeVideo) and [FFmpeg MP4 formats](https://ffmpeg.org/ffmpeg-formats.html#mov_002c-mp4_002c-ismv).

On October 11, a neutral real-Telegram probe ran the production preparation/upload/registration path and fetched 131,072 of the registered file's 340,098 bytes. A seekable player decoded five seconds (123 video frames and 232 audio frames), while the rest of the file remained inaccessible. It published no chat messages. A prior attempt with a larger fixture failed on an MTProto connection loss and was not counted as a playback success. This checks actual Telegram-stored bytes; it does not verify the user's Desktop instance or the supplied source-specific clip. Existing messages retain their original bytes; request the same content/quality again after updating and restarting the coordinator to trigger cache repair.

Validation for this correction: 724 tests passed on Windows/Python 3.14 and 724 on Linux/Python 3.12 with real PostgreSQL/Redis. Lint and formatting passed. Linux used installed FFmpeg 7.1.5 through `IMAGEIO_FFMPEG_EXE=ffmpeg`, matching CI's selection of the installed runtime. An initial run using imageio's embedded Linux FFmpeg 7.0.2 failed six MPEG-TS HLS cases with a process crash (`SIGSEGV`); that binary is not part of the verified configuration. Tests cover continuous partial playback, old fragmented-cache repair, direct-URL index rejection, database/cache persistence, cancellation cleanup and event-loop responsiveness during a blocked disk write. No active coordinator was restarted during development.

Validation on 2026-10-09: 670 tests passed on Windows/Python 3.14 and 670 on Linux/Python 3.12, both with real PostgreSQL/Redis and FFmpeg. Lint and formatting checks passed. The suite includes adoption of both a legacy database and already-applied migrations 1/2, failed-repair preservation, and 1,000 concurrent requests sharing one legacy repair with concurrent follower publications. The update was integrated with upstream `main` at `2b30936`; no running coordinator was restarted during integration.

## Full menus and complete media (October 10 update)

Repeated links discover the provider's complete quality list after the short metadata
TTL or a restart. The list is independent of `media_files`; saving one quality never
removes other choices. If discovery fails temporarily, the complete redacted catalog
can restore a menu for ten seconds. An uncached selection refreshes its identity and
quality before resolving. `inspection_finished.path=database_fallback` includes the
safe `origin_error_type`/`origin_error_code` so an outage is visible in logs.

Migration 4 adds `media_files.verified_complete` with a false default. Old audio,
video and image references are rebuilt once on demand; old duration attributes alone
cannot prove a previously truncated file was complete. The replacement is saved only
after successful validation and publication. The unique content/quality/account key
and shared producer lock remain unchanged. No database, user history or menu is deleted.

### Video previews and server registration (October 10 follow-up)

The previous upload path supplied streaming flags and dimensions but no JPEG
thumbnail. A real Telegram registration of a neutral generated MP4 returned no
thumbnails when none was supplied, and one thumbnail when a JPEG was supplied.
Telegram Desktop's [video renderer](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/history/view/media/history_view_gif.cpp)
uses the thumbnail for the displayed aspect ratio; without it that calculation
falls back to a square. A requested streaming flag alone therefore does not
establish that a client has a correctly displayed video preview.

MP4 relay uploads now retain at most 1 MiB of bytes already being uploaded and
decode one frame in a cancellable FFmpeg process. The JPEG is bounded to 320 pixels
per side and 20 KiB, and is uploaded through the shared part window. There is no
second source download or local media file. Preview preparation shares the existing
FFmpeg concurrency bound and does not change the video or audio codecs.

Before publishing an uploaded MP4, the bot registers it with `UploadMediaRequest`
and checks Telegram's returned video attribute, positive dimensions/duration,
streaming flag, MIME type and thumbnail. URL registration also requires a usable
thumbnail; otherwise it falls back to the relay. Rejected registrations are never
published or saved as successfully delivered files. `video_registration_checked`
records these returned fields without file IDs, credentials or source URLs.
`preview_upload_attempt_bytes` and `preview_upload_acked_bytes` are separate from
the video payload counters.

Migration 5 adds nullable `media_files.video_preview`. A verified old reference with
an unknown preview is checked against its original Telegram message once. A usable
reference is retained; a missing preview or invalid video attribute is rebuilt once
from the selected source and quality. Failed repairs preserve the old reference.
Unverified legacy files still require the completeness repair introduced above.
Redis writes include the same preview state, and restarting uses a fresh cache
namespace. History, catalogs and existing messages are preserved. Request the video
again after updating and restarting the coordinator; old messages are not edited.

Restored menus may fill previously missing dimensions for the same content and
quality key. A change in a known dimension or content identity remains rejected.
Bounded MP4 header inspection also preserves oversized input chunks when replaying
the stream, rather than dropping the excess bytes.

The earlier neutral real-Telegram probe registered the production relay output with a
thumbnail and streaming attributes, then decoded its first frame from 16,384 of
39,965 bytes fetched back from Telegram. It published no chat messages. This verified
one-frame partial-file decoding and returned Telegram metadata, which did not establish continuous playback on a seekable player. It did not verify playback
in the user's Telegram Desktop instance or the supplied source-specific video.

Validation: 711 tests passed on Windows/Python 3.14 and 711 on Linux/Python 3.12
with real PostgreSQL/Redis and FFmpeg. Ruff lint/format checks passed. Coverage
includes preview persistence, missing-preview cache repair/reuse, invalid server
registration rejection, unknown-dimension menu recovery and 1,000-request deduplication.

For direct Telegram URL registration, known source size and expected audio/video
duration must match before publication. A shorter or unclassified document falls
back to the capped local stream. Progressive MP4 uploads validate complete boxes and
track duration. Fragmented MP4 validates the received video and required audio tracks
independently, including sample payload lengths, before finalizing the upload.
Progressive M4A/MP4 audio retains its original bytes and validates its audio track
directly. Other supported progressive audio uses the bounded FFmpeg queue; sources
requiring small HTTP ranges feed FFmpeg through a cancellable bounded pipe while
retaining the provider's proxy/headers. HLS/DASH still require successful process completion,
no skipped segments and a complete duration. MP4 preparation and audio validation use
codec copy, not re-encoding, and do not save a complete media file locally.

`transfer_finished` now reports `expected_media_seconds`, `received_media_seconds`
and `completeness_verified`, alongside sizes, stage timings and traffic totals.
FFmpeg output, HTTP input and mixed measurements are identified separately;
`progressive_input_bytes` measures HTTP input for piped remuxing. MP4 duration counts
both initial sample-table media and later fragments, including VP9 renditions.
Unknown-duration or opaque original files retain byte/EOF validation; origin metadata
and Telegram attributes cannot establish an independent duration in every format.

Stop the running Python coordinator, update and restart without deleting Docker volumes:

```bash
cd ~/SaintCrispy
git pull --ff-only
source .venv/bin/activate
python -m pip install -e .
docker compose -p saintcrispy up -d postgres redis
python main.py
```

The Python application applies the additive migrations during startup. Existing
verified files continue to use Telegram references; concurrent repair requests still
share one origin transfer. File decoding tests do not establish the playback behavior
of every Telegram client or unrestricted access from the production network.

## Logs

All destinations are local and excluded from Git. Serialization and disk I/O run in bounded background writer queues.

| Destination | Contents |
| --- | --- |
| `logs/system.log` | Startup/library events, severity, logger and safe exception frames |
| `logs/telegram.log` | Telegram/Telethon failures and custom emoji availability/fallback |
| `logs/activity.log` | Link admission, state transitions and safe handler failures, with request/user/chat correlation where available |
| `logs/performance.jsonl` | Exact payload bytes, download/upload/resolve/wait/save durations, transfer outcomes, batch totals, throughput and system samples |

Each line is JSON. Activity IDs are operational data and these logs should stay private. Raw library messages, exception strings, bot tokens, cookie values, signed CDN URLs and downloaded media are excluded. File/quality identity in performance logs is hashed. Rotation uses `LOG_MAX_MB` and `LOG_BACKUP_COUNT` for each stream: defaults retain about 220 MiB per stream, up to about 880 MiB across four streams. `LOG_FILE` changes the performance path and determines the directory used for application logs. Blank disables the performance file; application logs remain in `logs/`.

```bash
tail -f logs/performance.jsonl
tail -f logs/system.log logs/telegram.log logs/activity.log
python tools/summarize_logs.py 'logs/performance.jsonl*'
```

`metrics_interval` and `runtime_stopped` report queue drops/write failures; `application_log_streams` reports the same counters separately for the other three streams. Host network counters include traffic from other host processes. Payload download/upload counters belong to the bot. Telegram's direct URL fetch occurs on Telegram's network, so its download duration/bytes cannot be measured as local payload traffic. A forced kill can lose queued logs; graceful SIGTERM/Ctrl+C drains them.

## Quality-menu timing

Quality menus are text replies with link previews disabled. They do not wait for
Telegram to fetch a remote thumbnail before showing the selection buttons;
artwork/preview processing remains part of final-file delivery where appropriate.

Every accepted provider link first receives an inspection acknowledgment replying
to that original link, before cache/provider inspection starts. The same bot
message is edited into the quality text/buttons or a recoverable inspection
error, including cached requests. An Instagram album edits that message for its
first 50 choices and sends additional pages for the rest. If the user deleted
the acknowledgment, a replacement menu replies to the original link. Cancellation
or an unhandled storage failure attempts to remove an unfinished acknowledgment.
Acknowledgment sends and cleanup have five- and three-second deadlines; there
are no detached UI tasks or automatic retries of ambiguous message sends.

The menu continues to store the submitted link's message ID, not the bot's
acknowledgment/menu ID. Selected fresh downloads and cached resends reply to the
submitted link in private chats and groups, keeping the existing site-specific
caption and premium-emoji handling. The separate transfer-progress reporter
retains its existing completion behavior.

Pinterest, SoundCloud, XVideos and XNXX emit `quality_menu_shown` only after the
menu response succeeds. The activity record includes `site`, `qualities`,
`acknowledgment_seconds`, `inspection_seconds`, `menu_storage_seconds`,
`telegram_seconds` and total
`elapsed_seconds`. Inspection includes application cache/provider discovery;
menu storage includes durable menu creation and button/text building; Telegram
time measures the menu response RPC. Dispatcher admission happens before these
handler timings and is covered by the existing admission/performance logs.

Use these stages to distinguish a slow origin from SQL latency or a slow Telegram
response. The Pinterest discovery memo only reuses identical playlists within
that request; the selected rendition is still refreshed and validated separately.
See [the live workstation comparison](PROVIDER_VERIFICATION.md#october-15-2026-quality-menu-latency)
for measured changes and the boundary between origin and simulated Telegram tests.

## Downloaded file expiry

Confirmed user-facing files are scheduled for deletion 30 seconds after the send
completes, for all providers and fresh/cached delivery paths. This includes video,
audio, photos and documents. The final caption tells the recipient to save or
forward the file to Saved Messages immediately. Link submissions, inspection
acknowledgments, quality menus, progress/status messages and callback notifications
are not scheduled for deletion. Existing messages from before this version are
not scanned or retroactively removed.

`file_message_expiry` records the bot account, destination peer, confirmed sent
message ID and UTC deadline. Migration 8 creates this queue automatically at
startup; it is also applied by `python scripts/migrate.py`. No new environment
setting is needed. One coordinator-owned worker recovers overdue rows after a
restart, polls every half second and batches at most 100 IDs for the same peer,
with at most four simultaneous deletion requests. It never uses the submitted
link's message ID as a deletion target.

Telegram deletion is revoked for the chat, not just hidden locally. API delays,
lost delete permission and FloodWait can delay completion beyond the requested
deadline; failures stay queued with bounded backoff. Inspect
`file_messages_expired`, `file_expiry_delete_failed`, `file_expiry_persist_failed`
and `file_expiry_worker_failed`. Cleanup persistence failures never retry file
publication. A small pending registry lets the worker retry an interrupted or
failed queue write; shutdown attempts a bounded flush before closing the database.

After confirmed deletion, matching file references are removed from PostgreSQL
and invalidated in Redis under their key locks. Completed queue tombstones remain
for two days to reject late cache writes referencing an expired message. A future
request can download the source again. Metadata/history remain stored. An internal
`MEDIA_STORAGE_CHAT` copy is not expired and does not show the expiry notice;
recipient copies still expire, keeping the internal refresh anchor usable.

The worker also verifies both stored deadlines against the application clock;
SQL's due query alone is insufficient when database and application clocks differ.
Keep the hosts synchronized. If Telegram explicitly rejects a cached reference
and refreshing its origin cannot recover it, the request invalidates only that
old anchor and rebuilds once under the existing content lock. A missing original
message therefore no longer leaves subsequent requests stuck on that cached file.
An ambiguous publication timeout or a write-permission error never starts this
repair, avoiding duplicate sends after uncertain RPC outcomes.

## Transfer status messages

All providers use the same progress reporter. Successful publication ends with
the completed status and removes the stop keyboard. Conversation-state persistence
runs afterward. The reporter drains its current edit before finalizing, so a
previous progress edit is not deliberately cancelled during normal completion.

Terminal edits get at most three attempts in a 15-second budget; each edit has a
three-second deadline. Short Telegram edit cooldowns are honored. Terminal edits
and Stop callbacks remove inline buttons with explicit `buttons=None`; an empty
`ReplyInlineMarkup` can be rejected with `ReplyMarkupInvalidError`.

If the transfer handler cannot confirm its terminal edit, it attempts one separate
terminal-text reply to the original link, with a three-second deadline. Only after
that reply succeeds does it attempt bounded deletion of the stale status. A failed
or ambiguous replacement reply preserves the original status and is never retried.
This only affects text status messages; media publication is never repeated to
repair a status. There is no detached retry task or unbounded finalization queue.

Inspect these events in `logs/telegram.log`, correlated by `transfer_id`:

| Event | Meaning |
| --- | --- |
| `transfer_status_finalized` | Telegram accepted the terminal text/keyboard, or reported it already applied |
| `transfer_status_retry` | A terminal edit was not confirmed; includes safe error identity and attempt number |
| `transfer_status_unconfirmed` | The edit budget/cooldown/permanent failure prevented confirmation |
| `transfer_status_replied` | A separate terminal-text reply was confirmed before old-status cleanup |
| `transfer_status_reply_failed` | The replacement was unconfirmed; the original status was retained |
| `transfer_status_removed` | The obsolete progress message was deleted successfully |
| `transfer_status_cleanup_failed` | Telegram also rejected/timed out deleting the old status |

These events describe the status message. Use `transfer_finished` for the media
transfer result, byte counts and timings. Updating this code repairs future
transfers after restarting Python; it does not recover previously orphaned status
messages whose identifiers were not retained by the running reporter.

## Origin checks on the deployment server

Incoming link handlers are capped before database admission. Configure
`MAX_CONCURRENT_REQUESTS=1000` for 1,000 active link handlers and the separate
existing transfer-task cap. `POSTGRES_POOL_SIZE` defaults to 32 (range 2-64,
including one coordinator connection); keep total connections from all processes
within the database server's configured limit. `METADATA_CONCURRENCY=8` limits
origin extraction separately. Increasing it without origin/network/resource
evidence does not guarantee 1,000 cold inspections per second.

Performance intervals now include `admission_database_checks`,
`admission_local_rejections`, `link_handler_capacity_rejections`,
`link_error_replies_suppressed` and `link_error_reply_failures`. Controlled overload
does not consume user admission. Repeated warning suppression affects rejected
requests only. Watch these counters together with p95 response latency and
history/menu counts, rather than reporting a rejection as a completed download.

For safe local arrival tests using a dedicated test database, see
[the throughput investigation](REQUEST_THROUGHPUT.md). Its real SQL tests simulate
origins and Telegram; the cold 1,000/s all-menu target and live origin acceptance
must be measured independently on the actual deployment resources.

Run these from the same host/egress as the bot. Origin/CDN access can differ between a workstation, GitHub runner and production server.

```bash
python tools/audit_providers.py --all-qualities --stream \
  --max-stream-mib 1024 --case-timeout 900 --transfer-timeout 240 \
  --url youtube='https://www.youtube.com/watch?v=l6zflbTGNFQ' \
  --output .runtime/provider-audit.json
```

Add `--telegram --stream-mismatches` to verify Telegram file registration. This uses `UploadMedia`, sends no chat messages and prints no signed URLs or credentials. A passed extraction alone is weaker evidence than a complete stream and Telegram registration.

For `youtube_login_required`, use an authorized Netscape cookie file and/or a working HTTP proxy in the owning YouTube settings. JavaScript challenge solving through Node and a PO token are separate mechanisms. If `YOUTUBE_POT_BASE_URL` is configured and `YOUTUBE_PLAYER_CLIENTS` is blank, the provider now selects `mweb,web_safari` so the helper is used by suitable clients. An explicit client choice remains authoritative. A PO helper or cookie does not guarantee that a datacenter IP can access every video/CDN.

The October 14 dependency pin is yt-dlp `2026.10.10.233141.dev0`, the official
nightly with the new default `tv_samsung` client. Run `python -m pip install -e .`
after pulling; pulling alone leaves the older extractor installed. A stale explicit
`YOUTUBE_PLAYER_CLIENTS` setting prevents the default set from taking effect. The
independent Instagram client also supports the current Instagrapi web document;
`INSTAGRAM_WEB_DOC_ID` is optional and defaults to `27830990013244856`. Read
[the knowledge base](KNOWLEDGE_BASE.md) before repeating previously failed methods.

For SoundCloud restricted tracks, the provider tries complete supported streams and the creator's original download when available. An authorized `SOUNDCLOUD_OAUTH_TOKEN` may expose additional formats according to the account's access. Preview snippets are not advertised as full tracks. DRM-only full streams and blocked original downloads remain explicit failures; supplying a different song from a search result would violate the requested content identity.
