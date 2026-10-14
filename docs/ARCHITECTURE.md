# Application architecture

The reference is Couplyo's `docs/PROJECT_REFERENCE_FA.md`, with its composition
root, transaction, FSM, dispatcher, cache and async lifecycle contracts. Application
layers now live at the repository root: `core`, `db`, `schemas`, `repositories`,
`services` and `bot`, beside `main.py` and `container.py`. Only the independent
provider package and a small module entry remain in `src/downloader_bot`.
See the [section-by-section mapping](REFERENCE_MAPPING.md) for implementation
details and domain-specific choices.

## Layer responsibilities

| Couplyo pattern | SaintCrispy implementation |
| --- | --- |
| `main.py` and `container.py` | Host `.env` bootstrap, small package entry, composition root and `AsyncExitStack` lifecycle |
| `core/` | Configuration, credential-safe diagnostics, logging queues, request context and keyed locks |
| `schemas/` | Frozen media/quality/source/document DTOs and framework-independent transfer counters |
| `db/postgres/`, `db/redis/` | Bounded connection pools and transactional startup migrations |
| `repositories/interfaces/` | Storage, history, catalog and delivery contracts |
| PostgreSQL repositories | Media references, accounts, histories, users, menus and conversations |
| Redis repositories | Cache-aside reads, SQL-first writes and SQL-authoritative rate admission |
| `services/` | Download coordination, user registration, admission policies and observability |
| `bot/handlers/dispatcher.py`, `bot/state/`, `bot/jobs.py` | Isolated update routing, scoped FSM and bounded transfer tasks |
| Texts and Telegram adapters | Start copy, quality replies, progress, cancellation and custom emoji fallback |

Provider handlers remain in their owning provider, as explicitly required. Site-specific clients, cookies, parsing, format discovery and HLS rules are never shared between providers. Shared Telegram presentation/transport code is infrastructure, not an extractor.

Handlers do not execute SQL or Redis commands. Business services do not import Telethon. PostgreSQL repositories do not depend on Telegram or Redis. Architecture tests enforce these directions and preserve each provider's six independent files.

## Deliberate backend choices

The database driver remains native asyncpg. `db/postgres/models` owns executable
table/constraint definitions; `migrations.py` retains the existing six revisions.
`Database` owns the asynchronous pool. `PostgresUnitOfWork` acquires one private
connection, binds its repositories to that connection and commits or rolls back
on context exit, including cancellation. The container injects a factory into
services; `/start` uses one transaction for user registration and MAIN state.
Single-statement hot-path writes stay atomic without holding a transaction across
origin extraction or Telegram calls. No existing table, primary key or migration
history is replaced.

Migrations are additive and tracked in `app_schema_migrations`. Startup acquires a transaction-scoped migration lock and applies unapplied versions in one transaction. Version 1 adopts existing tables; version 2 adds users, conversations and owned menus; version 3 adds `video_streaming`; version 4 adds `verified_complete` with a false default for legacy references; version 5 adds nullable `video_preview`; version 6 adds nullable `video_faststart` for a complete classic MP4 index at the front. Older video references are checked or repaired on demand. Catalog alias writes use one SQL transaction. File and history writes retain unique-key upserts. Tests cover adoption of an existing database and concurrent migration attempts.

Redis is an optional, evictable accelerator. PostgreSQL is authoritative for files, admission, conversations and menus. This provides the reference's durable, non-expiring conversation semantics without introducing a third Docker database or relying on Redis availability for state. `REDIS_CACHE_URL` can select an independent cache endpoint; blank uses the existing URL derived from `.env` ports.

The cache pool waits for capacity for up to two seconds, caps connections at 16 and
is owned/closed by its client. Central key factories and strict file envelopes
validate key identity, all DTO fields, integer/boolean types and binary values;
invalid or outdated cache objects are reconstructed from SQL. Cache eviction does
not erase durable FSM state.

`ConversationService` owns state orchestration. `bot/state/manager.py` is its
Telegram facade, with PostgreSQL and memory storage adapters. State includes bot,
user and chat scope. `update_data` merges atomically in SQL; an old operation cannot
overwrite a newer owner. Malformed state is retained for diagnostics while reads
return safe MAIN state. `get_state`, `get_data`, `set_state`, `update_data`, `finish`
and `reset` match the reference's manager contract with explicit chat scope.

The dispatcher checks user context and contains handler failures; cancellation
propagates to shutdown. Texts, keyboards, emoji entities and previews have separate
owners. HTTP, PostgreSQL, Redis and FFmpeg I/O are asynchronous. Session construction
and close, video staging, system sampling and log writes run outside the event loop.
Pure parsing, validation and button formatting remain synchronous functions.

## Conversation and transfer flow

1. Any supported link registers the user, with `is_started=false` unless `/start` was previously used. This flag is analytics, never authorization.
2. Atomic rate admission allows one inspection per configured interval across all chats. The state becomes `INSPECTING` with a request correlation ID.
3. A successful inspection persists the owned quality menu and enters `CHOOSING_QUALITY`. A failed inspection becomes `FAILED` only if it still owns the current operation.
4. A quality callback validates the bot, owner, chat and index before reserving a bounded transfer task. The state becomes `TRANSFERRING` with its stable transfer ID.
5. Verified Telegram references are reused first. Legacy references are rebuilt once from the same content and quality; source-message duration attributes cannot certify an old truncated file. Complete quality discovery precedes the catalog outage fallback, so saving one quality does not hide others. Concurrent requests for a missing or legacy content/quality share one producer. Once a validated reference is saved, follower publications happen concurrently outside the producer lock.
6. Completion sets `DONE`, `FAILED` or `CANCELLED` only when the same transfer still owns the conversation. An older completion cannot overwrite a later link or transfer.

Conversation rows and menus have no expiry. The hot menu cache is bounded and can evict entries because SQL stores the recoverable identity. Durable menus exclude signed media/thumbnail URLs and authorization values. After restart, stored files can be sent without contacting the origin. A missing file causes a fresh provider inspection; content ID, quality key and known dimensions must match before download. Previously unknown dimensions may be filled from the same quality.

MP4 relay uploads generate a bounded JPEG thumbnail from their already-transferred prefix in a separate FFmpeg process. The bot registers the uploaded video and verifies the returned Telegram video attribute and thumbnail before publishing. Playback readiness also requires a complete classic MP4 sample index before the media; a fragmented index or streaming flag alone does not establish this. Video needing conversion is fully received and validated in a capped temporary workspace, copied to `+faststart` without re-encoding, then uploaded. Disk operations run off the event loop; cancellation waits for an active disk operation before removing the workspace. The existing remux bound also bounds temporary workspaces. Direct registration and old verified references inspect at most 1 MiB of Telegram's file header; valid classic files remain reusable, fragmented files share the one-producer repair path. Preview preparation shares the process bound, and its part upload shares the global upload window.

Startup marks abandoned `INSPECTING` and `TRANSFERRING` states `INTERRUPTED`. It does not replay an uncertain Telegram publication. Graceful shutdown stops new jobs, cancels cancellable transfers, waits for final publication/persistence, closes network/database resources and drains log queues. Account-scoped Telegram sessions avoid needless repeated bot login and preserve update/entity state. Missed-update recovery runs after handlers are registered. A transient database/network failure rebuilds the coordinator after full cleanup with bounded async backoff; fatal configuration/authentication failures stay explicit.

Telegram publication and SQL commit are different systems. A forced crash between them can leave an already-published file without a saved reference. There is no unconditional exactly-once cross-system guarantee. Unknown publication failures are not blindly retried.

## Load and observability

The existing 1,000-request task cap, separate metadata/remux limits, bounded upload part window, per-user admission and cancellation isolation are retained. Pure transfer state is a DTO; formatting and RPCs stay in `bot/`. Queue-backed system, Telegram and activity logs follow the reference's stream separation. Performance events retain byte counters, per-stage durations, batch summaries and host network measurements. See the operations guide for destinations and interpretation.

Link handlers have their own `MAX_CONCURRENT_REQUESTS` cap, reserved before SQL
admission. Excess requests do not consume the user's quota. Rejection replies have
a separate 32-RPC cap and five-second deadline; identical warning codes for the
same user/chat are suppressed for five seconds in a bounded 2,048-entry cache.
Accepted menus are never suppressed by this cache. Reply failures remain isolated,
and cancellation releases the handler slot.

The native SQL pool is configurable with `POSTGRES_POOL_SIZE` (default 32, range
2-64, including the coordinator). Cached link processing uses three database
statements: atomic admission/user/INSPECTING ownership, pending link history, then
atomic durable menu/history completion/CHOOSING_QUALITY. Request context carries
only the pending history hash and completion flag. Failure cleanup is guarded by
the operation's request ID; an old menu cannot replace a newer inspection. A
provider call adds one atomic catalog/alias statement. Explicit snapshots retain
all quality identity fields while excluding credentials and signed sources;
immutable scalar fields do not need recursive dataclass copies.

Process-local admission leases can only reject. Every fresh grant still requires
SQL, including after eviction or restart. Keyed admission locks merge concurrent
attempts by the same user/stage. The cache is bounded at 100,000 entries. Metadata
capacity counts distinct pending keys; duplicate followers share their producer.
The 90-second deadline includes keyed-lock and worker waits. The durable menu cache
uses ordered eviction rather than rebuilding every cached menu per request.

The [request-arrival evidence](REQUEST_THROUGHPUT.md) distinguishes concurrency,
cached request throughput, uncached origin throughput and actual Telegram limits.
