# Full database reset before changing bots

Open `/admin` in private as the configured `MAIN_ADMIN_ID`, then select
**پاکسازی دیتابیس → پاکسازی کامل دیتابیس**. The inline cleanup picker also links
to the same action. The first click shows the scope and current record count;
it does not erase data. Confirm using **بله، حذف تمام داده‌ها و توقف ربات**, or
cancel using the inline cancellation button. Confirmation is owner-bound,
single-use and expires after 20 minutes. Delegated bot admins cannot run it.

## Erasure scope

This operation empties **all 30 application tables across all bot IDs in the
configured PostgreSQL schema**, including global provider account records. It
clears users and delegated roles, groups and memberships, event/message metadata,
history, stored Telegram file references, download menus, settings, quotas,
channels/watches, daily statistics, provider accounts, administrative jobs,
confirmations and outbox effects. Table-owned identities restart. There is no
per-bot filter or 1000-row batch limit for this operation.

Tables come from the application's model registry, so later registered models
join the reset automatically. Definitions, indexes, constraints and the technical
`app_schema_migrations` ledger remain. The operation uses transactional `TRUNCATE`
with an explicit table list and **no `CASCADE`**. An external table referencing
application data aborts the transaction instead of silently deleting that table.
Unrelated application tables or unrelated Redis namespaces are not erased.

Redis keys in the known cache, media, admission and durable-state namespace
families are removed for every bot ID found in SQL, plus the currently running
bot. Both configured CACHE and STATE services are processed, including when they
share one Redis client/database. This is scoped key deletion, not `FLUSHDB` or
`FLUSHALL`. SQL is authoritative for the set of historical bot IDs; unrelated
installations' keys and orphan namespaces with no matching SQL bot ID are retained.

`.env`, local Telegram session files, runtime log files and messages already sent
inside Telegram remain. Removing SQL file references does not delete Telegram
media itself. A full PostgreSQL backup is the recovery option for erased data;
the panel's public-data export does not include every table or operational state.

## Runtime and crash recovery

1. The existing exclusive maintenance gate pauses incoming updates, stops OPS,
   roster and file-expiry workers, and drains current lifecycle/activity streams.
   An active transfer prevents the reset and leaves its confirmation unused.
2. A database-wide advisory lease refuses reset while another current coordinator
   is running. Older bigint coordinator locks are also checked. New coordinators
   acquire the shared lease before initialization, so they cannot enter while
   reset holds it exclusively. Stop other bots using this database first.
3. Configured Redis services are checked before deletion. PostgreSQL locks all
   registered application tables, validates/consumes the confirmation and erases
   their rows in one transaction. A failed SQL transaction rolls back rows,
   identity resets and confirmation consumption.
4. The same transaction writes a consumed, non-expiring internal
   `database_reset_pending` manifest in `admin_requests`. It contains only the
   affected numeric bot IDs, format version and Redis-required flags. It is not
   an actionable confirmation and contains no credentials, URLs or message text.
5. Redis cleanup is idempotent. A pinned connection writes/removes a temporary
   fence and confirms local AOF fsync with
   [`WAITAOF`](https://redis.io/docs/latest/commands/waitaof/), or writes a fresh
   RDB snapshot with [`SAVE`](https://redis.io/docs/latest/commands/save/) when AOF
   is disabled. This also covers retries whose keys were already unlinked before
   a crash. Reset needs `INFO` and the applicable persistence-command permission;
   a missing permission, busy snapshot or unconfirmed fsync leaves recovery pending.
   Snapshot saving blocks that Redis server briefly and can take longer on large
   databases; the existing bounded Redis timeout fails closed and permits a retry.
   The internal manifest is removed only after all required Redis services have
   confirmed deletion persistence. On success every application table
   is empty, including `admin_requests`; no SQL audit row is recreated. Sanitized
   reset preparation/completion events are written to the activity log.
6. From the destructive phase onward, the gate is sealed, file-expiry buffers
   are discarded and stopped workers never restart in that runtime. The final
   message is attempted and Python exits normally. The recovery loop honors this
   intentional shutdown, even if disconnection or cleanup raises a network error.

A Redis failure after the SQL commit leaves the manifest in place and stops the
bot. At the next startup, after schema initialization and **before connecting to
Telegram, loading provider accounts or starting workers**, the manifest is read
across bot IDs and cleanup resumes. Thus switching `BOT_TOKEN` before restarting
does not skip the old bot's pending cleanup. Missing required Redis configuration
or a continued outage prevents startup until the original Redis services are
restored. Partial Redis cleanup can be retried safely. No new schema migration is
needed; the recovery marker uses the existing operational table.

Once destructive execution begins, even an SQL rollback intentionally stops that
runtime; inspect the log and restart after resolving the failure. Errors before
that phase resume ordinary operation. The gate and process shutdown protect
application coordinators; independent external SQL writers must also be stopped.

## Switching the token

After successful confirmation, update `BOT_TOKEN` in `.env`, then run:

```bash
cd ~/SaintCrispy
source .venv/bin/activate
python main.py
```

Keep PostgreSQL and Redis containers running. The numeric `MAIN_ADMIN_ID` remains
configured in `.env` and can open the panel on the new bot. New messages after
restart naturally create new records. If an external supervisor uses
`Restart=always`, suspend that policy before resetting/changing the token; a
normal exit cannot prevent the supervisor from starting the old configuration.

## Verification scope

`tests/test_database_reset.py` uses an isolated schema in dedicated workstation
PostgreSQL/Redis containers. It fills every application table, inserts data for a
previous bot, verifies all application tables become empty and the migration
ledger survives, reruns migrations without restoring data, and checks identity
restart, foreign-key refusal, SQL rollback after truncation, explicit UI scope,
confirmation ownership/cancellation/expiry, delegated-admin rejection, active
transfer refusal, old/new coordinator leases, 100 concurrent confirmations,
sealed queued updates, owner-lock drain protection, Redis failures on both sides
of SQL commit, persistence-confirmation failure and recovery under
a changed bot ID, SQL-only deployments and intentional runtime shutdown.
Telegram operations are simulated; no real user's database is reset by the tests.

October 17 workstation results: the full suite passed 2530 tests (no skips;
10 existing Windows curl-cffi selector-thread warnings) before the final Redis
persistence barrier. After adding that barrier and its error handling, all 121
affected reset/admin/maintenance/runtime/architecture/presentation/language checks
passed. A separate disposable Redis 7.4 instance with AOF and `appendfsync everysec`
also passed real cleanup/fsync followed by `SIGKILL` and restart: the deleted
membership stream stayed absent, the temporary fence was absent and an unrelated
key survived. RDB-mode deletion persistence ran in the SQL/Redis integration tests.
