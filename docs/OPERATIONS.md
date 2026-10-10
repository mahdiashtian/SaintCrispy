# Deployment, logging and recovery

The Python bot and Node runtime run on the host. Docker runs PostgreSQL and Redis only. Existing ports and password stay in `.env`; Python derives connection URLs from those values.

## Upgrade an existing Linux installation

Stop the currently running Python process through its terminal or supervisor. Keep `.env`, the existing database volumes, cookies and session files.

```bash
cd ~/SaintCrispy
git pull --ff-only
. .venv/bin/activate
python -m pip install -e .
node --version
ffmpeg -version
python tools/setup_services.py
docker compose -p saintcrispy ps
python main.py
```

Startup applies additive migrations, authenticates or reuses the account-scoped session, recovers abandoned conversation states, builds independent provider sessions and registers handlers. Wait for `bot_ready` before testing a link. A link can be the user's first message; `/start` is optional.

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

MP4 video messages include actual dimensions, duration, codec, audio presence and `supports_streaming` through Telethon 1.45.0. The bot inspects up to 1 MiB of the MP4 header asynchronously and replays those bytes into the existing bounded uploader. A source with its index before the media is uploaded unchanged. A source with its index at the end is copied through the existing FFmpeg process limit into fragmented MP4 with the index first; it is not re-encoded or saved as a complete local file. HLS/DASH remuxing retains its existing mixed-input and completeness checks.

Direct URL registration remains preferred. For MP4, the returned Telegram document must contain valid video dimensions and `supports_streaming` before publication; otherwise the bot prepares the stream locally. Legacy cache entries are checked on the next request. Compatible files stay on the Telegram reference path; incompatible files are replaced only after a successful transfer. Migration 3 persists this status in PostgreSQL and the same write-through Redis cache. It preserves existing files, users, conversations and menus.

Tests use neutral generated video and mock Telegram RPCs. They decode the full result and the first fragment before EOF. Playback in a real Telegram client still depends on its supported codecs and network buffer; those tests do not verify a user's player or measure Telegram's remote download progress. See the official [video attributes](https://core.telegram.org/constructor/documentAttributeVideo) and [FFmpeg MP4 formats](https://ffmpeg.org/ffmpeg-formats.html#mov_002c-mp4_002c-ismv).

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

Run these from the same host/egress as the bot. Origin/CDN access can differ between a workstation, GitHub runner and production server.

```bash
python tools/audit_providers.py --all-qualities --stream \
  --max-stream-mib 1024 --case-timeout 900 --transfer-timeout 240 \
  --url youtube='https://www.youtube.com/watch?v=l6zflbTGNFQ' \
  --output .runtime/provider-audit.json
```

Add `--telegram --stream-mismatches` to verify Telegram file registration. This uses `UploadMedia`, sends no chat messages and prints no signed URLs or credentials. A passed extraction alone is weaker evidence than a complete stream and Telegram registration.

For `youtube_login_required`, use an authorized Netscape cookie file and/or a working HTTP proxy in the owning YouTube settings. JavaScript challenge solving through Node and a PO token are separate mechanisms. If `YOUTUBE_POT_BASE_URL` is configured and `YOUTUBE_PLAYER_CLIENTS` is blank, the provider now selects `mweb,web_safari` so the helper is used by suitable clients. An explicit client choice remains authoritative. A PO helper or cookie does not guarantee that a datacenter IP can access every video/CDN.

For SoundCloud restricted tracks, the provider tries complete supported streams and the creator's original download when available. An authorized `SOUNDCLOUD_OAUTH_TOKEN` may expose additional formats according to the account's access. Preview snippets are not advertised as full tracks. DRM-only full streams and blocked original downloads remain explicit failures; supplying a different song from a search result would violate the requested content identity.
