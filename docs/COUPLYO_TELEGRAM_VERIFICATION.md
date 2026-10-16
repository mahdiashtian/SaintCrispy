# Couplyo administration verification, October 16, 2026

This is the follow-up to [source parity](COUPLYO_PARITY.md), using the actual local
Couplyo checkout at `e3e073276b489b72b798a3aecc5b3dd3c9361e8b`.
It changes administration, lifecycle storage and activity ingestion, not media
provider extraction. Local reference checkouts, credentials and runtime reports
remain ignored. No production server was modified by these workstation checks.

## Copy and panel behavior

`bot/texts/couplyo_admin.py` packages the applicable source messages;
`admin_buttons.py` retains its non-matching action labels. These cover categories,
administrators, group/member cards and filters, health, messaging, channels,
archive, storage/watch, automatic reminders, developer credit and daily history.
Names inserted in HTML are escaped. HTML entities and premium emoji use Telegram
UTF-16 offsets; long messages split without breaking entities or surrogate pairs.
Unsupported premium entities fall back to the same readable text.

`group_lifecycle.py` retains applicable add/remove/promotion/demotion/migration
notification wording, with Jalali timestamps. Removal notices carry the last
known group, membership count, owner/admin snapshot and pre-removal standing.
Unknown values remain unknown. A queued add announcement is suppressed after a
later promotion; removed groups cannot receive stale announcements.

Explicit adaptations remain: matching/ship/crush/VIP/referral rewards and promises
about couple selection are excluded; the directory says registered users;
download controls remain downloader-specific. Full database backup is not
implemented by executing SQL received through Telegram. The source-style backup
button uses the existing bounded public-data JSON flow and honest explanatory
copy. Long operations retain durable job progress and fresh main-owner checks.
Consequently, domain-inapplicable text and every operational response are not
claimed to be byte-identical to Couplyo.

## Schema and stored data

Migration 12 is additive. Revisions 1–11 and existing download/history data remain
intact. The database now has 29 domain tables plus its migration ledger.

| Source contract | Current storage |
| --- | --- |
| Group identity, title/username, first adder, owner/admin IDs, counts, link, first/re-add/remove/admin dates and last activity | Existing `bot_groups`, ordered `group_events` and group peer records |
| Human profile, private-start attribution, actual speaking activity and roles | `bot_users`, including `last_active_at`, immutable first `start_source`/`start_chat_id` |
| Membership presence, ordering barriers, recent week counters | `group_memberships`, exact message/join/leave ledger and durable departures |
| Shared mandatory/archive/storage destination flags | Normalized `channels`; no competing destination copy in settings JSON |
| Per-group watch destination and source media types | Normalized `group_watches` |
| Daily acquisition, group churn and eligible active speakers | Immutable Tehran-day `daily_stats`, initialized prospectively |
| Verified bot/deleted-account cleanup | `user_account_tombstones`, a downloader safety addition |

Physical `bot_users`/`bot_groups` names, composite bot scoping, nullable unknown
standing, native asyncpg and the existing migration ledger are retained. This is
not a byte-identical copy of the single-bot SQLAlchemy schema. Couple-specific
columns/tables are intentionally excluded.

Couplyo does not store raw message bodies in its SQL membership model. This bot
likewise stores identities, timestamps, activity counters and deduplication keys.
Configured storage/watch channels receive eligible messages by Telegram
forwarding, including bot/anonymous/channel traffic; those senders never become
fake human members. All seven storage classes default off until enabled. A
permanently refused route is suppressed briefly and reconsidered after expiry or
configuration change; FloodWait and temporary failures never poison that route.

Daily active-user eligibility is captured when the message arrives. Changing the
bot's group rights before a buffered event flushes cannot rewrite eligibility.
Legacy unknown eligibility remains unknown. Sealed history is immutable; totals
represent the actual sealing snapshot, not a reconstructed historical census.
No pre-installation daily history is fabricated.

Manual `/dailyreport` and `/weeklyreport` require fresh group administrator
authorization, with a main-owner override. Normal administrators share one
request per group/report type per rolling 24 hours. Windows use the current
Tehran day or Saturday-based week. Scheduled reports use the completed day/week;
fixed bounds survive retries. Speakers are distinct ledger users, including
members who subsequently left, rather than a count of current members.

## Bugs reproduced and repaired

- Independent channel flags now share one normalized row; an unrelated setting
  edit does not rewrite a thousand watch records.
- Missing first-start metadata, actual group speaking time, historical daily
  statistics, nullable destination titles and migration notices are handled.
- Source group-message exclusions now examine active membership; administrator
  and non-administrator broadcast audiences are separate.
- HTML/premium rendering, source confirmations, paged history and legacy public
  backups were checked against the installed schema.
- A definitive invalid-account purge cannot be undone by an old STATE message,
  stale profile or crawl page, CSV import, admission or public backup. Purge
  locks and rechecks the user first; root and delegated administrators are
  protected. Role writes reject tombstoned identities rather than granting a
  contradictory role. Unknown entities/RPC failures are not deletion evidence.
- Activity batches lock groups, sorted users, then memberships. This avoids the
  user-delete cascade and membership-update lock inversion. Transactions commit
  before STATE acknowledgement; duplicates cannot increment counters twice.

## Real Telegram scope

A bounded read-only bot login succeeded on this workstation using the configured
credentials and a fresh `MemorySession`. It did not receive updates, share/open
the production SQLite session, send messages or prompt for a human login.

No authorized human session, controlled test group or main-owner ID was available.
Live add/remove/promote/demote, participant paging and interactive panel actions
were therefore **not performed or certified**. Their automated tests use actual
Telethon types and simulated RPC outcomes with real PostgreSQL/Redis transactions.
The successful bot login does not substitute for those lifecycle tests.

The first diagnostic used zero request retries and stopped during Telegram's DC
migration. A bounded two-retry probe authenticated successfully. This failure did
not establish that the token was invalid; do not repeatedly regenerate credentials
in response. `tools/diagnose_group_telegram.py` reports sanitized results. An
optional controlled `--chat-id` can check a saved/resolvable peer; for a cold
session and public group, supply `--public-group @your_test_group` as well. The
resolved peer must match the requested ID. This probe remains read-only and
never claims to have tested lifecycle transitions.

## Telethon decisions checked against primary documentation

| Documentation | Applied decision |
| --- | --- |
| [Update dispatch](https://docs.telethon.dev/en/stable/concepts/updates.html) | `StopPropagation` prevents a handled admin command from falling through and resetting its FSM. |
| [Sessions](https://docs.telethon.dev/en/stable/concepts/sessions.html) | A diagnostic uses isolated memory state, not the running receiver's SQLite session. |
| [RPC errors](https://docs.telethon.dev/en/stable/concepts/errors.html) | Persist FloodWait deadlines and release owned jobs; errors are not proof of removal. |
| [Client API](https://docs.telethon.dev/en/stable/modules/client.html) | Bound concurrent work, use call-level deadlines, do not use unbounded sequential update queues or obsolete aggressive participant crawling. |
| [ChatAction](https://docs.telethon.dev/en/stable/modules/events.html#telethon.events.chataction.ChatAction) | Service events alone do not expose every large-group departure; raw participant evidence complements them. |
| [GetParticipants](https://core.telegram.org/method/channels.getParticipants) | Bots can call participant paging subject to access/administrator restrictions. Convergence does not prove Telegram exposed every member. |

## Workstation verification

The complete suite passed **1,630 tests, zero skips**, against isolated PostgreSQL
17 and Redis 7.4. Ten existing Windows curl-cffi selector warnings were emitted.
Architecture/provider boundaries, nonblocking rules, source copy, Unicode entity
splitting, migrations, lifecycle ordering, roster fencing, cleanup, permissions,
backup compatibility and failure recovery are included. Ruff and the generated
schema comparison passed.

`tools/load_test_group_state.py` uses dedicated `TEST_DATABASE_URL` and
`TEST_REDIS_URL`, creates and removes its own SQL schema and Redis keys, and makes
no Telegram requests. It generates traffic across 50 known groups and 1,000 known
human members, starts with cold process membership cache, and runs concurrent
STATE draining, event-loop monitoring and administrator statistics reads.

Before batching, 10,000 offered events at 1,000/s required 31.448 seconds to finish
SQL draining. All events survived, but ingestion alone did not prove sustained
capacity. The bounded batch path eliminated that backlog. After the final
lock-order and purge fixes, the 60-second run measured:

| Measurement | Result |
| --- | --- |
| Offered / accepted / committed / counted | 60,000 / 60,000 / 60,000 / 60,000 |
| Offered rate | 1,000 events/second |
| Failed / capacity rejected / lost | 0 / 0 / 0 |
| Ingestion / fully drained elapsed | 60.011 / 60.324 seconds |
| Ingestion p95 / p99 | 1.367 / 1.819 milliseconds |
| Event-loop lag p99 | 11.943 milliseconds |
| Concurrent administrator statistics p95 | 5.233 milliseconds |
| Peak active producer tasks | 31, bounded capacity 1,000 |
| Replay accounting | Unchanged after concurrent replay |

The earlier 10-second batched run drained in 10.433 seconds versus the 31.448
seconds single-event baseline, approximately a threefold reduction in that
workload's end-to-end drain time. The final 60-second run is the sustained test;
raw workstation reports remain in ignored `.runtime/`. Wheel construction also
succeeded; packaging excludes the local reference checkout and runtime state.

This benchmark measures accepted group activity and durable SQL accounting on
this workstation. It does not establish 1,000 Telegram API messages, origin
downloads, unknown-group discoveries or media uploads per second, nor promise
unlimited capacity on an 8-GB production server.

For deployment, stop the receiver, make and verify a PostgreSQL backup, update the
editable install and run `python scripts/migrate.py` before `python main.py`.
Keep existing `.env`, sessions and named volumes. See [the upgrade commands](OPERATIONS.md).
Set `MAIN_ADMIN_ID` to the human owner's numeric ID and send `/admin` privately.
