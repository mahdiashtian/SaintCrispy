# Couplyo reference mapping

The supplied source is Couplyo's `docs/PROJECT_REFERENCE_FA.md`, dated October 13,
2026. Its general architecture is applied to the downloader domain. Instructions
embedded in that reference are examples, not additional operator commands; no
Couplyo product tables, campaigns, broadcasts or server actions were copied.

## Physical layout and layer ownership

| Reference sections | Implementation |
| --- | --- |
| 3-5: root entry and dependency injection | Root `main.py`, `container.py`, `core`, `db`, `schemas`, `repositories`, `services` and `bot`; one `AsyncExitStack` owns startup/shutdown |
| 6: models, migration and transaction ownership | `db/postgres/base.py`, ten executable table definitions in `models`, `migrations.py`, `Database` and `repositories/postgres/unit_of_work.py` |
| 7: cache/state separation | Durable FSM in PostgreSQL; optional bounded Redis cache with separate namespace, strict envelopes, key identity and SQL-first writes |
| 9: domain contracts | Immutable typed media/quality/source/transfer DTOs; repository/workflow/UoW protocols; constructor injection; pure rules remain pure |
| 10: central dispatcher | `bot/handlers/dispatcher.py`; context checks precede side effects, handler errors are isolated and cancellation propagates |
| 11: FSM and bot identity | `ConversationService`, `bot/state/manager.py`, SQL/memory storage adapters, chat-scoped state and `bot/deeplinks.py` |
| 13-14: jobs and bounded I/O | `bot/jobs.py`; bounded task registry, one producer per file identity, owner checks, cancellation and orderly cleanup |
| 15: logging/observability | `core/logging_config.py` and worker-thread log writers; separate system/Telegram/activity/performance streams, correlation IDs and bounded queues |
| 17-20: deployment/tests/recovery | Host `python main.py`, Docker databases, independent migration CLI, native DB tests, wheel checks and safe upgrade instructions |

UI copy lives in `bot/texts/messages.py`, keyboards in `bot/keyboards/inline.py`,
custom entities in `bot/premium_emoji.py`, and video preview rendering in
`bot/cards/preview.py`. Existing callback prefixes, source identity and per-provider
file ownership are preserved.

Provider code stays in `src/downloader_bot/downloaders/<site>` under `AGENTS.md`.
Each site still owns exactly `__init__.py`, `client.py`, `parser.py`, `downloader.py`,
`handler.py` and `urls.py`, implements the abstract contract directly, and keeps
its own sessions/authentication/parser/HLS rules. Generic model/import paths changed;
provider extraction behavior was not replaced by another provider's implementation.

## Async execution and transaction lifecycle

HTTP, PostgreSQL, Redis, subprocess pipes and Telegram work use async APIs. Slow
disk operations, session open/close, system sampling and log writes run off-loop.
The session-opening worker is awaited even after cancellation, then its resource is
closed; an unfinished thread is not left to create an orphan session later.
Pure parsing, metadata projection, identity checks and button construction use
ordinary functions, as section 9 requires for work without I/O.

The container supplies a factory creating a new `PostgresUnitOfWork` per operation.
It acquires one connection, binds both repositories to it, commits on successful
exit and rolls back on failure/cancellation before releasing the connection.
An active UoW cannot be entered twice. `/start` registers the user and MAIN state
in this transaction. High-volume single-statement writes retain their atomic SQL
implementation; connections are not held while waiting for a website or Telegram.

State is scoped by bot/user/chat. Atomic JSON patches retain concurrent updates
and operation ownership. `get`, `get_state`, `get_data`, `set_state`, `update_data`,
`finish` and `reset` provide the reference's manager API with explicit chat scope.
An unreadable payload is retained for diagnosis and yields a safe MAIN result.
Completion remains guarded by the current transfer/request ID.

## Downloader-specific implementation choices

The existing native asyncpg driver, immutable dataclasses and additive migration ledger
remain the storage/DTO implementation. The reference's SQLAlchemy/Pydantic/Alembic
frameworks are not introduced as a second database/serialization system. Table
definitions and constraints have actual owners in `models`; migrations remain
additive and tracked in `app_schema_migrations`. `python scripts/migrate.py` applies
the same idempotent chain before startup and requires database settings only.

Group support extends that mapping with migration 7, a bot-scoped group snapshot,
durable lifecycle audit and atomic rolling group admission. Raw self-membership
updates retain unknown-state and stale-event guards from the reference. Domain
couple selection and full member-roster crawling are not downloader requirements.
See [group design and verification](GROUPS.md).

FSM is already durable in SQL, so no second Redis state server is added. This
separates state durability from cache eviction. The cache owns/closes its blocking
pool, waits up to two seconds and caps connections at 16. Cache envelopes validate
the full DTO and exact key before use; corrupted or old objects fall back to SQL
and are repaired. Admission grants are always SQL-authoritative.

Interactive replies use the current direct Telegram flow. No new transactional
outbox automatically replays an uncertain media publication. Startup marks abandoned
operations INTERRUPTED; the next user request can reuse a verified stored file.
Cross-system exactly-once delivery remains unproven, as the reference cautions.

The deployment continues to run Python and Node on the host, with PostgreSQL/Redis
in Docker. Application imports now use root packages; reinstall with
`python -m pip install -e .` after pulling. Existing database/session paths, ports,
passwords and user data are preserved. This structural update is separately
authorized for commit/push; unresolved origin access and distinct-cold throughput
remain documented in the knowledge base.

## Validation

Tests cover UoW commit/rollback/cancellation, distinct transaction ownership,
atomic state patching, malformed state retention, bad cache reconstruction,
dispatcher isolation/cancellation, off-loop real SQLite session reopen and Redis
pool shutdown. Architecture tests scan the root layers and all independent
providers. The wheel is checked for both layouts and excludes runtime secrets.

The post-refactor Linux request test completed 15,000 cached-input menus/history
records at 1,000 arrivals/s in 15.031 seconds, p95 75.4 ms, with real PostgreSQL,
eight qualities, the dispatcher, default 10,000-record queues and zero lost logs or
escaped exceptions. Origins and Telegram were simulated. This is request-path
evidence, not proof of live origin acceptance or cold 1,000/s capacity.

Final local validation: **814 tests passed** with real dedicated PostgreSQL/Redis,
zero skips and two expected Windows curl-cffi event-loop warnings. Lint,
formatting, English-comment and provider boundary checks passed. The 0.2.0 wheel
built successfully; isolated imports loaded the canonical root layers and provider
package from the wheel. The native migration entry applied all six revisions to
the disposable test database. No Telegram login/messages or production upgrade
were performed during verification.
