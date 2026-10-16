# Group downloads and lifecycle

The implementation follows the group identity, transaction, update-ordering and
unknown-standing rules in the October 15 Couplyo `docss/PROJECT_REFERENCE_FA.md`.
The actual local Couplyo source is also mapped in [the parity guide](COUPLYO_PARITY.md).
Full member-roster crawling is now explicitly required and implemented; couple
selection remains excluded.
See [the Telegram verification follow-up](COUPLYO_TELEGRAM_VERIFICATION.md) for
source lifecycle copy, normalized storage, report windows and measured activity
throughput. Message contents are forwarded only to configured destinations;
SQL records membership/activity metadata, as in the source.

## User flow and Telegram configuration

Add the bot to a basic group or supergroup. A supported link receives the same
provider-specific quality menu as a private request, replying to the original
link message. Only its sender can use that menu; callbacks also verify the chat.
Progress, cancellation and the final file stay in that group and reply to the
original link. No private conversation or `/start` is required. `/start` in a
group explains the group quota and does not mark the sender as a private subscriber.
Callbacks with a cold Telegram entity cache resolve their supergroup kind from
the bot-scoped SQL snapshot rather than being mistaken for a broadcast channel.

Telegram must deliver ordinary group messages to the bot. Either make it an
administrator or use BotFather's `/setprivacy` to disable privacy mode for this
bot. Keep permission to send text and the requested media. With privacy enabled
and no admin status, an unaddressed link may never reach this application; no
downloader can process an update Telegram does not deliver. See the
[official message visibility rules](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).
Broadcast channels and anonymous/channel senders are excluded from user download
routing; they are not invented as human users in SQL. Their messages can still
provide real group metadata when the peer is a verified group.

## Rolling quota

```dotenv
GROUP_LINKS_PER_MINUTE=5
USER_REQUEST_INTERVAL_SECONDS=60
```

The group limit accepts integers 1-1000 and defaults to five. It applies to the
whole group across users and providers in a **rolling 60-second window**, not a
calendar-minute counter. The existing per-user interval still applies across
private chats and groups. A quality click does not consume another group link;
the existing download-stage user interval still applies to quality selections.

PostgreSQL locks the bot/group row and checks both quotas in one private Unit of
Work. Accepted links register the user and own the per-user/chat `INSPECTING`
state in that same transaction. A group denial does not consume user quota; a
user denial does not consume group quota. A transaction failure rolls everything
back. Accepted inspections count even if the origin later fails. Excess handler
capacity is rejected before quota admission. A group-limit warning is coalesced
across users for five seconds and replies to a rejected link to avoid a flood of
warnings. Every new grant remains SQL-authoritative, including after restart or
Redis eviction/outage.

## Storage and lifecycle

Migration **7**, `group_lifecycle_and_admission`, adds three tables. Existing
media, users, menus, histories, conversations and migrations are preserved.

| Table | Responsibility |
| --- | --- |
| `bot_groups` | One row per `(telegram_account_id, chat_id)`; negative BIGINT chat identity; group/supergroup kind; title/username; nullable active/admin/send standing; human adder when known; added/readded/removed/admin/seen timestamps; lifecycle ordering cursors and migration destination |
| `group_events` | Durable audit with a unique bot/chat/event key, actor when known, occurrence/receipt times, previous/resulting standing and `applied`; stale evidence is retained without replacing a newer snapshot |
| `group_request_limits` | Recent accepted timestamps for the shared rolling quota; no link contents or credentials |

`GroupChange` is framework-independent. `bot/checks/group.py` interprets Telegram
types; `bot/handlers/groups.py` receives updates; `services/group.py` applies
policy and owns transactions; `repositories/postgres/group.py` owns SQL. The
container constructs these dependencies and the UoW binds repositories to one
fresh connection. Provider implementations remain independent and unchanged.

Repository reads now return immutable `GroupSnapshot` values rather than asyncpg
records. `bot/checks/probe.py` independently returns `GroupStanding` with explicit
ADMIN/MEMBER/GONE/UNKNOWN and a separate send capability. It performs no SQL/cache
writes. A failed input-user lookup may use a real channel access hash with the
self participant, or a complete matching basic-chat roster; no zero hash is
invented. Empty permission fields, incomplete rosters and inaccessible channels
remain unknown. Lookup fallback shares the original timeout/concurrency budget.

Migration **9** repairs first known adder attribution from applied add/re-add
audit events, clears invalid admin dates and adds the admin-date standing check.
Re-add preserves the first known adder, first-added and removed timestamps.
Migration always clears the retired identity's admin date. Unknown/missing
historical attribution cannot be recovered without actual recorded evidence.

The raw lifecycle route is independent of the positive-human-sender guard:

- `UpdateChatParticipantAdd/Delete/Admin` covers the bot's basic-group membership
  and promotions/demotions using the chat version.
- `UpdateChannelParticipant` covers the bot's supergroup member/admin/creator/
  restricted/left/banned states using the update's timestamp and `qts`. Other
  members' updates do not change the bot's standing. A muted member is still an
  active member, with sending disallowed.
- Service messages cover bot add/remove and basic-group-to-supergroup migration.
  This uses the underlying service updates directly; `ChatAction` alone omits
  many promotion/demotion cases in the installed Telethon implementation.
- `UpdateChannel` can trigger a bounded current-self permission probe. Unknown
  entity, access or network errors are logged as unknown, without marking the
  group removed or demoting the bot. A definite response can refresh standing.

An update hash identifies replay of the same raw event; raw payloads and access
hashes are not logged or copied into the audit. Separate Telegram update forms
for one physical action can each have an audit row, but share one group snapshot.
Older versions/timestamps and weaker same-second service evidence cannot replace
newer authoritative standing. An ordinary message observation does not advance
an existing lifecycle watermark. A fresh re-add clears the old removal's sending
restriction; an unknown role remains unknown until evidence supplies it.

Groups already containing the bot are discovered lazily from received group
activity. A 2048-entry, 60-second observation cache, per-active-chat keyed locks, eight probe
slots and finite deadlines bound metadata work. Definite group entities prevent
phantom groups from users/broadcasts. Fresh activity after a known removal can
request a definite self-membership probe before reactivation; a message alone
does not reverse a recorded removal. Startup registers handlers before catch-up;
eligible administrator groups receive a separate resumable roster crawl.
Only updates Telegram actually delivers can be recorded; this is not a historical
inventory of groups the bot left before this feature was installed.

Known non-admin groups also receive a lazy probe at most once per five minutes
within a bounded 2,048-entry cache. A definite current membership can reject an
explicit replayed exit; an unknown probe does not discard a genuine exit event.
Probe timestamps retain microseconds so a response started before a newer raw
event cannot acquire a newer standing merely by completing late.

Migration retains both group identities and the audit, marks the old identity
inactive with `migrated_to`, and carries recent group quota into the new identity
once. Existing new-group standing is not erased by an older migration event.
Delayed old-group updates cannot reactivate the retired identity. Old callbacks
remain bound to their original chat and are not redirected into another chat.

## Administration and membership extension (revision 10)

[Administration design](ADMINISTRATION.md) adds separate observed human memberships, replay-safe message counters, owner/admin snapshots, typed peer recovery, SQL-backed health/leave jobs, group quota overrides and transactional lifecycle notifications. An actual departed state changes `removed_at`; replayed removals do not reset the date. A provisional add settles through up to three 1.5-second checks, while stale demotion is checked against fresh admin evidence. UNKNOWN remains non-destructive.

Known-active activity uses the persistent STATE stream when configured; new membership and explicit join/leave commit immediately. Received-message presence is not a full-roster crawl. Every human group message can update last activity regardless of whether it includes a supported link. Only real non-bot, non-deleted user entities count; no message text is stored by this ledger.

Revision **11** additionally crawls eligible group members using the source's
Search/Recent passes and durable checkpoints. Raw departures create an atomic
negative fence even when no human profile exists. SQL failures defer typed events
to STATE, and replay commits before ACK. Roster-only members receive no fabricated
message activity or private-start status. See [algorithm, settings and coverage
limits](COUPLYO_PARITY.md#roster-algorithm-and-truthful-coverage).

## Logs and deployment

Committed lifecycle events emit `group_lifecycle` in `logs/activity.log`, with
`kind` such as `added`, `readded`, `removed`, `promoted`, `demoted`,
`permissions_changed`, `standing_refreshed` or `discovered`. Fields include chat,
known actor, audit event ID, resulting standing and `applied`. A replay is not
logged as another committed event. `group_migrated` records both identities;
`group_link_rejected` includes a safe reason and retry delay. Unknown probes and
dispatch failures are contained and logged. SQL retains the audit even if the
bounded file logger is unavailable; monitor the existing log dropped/write-error
metrics. These events never include cookies, signed URLs or access hashes.

On the bot host, stop the existing Python process, then:

```bash
git pull --ff-only
source .venv/bin/activate
python -m pip install -e .
python tools/setup_services.py
python scripts/migrate.py
python main.py
```

Python remains on the host; PostgreSQL, evictable Redis CACHE and persistent Redis STATE run in Docker. Setup keeps existing ports/password and configures a distinct STATE endpoint. Do not delete their volumes. Startup also applies the idempotent
migration chain. The new quota defaults to five if the existing `.env` omits it.
No provider credentials or new runtime dependencies are required.

## Verification scope

Workstation tests use real isolated PostgreSQL 17 and Redis 7.4. They cover
concurrent group admission, restart, rolling expiry, user/group quota interaction,
rollback, cross-bot isolation, replay, stale ordering, removal/re-add, promotion/
demotion, lazy discovery, unknown permissions and migration quota preservation.
Telegram-shaped tests exercise every provider's group reply/menu/quality flow,
owner/chat rejection, original-message file reply and private-subscriber behavior.
Telegram calls and origin extraction in these group-flow tests are simulated;
no live group message, production deployment or origin throughput claim follows.

Historical October 15 workstation validation passed **998 tests with zero skips**
against those dedicated services; ten existing Windows curl selector warnings
were emitted. Ruff lint/format checks passed for 235 Python files. Provider
boundaries, layer directions, English-comment and nonblocking checks passed.
