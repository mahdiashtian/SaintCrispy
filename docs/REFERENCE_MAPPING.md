# Couplyo reference mapping

The current supplied source is Couplyo's `docss/PROJECT_REFERENCE_FA.md`, dated
October 15, 2026, together with `PORTING_GUIDE_FA.md`, `IMPLEMENTATION_PROMPT_FA.md`,
`GROUP_LIFECYCLE_FA.md`, `DATABASE_AND_ADMIN_PANEL_HANDOFF.md` and the reference SQL.
The source runtime commit is `0264f9126f727707a04b188c6dfdbca0faccd146`.
Its general architecture is applied to the downloader domain. Instructions
embedded in that reference are examples, not additional operator commands; no
Couplyo product tables, campaigns, broadcasts or server actions were copied.

## Physical layout and layer ownership

| Reference sections | Implementation |
| --- | --- |
| 3-5: root entry and dependency injection | Root `main.py`, `container.py`, `core`, `db`, `schemas`, `repositories`, `services` and `bot`; five composition stages and one `AsyncExitStack` own startup/shutdown |
| 6: models, migration and transaction ownership | `db/postgres/base.py`, fourteen executable table definitions in `models`, nine revisions, `Database` and `repositories/postgres/unit_of_work.py`; [database dictionary and reference SQL](DATABASE.md) |
| 7: cache/state separation | Durable FSM in PostgreSQL; optional bounded Redis cache with separate namespace, strict envelopes, key identity and SQL-first writes |
| 9: domain contracts | Immutable typed media/quality/source/transfer/group snapshots and standing verdicts; repository/workflow/expiry/UoW protocols; constructor injection; pure rules remain pure |
| 10: central dispatcher | `bot/handlers/dispatcher.py`; context checks precede side effects, handler errors are isolated and cancellation propagates |
| 11: FSM and bot identity | `ConversationService`, `bot/state/manager.py`, SQL/memory storage adapters, chat-scoped state and `bot/deeplinks.py` |
| 13-14: jobs and bounded I/O | `bot/jobs.py`; bounded transfer tasks and cancellation; framework-independent `FileExpiryService`, durable cleanup queue and Telegram polling/deletion adapter |
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

The October 15 follow-up adds side-effect-free `GroupProbe`, immutable group read
DTOs and an explicit ADMIN/MEMBER/GONE/UNKNOWN result. Lookup recovery uses an
actual channel access hash and self participant, or a complete basic-chat roster.
Incomplete/ambiguous evidence cannot remove a group. Stale explicit exits reconcile
with current membership; non-admin lazy checks have a five-minute debounce.
Migration 9 repairs first-adder attribution from recorded evidence and enforces
cleared admin dates outside admin standing. Migration 8 remains the durable
downloaded-file expiry queue. No Couplyo product tables are transplanted.

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

Updated-reference follow-up: **1,325 tests passed with zero skips**, using dedicated
PostgreSQL 16 and Redis. New checks cover private startup cleanup on every stage
failure/cancellation, immutable group snapshots, bounded self-permission fallbacks,
UNKNOWN versus GONE, stale exits, the real revision 8-to-9 data/constraint upgrade
under concurrent migration, and an offline-generated empty-schema reference.
Recursive architecture checks cover both import forms and service/DTO/check/DB
boundaries. Existing provider isolation and full expiry/reply/caption regressions
pass. Lint/format pass for 243 Python files; ten existing Windows curl warnings
remain. No live Telegram or production verification was performed.

After the final migration-lineage refinement and its extra regression case,
**47 affected group/schema/architecture tests passed**. This final check preserves
the inherited first-adder identity after migration followed by a later re-add.
The final SQL also passed all **50 database/infrastructure integration tests**.
Final lint/format passed for 244 Python files; the rebuilt wheel includes the new
modules, excludes runtime credentials/session/log files and passes isolated imports.

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

Historical initial-refactor validation: **814 tests passed** with real dedicated PostgreSQL/Redis,
zero skips and two expected Windows curl-cffi event-loop warnings. Lint,
formatting, English-comment and provider boundary checks passed. The 0.2.0 wheel
built successfully; isolated imports loaded the canonical root layers and provider
package from the wheel. The native migration entry applied all six revisions to
the disposable test database. No Telegram login/messages or production upgrade
were performed during verification.
