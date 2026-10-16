# Couplyo source parity: administration and group membership

October 16, 2026. The actual local `Couplyo/` checkout, commit
`e3e073276b489b72b798a3aecc5b3dd3c9361e8b`, was inspected in addition to its
Persian reference documents. `/Couplyo/` and `/docss/` are ignored, including the
reference project's embedded repository, environments and runtime files.

This supersedes the earlier observed-members-only scope. Full group-member
crawling is now part of this application's runtime. Matching, shipping, crush,
VIP and other couple-product features are excluded. Site providers retain their
independent implementations and existing extraction behavior.

## Source-to-implementation map

| Couplyo source | Downloader implementation and behavior |
| --- | --- |
| `bot/handlers/admin.py`, reply keyboards and button texts | `bot/handlers/admin_reference.py`, `admin_maintenance.py`, `bot/keyboards/admin_reply.py`, `bot/texts/admin_buttons.py`: persistent category menus, back/cancel and SQL FSM prompts |
| Dispatcher and onboarding | `bot/handlers/dispatcher.py`, `groups.py`, `membership.py`: raw self/member events, real group discovery, all human text/media speakers and private `/start` kept distinct |
| Group storage/lifecycle repositories | `services/group.py`, `repositories/postgres/group.py`: first adder, first/re-add/remove/admin dates, stale evidence, migration and transactional audit |
| Membership models and activity storage | `repositories/postgres/membership.py`: one bot/group/user row, exact event deduplication, state ordering, departure fences and activity counters |
| `services/roster_crawl.py`, `repositories/postgres/roster_crawl.py`, `_crawl_one_group` in `bot/jobs.py` | `services/roster_crawl.py`, `repositories/postgres/roster_crawl.py`, `bot/roster_crawl.py`: durable paginated crawl, recovery and convergence |
| Admin group/user directory and jobs | `services/admin.py`, `services/operations.py`, admin/outbox repositories and `bot/operations.py`: filtered pages, members, refresh, health, leave, nudge and confirmed messaging |
| Storage/watch/archive and scheduled notices | `services/admin_channels.py`, `services/management.py`, `bot/management.py`: configured destinations, fresh authorization, opted-in schedules and durable effects |
| Backup, import and cleanup | `services/admin_maintenance.py`, `core/maintenance.py`: bounded public-data backup/import, dry run, confirmation, worker pause, durable queue drain and cache invalidation |

Native asyncpg, frozen dataclasses, the existing additive migration ledger and
SQL conversation store remain the backend. A second SQLAlchemy/Alembic stack is
not introduced into an installed database. The application adopts the source
contracts, folder/layer ownership, menus and event flow while preserving existing
downloader data and six-file provider boundaries.

## Administration

Set `MAIN_ADMIN_ID` to the installation owner's numeric Telegram ID, restart, and
send `/admin` **in private**. This is a Telegram reply-keyboard panel; no HTTP
port is opened. Its seven categories are administrators, messaging, groups,
channels, data, cleanup and downloader settings. Back/cancel and new `/admin` or `/start` commands
replace old prompts correctly. `/admin` stops subsequent handlers for that update
so a newly opened panel cannot immediately reset itself.

Directories expose filters, stable pages, group details, real member records,
subscriber status, recent activity, job progress and crawl progress. Only actual
private `/start` grants subscriber status. The main owner controls role changes,
configuration and data-changing jobs. All sensitive actions recheck SQL roles;
confirmations are owner-bound, expiring and single-use. Long jobs enumerate by
keyset, persist their cursor and use leased outbox effects with stable random IDs.

Channel controls include forced join at configured private-start/download/recheck
points, seven source media classes for group storage/watch, completed-download
archive, promotion text/interval, non-admin reminders, weekly reports and optional
developer credit. Storage observes groups after normal user routing. Private
messages and administrator backup uploads are not archived as group traffic.
Archive effects use a confirmed document reference, preserving the completed file
after its recipient-facing message expires. Incoming protected/expired Telegram
content may still be impossible to forward; failures remain visible job outcomes.

Forced-join checks cache only confirmed membership for a bounded period. Unknown
Telegram results fail open and are not cached as verified membership. Owner and
administrator reminders distinguish active non-admin groups from removed groups;
private recipients must have started the bot and remain contactable. Removing a
group retires its watch configuration in the lifecycle transaction; migration
preserves the old watch only if the new identity has no configured watch.

Promotion, non-admin reminders and weekly reports default to disabled. Reports
use a rolling seven-day observed-message ledger and identify that period in the
caption. They do not invent activity before installation. Every side effect
rechecks current configuration before sending, so disabling a feature also stops
queued effects that are no longer eligible.

## Lifecycle and membership matrix

| Evidence | Stored outcome |
| --- | --- |
| Confirmed bot add/join/request approval | Verified group, active standing, known human inviter, preserved first-add attribution, audit; enqueue roster if administrator |
| Confirmed bot removal/left/banned | Inactive standing, actual removal time, cleared administrator date, suspended crawl, watch retirement and audit |
| Promotion/demotion | Administrator flag/date and separate send permission; enqueue/resume or pause crawl; audit without erasing membership history |
| Ambiguous probe, inaccessible channel, transient RPC | UNKNOWN/error and bounded retry; no fabricated removal or demotion |
| Real human group text or media message | Profile and bot/group/user membership, last activity and one exact counter increment; no private-start flag and no message body in the activity ledger |
| Human join/leave service or raw update | Ordered, deduplicated membership transition; rights-only member updates are not fake joins |
| Unknown departing account | A negative departure fence for an existing group; no invented human profile or group |
| Basic group migration | Retire old identity; preserve confirmed new standing, first adder, quota and compatible watch; crawl new eligible identity |

Presence alone cannot revive a confirmed removed bot group. Owner IDs and
administrator IDs are distinct snapshots, not installation permissions. Negative
senders, channel senders, the bot itself, deleted users and bot accounts do not
become observed human members. Roster-only names do not unblock a user, grant
roles, grant `/start` or invent current message activity.

## Roster algorithm and truthful coverage

```dotenv
ROSTER_CRAWL_ENABLED=1
ROSTER_CRAWL_MAX_CONCURRENT=1
ROSTER_CRAWL_MAX_PASSES=3
ROSTER_CRAWL_MAX_REQUESTS=400
ROSTER_CRAWL_DELAY_SECONDS=1
```

Only verified active administrator groups are eligible. Missing crawl jobs are
seeded in bounded batches. Each claim has a fresh owner token and expiring SQL
lease, checked again before a page RPC and before committing its result. A stale
worker cannot publish a page or finish a new owner's run.

Supergroups use `GetParticipants` with `hash=0`, 200 participants per page and
`Search("")` followed by `Recent` in each pass. Offset advances by returned
participants, including skipped accounts. Durable SQL per-run deduplication
unions both filters and repeated passes. A pass with no new human IDs converges;
otherwise the pass/request budget ends the run explicitly. Basic groups require
the matching complete `ChatParticipants`, and migrated peers use actual saved
access hashes. No zero access hash is invented.

Users, memberships, deduplication and checkpoint commit in one transaction.
Every page obtains a database revision fence before reading Telegram. A newer
departure or member update defeats an old page; SQL-before-ACK replay and unknown
departure tombstones also prevent an old buffered message reviving a departed
member. FloodWait persists its deadline and releases the claim. Promotion wakes
rights pauses without erasing a FloodWait deadline. Restart resumes the same run,
cursor and counters; terminal runs can be explicitly requested again.

Progress distinguishes Telegram's claimed total, unique humans seen, newly
stored memberships, unique skipped accounts, request count, convergence and stop
reason. A converged result is **not** proof of a complete roster: Telegram may
hide/cap participants and counts can include bots/deleted accounts. Absence from
a result never deactivates an existing membership. Crawling cannot recover
updates or hidden members Telegram never provides.

## Data operations and recovery boundaries

The panel exports an allowlisted **public-data JSON backup**, not a complete
PostgreSQL archive. It excludes credentials, access hashes, sessions, operational
payloads and private settings. Import validates the complete bounded payload off
the event loop, dry-runs it, then inserts only missing rows after confirmation;
it does not grant administrators or replace existing rows. CSV remains a separate
bounded directory operation. Use native `pg_dump` and a verified isolated restore
for a full installation backup; see [the deployment runbook](OPERATIONS.md).

Cleanup requires a dry run and single-use confirmation. It protects the root,
delegated administrators and active memberships/groups. Maintenance waits for
startup readiness, pauses incoming handlers, cancels/awaits owned workers, drains
STATE to SQL and refuses to proceed during active transfers. It then invalidates
the current bot's caches and resumes workers even after rollback. Other bots'
cache keys and other Docker applications are untouched.

PostgreSQL remains authoritative. STATE uses AOF/everysec/noeviction and
SQL-before-ACK; a host crash can lose Redis's most recent persistence interval.
Simultaneous unavailable SQL and STATE cannot durably accept events. Telegram and
SQL are separate systems: persisted random IDs reduce repeat publications, but
unlimited exactly-once delivery and unrestricted API throughput are not claimed.

## Verification scope

Workstation tests use isolated PostgreSQL 17 and Redis 7.4, plus Telegram-shaped
RPC simulations. They exercise real transactions, migrations, concurrent claims,
duplicate events, stale owners/pages, SQL outage replay, rollback, two-filter
pagination, skip-only pages, rights changes, FloodWait and ambiguous responses.
Panel routing, settings races, authorization, archive targets, nudge eligibility,
backup validation, cleanup and maintenance concurrency have dedicated regressions.
Live Telegram group inventory, production deployment and origin downloads are
not certified by these tests. Final suite results are recorded in the knowledge
base after verification.

Final local result: **1,596 tests passed, zero skipped/failures** (114.21 seconds).
Ruff lint/format passed for 308 Python files. The offline schema matches revision
11; wheel construction and isolated imports passed. Ten pre-existing Windows
curl-cffi selector warnings remain. No production deployment or live Telegram
send/crawl was performed.
