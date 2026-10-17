# Group, history and availability follow-up — October 17, 2026

## Promotion-only automatic runs

The later operator report contained a completed crawl message sent on startup.
Code inspection found two independent causes: every `claim()` seeded jobs for
existing administrator groups with no crawl row, and idle worker polls reported
historical summaries without any current work. Both automatic startup behaviors
are removed. This does not establish whether the supplied message represented
new network crawling or just reporting existing results on that server.

Each applied transition into admin standing now requests a fresh run even if the
previous run is paused/running/finished/failed. Run identity, counters, filter/pass
and offsets reset atomically with the group transition. Old workers cannot write
or finish the replacement run. Duplicate unchanged admin evidence does not reset
it. Interrupted promotion work still resumes its existing run after a restart;
that is recovery of an existing obligation, not a new startup-triggered crawl.
Migration carries only pending crawl work. Explicit manual requests remain
supported. All recent-group rows show an escaped name and numeric ID in code.

Workstation regressions cover idle startup with/without an existing report anchor,
admin groups missing a job, completed and interrupted runs, repeated promotion
from running/done/paused/failed states, stale-worker fencing, duplicate rights and
numeric-ID rendering. PostgreSQL/Redis checks use dedicated local test data;
Telegram is simulated and the separate production host is not accessed.

Validation: 141 affected group/roster/probe/architecture cases passed with real
local PostgreSQL 16 and Redis, zero skips. The final focused trigger suite passed
15 cases, including four additional migration/report-recovery cases (145 distinct
passing cases across both runs). Ruff and formatting passed for 358 Python files.

## Verified code findings

The supplied Mdi Family notices establish that an add was recorded and admin
standing was subsequently recognized. They do not establish which Telegram
updates arrived. No production host, private session, raw historical update log
or real chat was accessed in this change.

`GroupTracker.refresh(UpdateChannel)` already received a Telegram signal, checked
current permissions, then stored `standing_refreshed`. That change was rendered
with a claim that no event arrived. The current implementation stores a direct
`permissions_changed` transition and normalizes it to promotion/demotion. Genuine
message/health discovery still uses the lazy path, whose copy now says only that
no direct transition was recorded. The probe makes up to three bounded checks
when Telegram temporarily reports MEMBER, within the existing refresh deadline.

The raw route also omitted `UpdateChatParticipant` and `UpdateChatParticipants`.
Both now update the bot's basic-group standing; the modern per-user form also
feeds human membership ingestion. A forbidden/incomplete roster supplies no
negative evidence. QTS and basic-chat version cursors remain independent. Group
identity verification, unknown standing, stale-event ordering and provider
boundaries remain in place. Official protocol references:
[channel participant](https://core.telegram.org/constructor/updateChannelParticipant),
[basic participant](https://core.telegram.org/constructor/updateChatParticipant),
[basic roster](https://core.telegram.org/constructor/updateChatParticipants).

## User-visible behavior

- YouTube is public in private chats and groups, with existing quotas, bans,
  maintenance policy and original-sender quality ownership.
- Private administrator trace cards have ten-row download history pages, querying
  eleven rows for existence of the next page. Ties order by source/content/quality.
  Only existing previous/next directions appear. Page callbacks reauthorize the
  administrator and preserve the displayed profile/group snapshot and formatting.
  Long profiles are sent separately so paging keeps one bounded history message.
- Promotion queues the durable crawl atomically as before. Group copy no longer
  says members will be checked. A Couplyo-style owner DM displays queue state,
  stored/seen/skipped counters and recent groups. It edits a SQL-persisted message
  anchor across restarts, throttles regular updates to 60 seconds and respects
  FloodWait. Paused crawls remain pending; failed work never gets a success header.
  Missing Telegram totals remain unknown. Reporting failure is isolated from
  crawling. Matching/ship logic remains excluded.
- Premium button art uses actual catalog document IDs in `KeyboardButtonStyle.icon`
  on inline and reply keyboards. Existing labels, callback payloads, row order and
  danger colors are retained. Text-only buttons use an available application icon.
  Rejected icons/entities fall back once without removing ordinary HTML entities.

## Workstation validation and retry conditions

Tests simulate Telegram sends, edits, updates, delayed permission visibility,
premium rejection and page clicks. SQL/Redis checks use a disposable dedicated
PostgreSQL 16 database and isolated Redis keys; no production data is involved.
The original dirty checkout was preserved, and implementation uses the current
upstream history in an isolated worktree. No source-media network retry was made;
previous origin failures retain their existing retry conditions.

Validation records: the full workstation run passed 1,884 cases, with two failures
in the same group-audience test. Isolation reproduced different affected audience
parameters. PostgreSQL runs inside WSL and its clock was ahead of the Windows
test process; that fixture fabricated a job creation timestamp before the SQL
group creation timestamp. Production jobs already use SQL timestamps. The test
now uses `clock_timestamp()` as production does; application audience logic was
preserved. The final affected administration/history/premium suite passed 156
cases with real PostgreSQL/Redis, and the group report's recent rows, summary and
durable message anchor passed a separate real SQL check. Provider isolation,
application architecture, English comments, Ruff and formatting checks passed.
After the clock-fixture correction, all 24 source-parity tests passed; the three
affected audience variants also passed their focused rerun. The expanded current
collection contains 1,892 cases; a second complete run was not performed after
these focused final checks.
The ten existing Windows curl selector warnings remain unrelated to this change.

Live Telegram artwork, event delivery on the reported installation, actual member
visibility and production deployment remain unverified. Retry those checks after
deployment with an authorized real chat and safe update-type/error tracing; do
not infer a lost Telegram update from a lazy notice alone.
