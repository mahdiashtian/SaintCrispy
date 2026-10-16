# Registration and security follow-up, October 16, 2026

Compared the runtime at `871b438` with the actual local lowercase `couplyo/`
reference checkout at `e3e073276b489b72b798a3aecc5b3dd3c9361e8b`.
Read its dispatcher, onboarding/lazy discovery, user/membership services and
cold-cache, authorization and recovery regression contracts. Reference source,
its Git repository and runtime configuration are not imported or modified.
The lowercase folder now has its own explicit ignore rule on case-sensitive Git.

## Reproduced failures and corrections

| Condition | Before | Corrected behavior |
| --- | --- | --- |
| First private message has no ready `sender` | Observer returned before resolution, so help/plain/media messages could leave no user row. | Resolve the matching update entity, message sender accessor, then bounded entity lookup. Verify numeric ID, human kind and live account before writing. |
| An event carries the inviter's sender entity rather than its member's | An unrelated `sender` prevented looking up the actual member. | Resolve the requested identity from the entity map/client and require an exact ID match. |
| A known bot/deleted/mismatched sender reaches normal download routing | Membership observation rejected it, but a later handler could still create an unknown user from the numeric ID. | The user dispatch boundary rejects these known-invalid entities before a user-creating handler can run; cold genuine-user contexts remain supported. |
| SQL fails while registering a private human | Profile writes had no durable fallback. | A validated, timestamped `HumanProfile` goes into the existing bounded STATE activity stream; SQL replay precedes ACK. No raw message content, consent or roles are stored in this event. |
| SQL fails before group discovery's first write | Preliminary group read prevented reaching lifecycle deferral. | Commit/defer verified group evidence before optional SQL reads, permission probes and metadata. |
| Parent group creation is queued or its read fails | Adapter dropped the speaker/join before the membership recovery service received it. | Re-establish the verified parent, then submit the member; existing SQL-before-ACK parent recovery retains the event until group creation commits. |
| Parent row disappeared while its observation cache is still warm | The warm cache could suppress recreation despite a verified group entity. | Missing-parent recovery bypasses the observation cooldown. Maintenance also clears the private-profile observation cache, so users removed by cleanup can be learned again immediately. |
| Cold basic-group raw update during SQL outage | Optional SQL/entity lookup prevented recognizing a genuine basic-group peer. | Typed basic-group evidence can be recorded without that lookup. A member join can save the basic parent identity with unknown bot standing, without inventing membership or admin rights. |
| Permission probe returns unknown | Five-minute check cooldown was installed before a successful check. | Only a successful persisted standing check earns that cooldown. Normal observation remains bounded/coalesced. |
| Forbidden group entity | Missing `left` flag was interpreted as active membership. | Group identity may be saved, but standing stays unknown. Ordinary observations still cannot revive a known removed group; a definite live/self event is required. |
| An old profile replays after newer profile/block/ban changes | A naive retry of the prior profile SQL would clear block state and overwrite names. | Event-time fences preserve newer profile data and later blocked state. Ban, roles and `/start` state are never granted or cleared by replay. Tombstones still prevent resurrection. |
| Old direct/batched group activity follows a fresh private profile | Older names could overwrite newer ones even while `last_seen_at` remained newer. | Both SQL paths fence metadata by the last-seen timestamp, preserving message accounting and departure order. |
| NUL text in group lifecycle/profile fields | Invalid PostgreSQL text could enter STATE and fail every replay. | Validate UTF-8, NUL, sizes and integer boundaries before durable append; replay quarantines malformed records. |
| Deep JSON within stream size caps, or a recursive object-cache payload | `RecursionError` escaped poison handling and interrupted recovery/cache fallback. | Quarantine/ACK the malformed queue entry, retain only its ID/reason, and continue valid work; repair evictable cache from SQL. |

## Registration boundaries

| Remaining condition | Expected outcome / recovery requirement |
| --- | --- |
| SQL unavailable, durable STATE available and below its cap | Verified profiles/lifecycle/member events are queued. They survive application restart, subject to the deployed STATE server's persistence guarantees. |
| STATE not configured, or an evictable/non-persistent Redis is used as STATE | SQL still handles immediate writes, but outage/restart durability cannot be claimed. Configure the separate `REDIS_STATE_PORT`/`REDIS_STATE_URL` and persistent no-eviction service described in the operations guide. CACHE is not a durable substitute. |
| STATE unavailable/full, SQL available | Immediate SQL paths remain authoritative. Buffered known activity falls back to SQL. |
| Both SQL and STATE unavailable/full | Persistence cannot be promised. The operation raises and dispatcher diagnostics record failure; it is not treated as a successful save. Restore storage and inspect monitoring. An unseen/lost Telegram update cannot be reconstructed from an empty database. |
| Missing update entities and Telegram cannot resolve the sender | No human is invented from a bare number. A later resolvable message, membership event or authorized roster crawl can repair it. |
| Unresolved channel peer whose group/broadcast type is unknown | Do not guess supergroup status from a `-100...` ID. A verified group entity or saved group classification is needed. |
| Bots, deleted accounts, channel/anonymous senders, private peers presented as groups | Rejected from human/group inventories as appropriate. Persistent deleted/bot tombstones remain effective across queue replay, CSV, backup and old menus. |
| Privacy mode/missing admin rights/inaccessible roster | Only delivered updates and Telegram-permitted member pages can be stored. There is no supported bot group-history backfill; a missed member is learned from later activity or an authorized roster crawl. |
| Group activity without private `/start` | Real users may be registered, but remain non-subscribers. Observation/replay does not opt anyone into broadcasts. |

## Security review scope

Rechecked dispatch and session identity, numeric-ID authorization, owner/chat
binding, fresh role revocation, confirmation and replay protection, quotas,
SQL interpolation allowlists, backup/import boundaries, tombstones, worker
authority, subprocess invocation, credential-safe diagnostics and provider
session separation against the reference contracts and existing regressions.
The security-rule scan still reports the same 27 previously triaged findings
documented in [the security review](SECURITY_REVIEW.md); there are no new security
linter findings in this change. Dynamic SQL uses allowlisted identifiers/fragments
and bound values; the MD5 use is Telegram's upload checksum. Reference forced-join
unknown-result behavior is a documented fail-open policy, not installation-admin
authorization. User-supplied text, forwarded identities and group roles cannot
grant installation roles.

No production host, production storage, live Telegram account or provider origin
was used for these checks. No runtime dependencies, provider extraction code or
schema revision changed. This review does not certify the reference application
or every possible project bug. Production credential permissions, SQL/Redis
access control and persistence, dependency/native-tool patching, network egress
isolation against private-network/DNS-rebinding/nested-playlist access and actual
deployment remain outside these workstation tests. See the existing review's
deployment boundaries for those unresolved operational risks.

## Verification

New regressions exercise the actual adapters through SQL failure, both queued
parent and speaker recovery, cold entity paths, old profile/message replay,
blocked/ban/role preservation, both storage systems failing, and malformed queue
entries followed by valid events. Database tests use disposable PostgreSQL 17
and Redis 7.4; Telegram behavior is simulated. Final full suite: **1,882 passed,
zero failures and skips**, in 379.53 seconds. Ten existing Windows curl-cffi
selector warnings remain. The suite includes provider and application architecture,
schema, registration, admin authorization, quota, storage, media decoding and
transfer regression coverage. No code changed after that final run started.

The project virtual environment lacked the optional setuptools build backend, so
the direct `pip wheel --no-build-isolation` probe could not import it. The bundled
runtime's installed setuptools 84.0.0 built the wheel without installing or
upgrading dependencies. All 215 packaged Python sources matched the checkout;
isolated packaged imports and private/reference-file exclusion checks passed.
`pip check`, all 352 Python files' lint/format, the offline schema reference and
the staged whitespace checks passed.
