# Deployment, logging and recovery

The Python bot and Node runtime run on the host. Docker runs PostgreSQL and Redis only. Existing ports and password stay in `.env`; Python derives connection URLs from those values.

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

The reference-layout upgrade moves application imports to root packages. Reinstall
the editable project after pulling so `core`, `db`, `schemas`, `repositories`,
`services`, `bot` and `container` are registered together with the provider package.
`scripts/migrate.py` uses the same idempotent six-revision ledger as startup and
needs only database configuration. It does not reset volumes or require Telegram
credentials. Existing ports, passwords, file references, menus, histories and
account-scoped session paths remain valid. Details are in
[the reference mapping](REFERENCE_MAPPING.md).

Missed-update recovery is requested after handler registration. A transient database/network startup failure or loss of the coordinator's database connection triggers a fresh, fully cleaned-up runtime with async backoff from 2 to 30 seconds. Invalid settings, wrong credentials and competing coordinators remain explicit startup failures. Ctrl+C/SIGTERM cancels recovery and performs shutdown. An unavailable database/network still prevents serving requests while it is down.

`SESSION_NAME` defaults to `.runtime/sessions/saintcrispy`. The bot appends its numeric account ID. `CUSTOM_EMOJI_SET=IconsEmoji` reads the requested pack. Empty disables custom emojis. Telegram may require Premium/Fragment privileges for custom entities; permission errors fall back to normal Unicode emojis automatically. The start text uses the actual username returned by Telegram, not a copied example handle.

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
