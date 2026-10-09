# Application architecture

The reference reviewed for this change is the local Couplyo project: its README, migration/deployment runbook, Telegram bot checklist, composition root, logging configuration, conversation manager/storage, PostgreSQL engine/unit of work, and Redis pool/cache base. The package uses the same application layer names inside `src/downloader_bot`; provider ownership stays at the path required by `AGENTS.md`.

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
| `bot/handlers/`, `bot/state/`, `bot/jobs/` | Telegram I/O, explicit conversation state and bounded transfer tasks |
| Texts and Telegram adapters | Start copy, quality replies, progress, cancellation and custom emoji fallback |

Provider handlers remain in their owning provider, as explicitly required. Site-specific clients, cookies, parsing, format discovery and HLS rules are never shared between providers. Shared Telegram presentation/transport code is infrastructure, not an extractor.

Handlers do not execute SQL or Redis commands. Business services do not import Telethon. PostgreSQL repositories do not depend on Telegram or Redis. Architecture tests enforce these directions and preserve each provider's six independent files.

## Deliberate backend choices

The database driver remains asyncpg and the established tables and primary keys remain intact. Switching a working production database to Couplyo's application-specific SQLAlchemy models would add migration risk without improving download identity or atomic admission. The layer structure and transaction rules match the reference, while the downloader schema remains its own domain.

Migrations are additive and tracked in `app_schema_migrations`. Startup acquires a transaction-scoped migration lock and applies unapplied versions in one transaction. Version 1 adopts the existing tables without deleting their rows; version 2 adds users, conversations and owned menus. Catalog alias writes continue to use one SQL transaction. File and history writes retain unique-key upserts. Tests cover adoption of an existing database and concurrent migration attempts.

Redis is an optional, evictable accelerator. PostgreSQL is authoritative for files, admission, conversations and menus. This provides the reference's durable, non-expiring conversation semantics without introducing a third Docker database or relying on Redis availability for state. `REDIS_CACHE_URL` can select an independent cache endpoint; blank uses the existing URL derived from `.env` ports.

## Conversation and transfer flow

1. Any supported link registers the user, with `is_started=false` unless `/start` was previously used. This flag is analytics, never authorization.
2. Atomic rate admission allows one inspection per configured interval across all chats. The state becomes `INSPECTING` with a request correlation ID.
3. A successful inspection persists the owned quality menu and enters `CHOOSING_QUALITY`. A failed inspection becomes `FAILED` only if it still owns the current operation.
4. A quality callback validates the bot, owner, chat and index before reserving a bounded transfer task. The state becomes `TRANSFERRING` with its stable transfer ID.
5. Existing Telegram references are reused first. Concurrent requests for a missing content/quality share one producer. Once the reference is saved, follower publications happen concurrently outside the producer lock.
6. Completion sets `DONE`, `FAILED` or `CANCELLED` only when the same transfer still owns the conversation. An older completion cannot overwrite a later link or transfer.

Conversation rows and menus have no expiry. The hot menu cache is bounded and can evict entries because SQL stores the recoverable identity. Durable menus exclude signed media/thumbnail URLs and authorization values. After restart, stored files can be sent without contacting the origin. A missing file causes a fresh provider inspection; content ID, quality key and dimensions must match before download.

Startup marks abandoned `INSPECTING` and `TRANSFERRING` states `INTERRUPTED`. It does not replay an uncertain Telegram publication. Graceful shutdown stops new jobs, cancels cancellable transfers, waits for final publication/persistence, closes network/database resources and drains log queues. Account-scoped Telegram sessions avoid needless repeated bot login and preserve update/entity state. Missed-update recovery runs after handlers are registered. A transient database/network failure rebuilds the coordinator after full cleanup with bounded async backoff; fatal configuration/authentication failures stay explicit.

Telegram publication and SQL commit are different systems. A forced crash between them can leave an already-published file without a saved reference. There is no unconditional exactly-once cross-system guarantee. Unknown publication failures are not blindly retried.

## Load and observability

The existing 1,000-request task cap, separate metadata/remux limits, bounded upload part window, per-user admission and cancellation isolation are retained. Pure transfer state is a DTO; formatting and RPCs stay in `bot/`. Queue-backed system, Telegram and activity logs follow the reference's stream separation. Performance events retain byte counters, per-stage durations, batch summaries and host network measurements. See the operations guide for destinations and interpretation.
