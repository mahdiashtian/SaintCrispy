# Group data parity and rollout — October 17, 2026

Compared the supplied local Couplyo checkout at
`e3e073276b489b72b798a3aecc5b3dd3c9361e8b`, including its group, membership,
roster and user models, onboarding handlers and membership/group services.
The reference remains ignored and is not imported by the application.

## Persisted field mapping

The downloader retains its installed bot-scoped asyncpg schema and revisions
1–13. Matching, couple, crush, VIP and profile-selection fields belong to the
reference product and are excluded, as previously requested. The relevant group
and membership data are represented as follows.

| Couplyo field | Downloader SQL field |
| --- | --- |
| `groups.chat_id` | `bot_groups.chat_id`, scoped by `telegram_account_id` |
| `title`, `is_active`, `is_admin` | Same names on `bot_groups` |
| `added_at`, `readded_at`, `removed_at`, `admin_since` | Same names; first add and first known adder survive re-add; removal history survives re-add; demotion clears current admin date |
| `added_by_user_id` | Same name; only a known positive human actor supplies attribution |
| `owner_id`, `admin_ids` | Same names; real creator and distinct other human admins; group roles do not grant installation administration |
| `member_count`, `can_send`, `link` | Same names; native full-group counts and readable existing invite/public link; unknown lookups preserve known values |
| `last_activity_at`, `created_at`, `updated_at` | Same names on `bot_groups` |
| `group_memberships.id`, `chat_id`, `user_id`, `is_active` | Same names; unique bot/group/user identity with real foreign keys |
| `last_seen_at`, `state_changed_at`, `state_event_order` | Same names; real joins update presence, messages update activity, ordering prevents stale presence from reversing a leave |
| `msg_week`, `msg_this_week`, `msg_last_week`, `created_at` | Same names; join, roster lookup and replay do not manufacture messages |
| `group_roster_crawls.chat_id`, status/pass/filter/counters/timestamps | Same names; source `offset` maps to `offset_count` |
| User identity/name, private-start consent/date/source/chat, blocked flag and last activity | `bot_users`; source `is_blocked_bot` maps to `is_blocked` |

Additional downloader audit, account scoping, departure fences, run IDs and lease
fields preserve concurrency/recovery. `group_events` retains accepted and stale
lifecycle evidence. `membership_events` deduplicates message/join/leave evidence.
The group ledger stores activity metadata, not raw message bodies; configured
storage/watch forwarding handles message contents, as in the reference.

## Corrections found by this audit

1. Telethon 1.45's basic-chat participant iterator ignores
   `ChannelParticipantsAdmins` and can return ordinary members. Group snapshots
   now read a basic chat's actual participant constructors, saving only its
   creator/admins as contacts. An installed-SDK regression reproduces the
   unfiltered result before verifying the corrected stored snapshot.
2. Full-group metadata supplies member counts and existing private invite links
   that may be absent from the short chat entity. A failed or forbidden roster
   lookup supplies `None`, preserving known contacts; an explicit empty admin
   list still means a successful lookup found no other human admins.
3. Contact writes deduplicate admins, exclude the resolved owner and validate
   IDs/counts. Partial count updates preserve unresolved owner/admin/link values.
4. First-message discovery now re-reads the newly persisted group standing before
   saving `observed_admin`; unknown remains unknown rather than becoming false.
5. A real join/rejoin updates membership `last_seen_at`, matching Couplyo's
   `add_or_activate`; it does not increment message buckets or grant `/start`
   consent. A late join cannot move last-seen time backwards.
6. An explicit health sweep refreshes contacts/count/link even when the bot's
   admin standing is unchanged. This repairs old misclassified contacts and
   refreshes existing group snapshots without requiring demotion/re-promotion
   or starting a new roster run. Run the panel's group health check after rollout
   to refresh existing groups that have not delivered a new transition.

## Lifecycle and crawl verification

| Input/transition | Stored behavior verified |
| --- | --- |
| Basic-group and supergroup bot add | Known first adder, first-added timestamp, active standing, group identity and metadata |
| Promotion | Admin date, contact snapshot and queued resumable roster run |
| Demotion | Active group retained, admin date cleared, crawl paused, users/history retained |
| Bot removal/leave | Group inactive, removal date retained, crawl cancelled, human records retained |
| Re-add/re-promotion | First adder/add date and old removal preserved; re-add date recorded; new promotion run fences old workers |
| Human join/leave/kick/rejoin | One global human and one membership per group; ordered active flag, accurate presence and counters |
| Message after delayed leave | Message may count as historical activity but cannot reactivate a member against newer leave evidence |
| Roster paging/restart | Existing tests cover basic/full-channel reads, Search/Recent passes, offsets, convergence, partial totals, leases, FloodWait, deletion/demotion and departure fences |
| Replayed updates/messages | Unique ledgers prevent duplicated member rows or counters; a 1,000-way replay is exercised for joins and messages |

Dedicated PostgreSQL 17 and Redis containers back the workstation checks.
Telegram updates, permissions and RPC results are simulated using real Telethon
constructors and the installed iterator; no production deployment or live group
membership changes are claimed. All applicable group/member/crawl columns are
checked against `information_schema`; repeated migrations preserve populated
membership rows. Existing unknown-standing, migration, deferred STATE recovery,
week-bucket and user-retention suites are included in the full run.

Final workstation result: **2,461 tests passed, zero skipped**. Ruff, formatting
and the offline schema-reference check passed. The migration entry point ran
twice against the dedicated test database and reported `versions: 13` both times;
the documented Linux upgrade block passed `bash -n` syntax validation. The ten
existing Windows curl event-loop warnings do not represent test failures.

## Upgrade and database version

These corrections introduce **no migration after revision 13** and need no manual
DDL or database recreation. An older installation still needs the already
existing missing migrations; `python scripts/migrate.py` applies them
transactionally, and reports `migrations_applied` with `versions: 13` when current.
Startup also runs the same idempotent chain. Keep the current Compose project,
database volume, `.env` password/ports and session.

Use the [current server upgrade commands](OPERATIONS.md#current-server-update-and-restart).
