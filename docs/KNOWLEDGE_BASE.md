# Downloader knowledge base

Last updated: October 17, 2026. This is the shared record of verified findings,
failed approaches and decisions for all providers. Read it before investigating a
provider. Update it when new evidence changes a conclusion; preserve dated results
rather than rewriting historical failures as successes.

## Full database reset and replay prevention (October 17)

A bot-token change can leave old bot IDs, file references, settings, jobs and
durable streams in the installation. Filtered per-account cleanup is insufficient
for an explicit full reset. The root-only panel action now previews the global
scope and requires a private owner-bound, expiring confirmation. It truncates all
30 registered application tables with identity restart, preserves schema and the
migration ledger, and clears matching Redis namespaces for SQL-known bot IDs.

Do not use `TRUNCATE ... CASCADE`, `FLUSHDB`, or clear SQL while workers/handlers
can still write. Include file-expiry buffers/workers in quiescence, seal the
maintenance gate for destructive execution, and honor intentional shutdown in
the coordinator recovery loop. A database-wide shared/exclusive advisory lease
and legacy-coordinator checks refuse reset while other coordinators are active.

SQL and Redis cannot commit atomically. The SQL transaction keeps a consumed
non-expiring `database_reset_pending` manifest in `admin_requests` until Redis
cleanup succeeds. Startup completes it across bot IDs before Telegram or workers
start. Missing required Redis configuration blocks startup. Never erase the last
manifest or resume writers after partial cleanup. A failure during destructive
execution stops the runtime even if PostgreSQL rolled back; errors before that
phase resume normal work. An acknowledged Redis deletion can still be absent
from an `appendfsync everysec` AOF at a power loss. Pin one Redis connection,
write/remove a temporary fence even on an already-empty retry, and confirm local
`WAITAOF` fsync (or a fresh RDB `SAVE` without AOF) before deleting the SQL marker.
Denied/busy persistence commands and unconfirmed fsync keep recovery pending.
Full-reset callbacks use the maintenance gate's serialization instead of holding
the admin owner lock while waiting for other updates to drain.
Root/cancellation/expiry and SQL transaction failures
were exercised on dedicated workstation PostgreSQL/Redis, including every model
table populated, old-bot records, foreign-key refusal, 100 concurrent confirms,
post-truncate rollback and restart under a changed bot ID. Telegram is simulated.
See [reset scope and operational instructions](DATABASE_RESET.md).

The AOF persistence path was verified on a disposable Redis 7.4 instance using
real `WAITAOF`, then `SIGKILL` and restart. Deleted streams stayed absent and an
unrelated key survived. Docker reallocated the random published port on restart;
the first workstation reconnect to the old port timed out. Inspect the new
published port and internal `PING` before retrying; that failure was a port
mapping change, not lost AOF data. Final affected checks passed 121 tests after
the persistence change; the earlier full regression run passed 2530 tests.

## Administrator rate exemption and independent controls (October 17)

Download admission previously treated all user IDs identically, so even the
configured root and SQL-authorized delegated bot admins were rate-limited. The
production repository now receives `MAIN_ADMIN_ID` and reads delegated roles
fresh from bot-scoped SQL. Exempt admissions retain transactional registration
and group permissions but spend no user/group rate timestamps. A Telegram group
owner/admin snapshot is not an installation role. Revocation must never depend
on a cached user profile.

Four independent switches control user/group minute quotas and optional gaps.
Gap seconds are 1–3600, initially disabled; changing a number does not toggle it.
Do not implement disabled quotas by appending unbounded timestamps, or discard
all timestamps outside 60 seconds before checking a longer gap. Keep the newest
1000 ordinary admissions and read the last accepted time before pruning the
rolling view. Report the longer wait when both quota and gap apply to a subject.
Existing revision-13 tables support this without a new migration or Redis reset.

Dedicated workstation PostgreSQL tests verified fresh grant/revoke/ban handling,
cross-instance/cross-chat races, no admin group-slot consumption, transactional
rollback, owner-bound quality clicks, live panel persistence and 1005 accepted
ordinary requests with quotas disabled and bounded storage. Telegram is simulated
in these tests; there is no production/live throughput claim. See the
[policy, settings and verification details](REQUEST_LIMITS.md).

## Group/member field and ingestion audit (October 17)

Compared the actual Couplyo group, membership and roster models against the
downloader's populated PostgreSQL schema. All applicable group/member/roster
fields already exist through revision 13; no new migration or physical-table
rename is needed. See the [field/event map and rollout](GROUP_DATA_PARITY_20261017.md).

The installed Telethon 1.45 basic-chat participant iterator does not apply
`ChannelParticipantsAdmins`. Do not assume its returned users are all admins.
Read native basic-chat participant constructors and exclude ordinary members,
bots and deleted accounts. Keep creator and other-admin IDs separate. Unknown
roster results preserve prior contacts; an explicit empty list remains distinct.
Use full-group metadata for readable existing invite links and accurate counts.
Explicit health checks refresh these fields even without a new role transition.

First-message discovery must reload persisted admin standing before tagging
activity eligibility; unknown is not false. Real joins update membership presence
without increasing message counters or private-start consent. Workstation tests
use real PostgreSQL/Redis, installed Telethon iteration, raw-update constructors,
replay races and migration reruns; they do not prove production Telegram delivery.

## Couplyo native admin controls (October 17)

The local source at `e3e0732` uses reply category/form menus but native inline
cleanup, directory filters, watch switches and confirmation controls. Converting
all inline buttons into reply text was the reason these sections looked and
behaved differently. Preserve each keyboard type; inline actions edit their
message and carry the private owner's ID. Recheck current roles on every callback
and retain transactional single-use confirmation tokens. Cleanup now uses
independent choice masks, live SQL counts and a separate confirmation step.
See the [current panel interaction matrix](ADMINISTRATION.md#couplyo-keyboard-behavior).
Verification uses workstation PostgreSQL/Redis and real Telethon TL objects;
Telegram RPC effects are simulated, with no production deployment claimed.

The full-suite check also exposed an outdated SoundCloud handler-test assertion
that still expected the `www` alias after the documented canonical-host change.
Update that fixture to `soundcloud.com`; do not revert alias normalization or
repeat live extraction to repair this test-only mismatch. No provider runtime
code or previous origin findings changed in this UI batch.

## Alternate provider links and SoundCloud mobile fix (October 17)

Deeper same-day review: added Spotify native track URIs/legacy track widgets,
Pinterest's official iframe and `in`/`au` locale hosts, TikTok's `www.tiktokv.com`
input alias, XVideos' old SWF query-link mapping, and YouTube's Google API embed
host. Provider-local normalization preserves the exact content ID and avoids
requesting obsolete player shells. New regressions exercise inspection/selection
and the Spotify group handler, not only URL parsing. Pinterest/TikTok alias inputs
completed fresh full transfers with integrity checks (223,586 / 2,004,627 bytes).
Spotify's canonical embed returned an error-shaped HTTP-200 page without the
required track entity; that live lookup failed and must not be called a successful
download. Its legacy shell was HTTP 403. Retry that origin failure only after
usable metadata or changed connection evidence. See the
[follow-up coverage and exact scopes](URL_VARIANTS_20261017.md#deeper-follow-up-app-and-player-aliases).

The newly reported `m.soundcloud.com` track returned HTTP 200 with mobile assets
but no desktop hydration. Its desktop counterpart contained usable metadata.
Normalize SoundCloud web aliases before HTML requests, including after redirects;
use one bounded homepage bootstrap lookup when a supplied page lacks a public ID.
The exact public track `1595188473` now passes anonymous inspection/resolution and
a complete 294-second AAC/HLS stream through the application's integrity path
(3,625,826 bytes, 28.83 seconds on this workstation). This does not change the
earlier DRM-only track findings.

All eight providers have an alternate-link routing/identity test matrix. YouTube
legacy/wrapped links, Instagram post embeds, TikTok share/older embed paths and
SoundCloud widget/numeric-API inputs gained provider-local support. Existing
Spotify/Pinterest/XVideos/XNXX aliases needed no runtime changes. Nested targets
and bootstrap assets retain strict origin validation. See the
[dated coverage, primary references and measurement scope](URL_VARIANTS_20261017.md).
Other providers were checked with unit regressions, not fresh live transfers.
When probing from a managed worktree with the original checkout's editable
virtualenv, set `PYTHONPATH` to the worktree root and `src` so the probe tests the
changed code. Revisit old origin failures only under the retry conditions in the
dated evidence.

## Public YouTube, download history and group follow-up (October 17)

User-retention audit: reread the actual local Couplyo source at `e3e0732`, including
`GroupService.set_admin/set_active`, onboarding demotion/deactivation and
`MembershipService.leave`. The downloader already preserves global users,
memberships and download history on bot demotion/removal; a departing human only
deactivates that group's membership. Re-promotion resets temporary per-run seen
tracking without removing human data. Real PostgreSQL regressions now snapshot
these tables across lifecycle changes. No runtime change was needed. See
[retention evidence](GROUP_FOLLOWUP_20261017.md#user-retention-parity-with-couplyo).

Later October 17 correction: the operator reported a completed crawl message on
startup and requested promotion-only automatic runs plus numeric IDs in report
rows. Idle polling had both seeded missing jobs for existing admins and replayed
historical summary data. Both triggers are removed. Reporting activates only when
actual queued work is claimed; a fresh promotion resets the run/counters and
fences previous workers, including after demotion. Interrupted work can resume its
existing run, and explicit manual admin requests remain supported. See the
[dated follow-up](GROUP_FOLLOWUP_20261017.md#promotion-only-automatic-runs).

The operator removed the temporary owner-only YouTube policy. Public inspection,
quality selection and cached delivery now follow the same admission/ownership
rules as other providers. Start/sites/help include YouTube even without a configured
main administrator. No provider extraction, session or cookie behavior changed.
This supersedes the historical October 15 availability restriction below, not its
origin-access observations.

Download history has ten-row private owner-scoped inline pages, with missing
directions omitted. Edits preserve the displayed profile/groups/hint and their
UTF-16 entities. Current SQL authorization is checked on every page. Group admin
announcements omit the member-check promise; the crawl still runs. Its private
owner report is edited in place with a SQL-persisted message anchor.

Code inspection reproduced a misleading lifecycle explanation: `UpdateChannel`
triggered a live permission check but was labeled as though Telegram sent no
event. It now produces a direct permission transition, with bounded settling for
delayed rights. Modern basic-group participant/roster update forms are also
handled. The original reported production event cannot be reconstructed without
its safe raw-update trace. See [dated findings and validation](GROUP_FOLLOWUP_20261017.md).

Premium button icons now use Telegram's native keyboard style icon field and
the loaded catalog; callback bytes, reply labels and colors remain intact.
Unsupported custom icons/entities retry once with the readable Unicode fallback.

## Registration outage and source security follow-up (October 16)

Reinspected the actual lowercase `couplyo/` reference at `e3e0732` and the
application's cold sender, lifecycle, profile and membership recovery boundaries.
Ten initial regressions reproduced dropped cold private senders/member identities,
SQL-before-queue losses and lifecycle NUL acceptance. Corrected those boundaries,
added durable timestamped private-profile replay, preserved queued parent/member
handoff, and fixed old activity overwriting newer profile metadata. Unknown
permission probes no longer start a success cooldown; forbidden group metadata
does not prove bot membership. Deep JSON no longer poisons stream recovery or
object-cache fallback. Lowercase reference code is explicitly ignored.

The first focused integration run passed 52 cases and exposed one existing test
that asserted probe-before-observation ordering. Its assertions now verify the
intentional persist-before-enrichment order and definite membership evidence.
The next focused run passed 94 cases; additional final regressions are included
in the subsequent full run. The first broad run passed 1,875 tests and failed one
older test that expected unknown permission results to receive the success
cooldown. That test now explicitly distinguishes retried unknown results from
debounced confirmed results. Later focused runs passed 76 SQL/adapter cases and
107 dispatch/probe cases. Missing-parent recovery now bypasses a warm observation
cache, maintenance clears private profile observations, and known bot/deleted or
mismatched senders cannot enter user-creating download handlers.
No provider origin was retried, no provider extraction
code was changed, and no schema/dependency update was needed. Initial upstream
fetch failed on DNS; a later fetch succeeded and its main was integrated before
editing. Read the [failure matrix, scope and remaining storage/Telegram/security
limits](REGISTRATION_SECURITY_AUDIT.md) before repeating investigations.

Final workstation run on the completed implementation: **1,882 passed, zero
failures/skips**, in 379.53 seconds, using dedicated disposable PostgreSQL 17 and
Redis 7.4. Ten existing Windows curl-cffi selector warnings remain. All 352 Python
files passed lint/format, the changed runtime files passed security lint, and the
full suite includes provider/application architecture and schema contracts.
The schema reference, dependency consistency, staged whitespace and wheel checks
passed; all 215 packaged Python sources match the checkout and isolated imports
work without including private/reference/runtime files. This is not production
verification or a guarantee against every security flaw. Private-profile outage
recovery requires configured persistent STATE; an evictable CACHE is insufficient.

## Reply-only administration and configurable quotas (October 16)

The registered private admin runtime now converts every directory, pagination,
settings and confirmation action to an ordinary reply keyboard, retaining the
action payload only in the owner's SQL conversation. Text cannot inject an action
or target; stale destructive confirmations have distinct operation references.
Old inline admin panels require reopening `/admin`. Fresh numeric-ID/SQL roles,
owner-bound one-use tokens and per-owner UI serialization remain authoritative.

Download settings previously exposed pause/notifications only. They now edit the
user requests/minute and global group default (1–1000), alongside per-group
overrides. Revision 13 adds bounded rolling user windows, adopting the old
inspection/download timestamps without deleting old history or rewriting earlier
migrations. Every selected format consumes a download slot, including old menus
and cached sends; a full download window also rejects fresh links. Menu discovery
does not block its first format. Counts survive restart and span private/groups;
grants and group accounting are atomic SQL operations with fresh settings.
Rejected clicks answer with wait time; links receive an original-message reply
with remaining seconds and a Tehran retry clock. Public help reads live limits.

Security review restricted remote FFmpeg protocols and rejected header control
injection. Real FFmpeg proved that a remote-media source cannot read a local file.
The first full run exposed eleven old media-fixture failures because these tests
used local paths as remote inputs; the same fixtures now run over local HTTP,
retaining decoding/completeness assertions. The new network restriction was not
relaxed. No provider extraction implementation or credential session changed.

Final workstation coverage: **1,841 full-suite tests passed** with zero skips,
then **three additional SQL/UI regressions passed**; together all **1,844 current
cases** are covered. Dedicated disposable PostgreSQL 17/Redis 7.4 were used;
Telegram was simulated and no production host or live origin was contacted.
Ten existing Windows curl-cffi selector warnings remain. All 349 Python files
passed lint/format; architecture, offline schema comparison and wheel packaging
passed. The wheel contains the new modules and excludes tests/private runtime.
The 27 security-linter findings were manually triaged. OSV rejected its one batch
probe; the separate PyPI version-metadata check returned no listed vulnerabilities
for eleven installed runtime packages. Do not describe that as an exhaustive
native/transitive dependency audit. See the [security review, exact scope and
remaining deployment boundaries](SECURITY_REVIEW.md).

## Quality button layout (October 16)

The shared Telegram keyboard now caps all quality rows at two buttons and mirrors
each pair's left/right positions. Within each audio/video/image group, menus with
more than two choices feature the first choice alone, then use pairs and a final
singleton when needed. Exactly two choices remain one mirrored pair. Original
callback indexes, media grouping and Instagram's 50-choice pagination are retained.
No provider extraction code changed or live origin/Telegram request was made.
Workstation validation passed 261 affected handler, keyboard, album-pagination,
ownership, presentation and architecture tests with zero skips; lint/format and
diff checks passed. One existing Windows curl-cffi selector warning remains.
A separate in-memory check verified 72 menu shapes across all eight callback
prefixes, including empty, single, paired and paginated-size selections.

## Administration and group reference alignment (October 16)

Help/start follow-up: the actual Couplyo `bot/handlers/about.py` and help-topic
keyboard were inspected. The downloader now uses the same overview/topic/back
interaction, with downloader-specific content. `/help`, the private help button
and the plain guide label share one renderer, including quotas and developer
credit. `/start` and its membership-policy handler are private-only; direct calls
also reject groups before onboarding/FSM/settings work. Old group home callbacks
cannot reopen the private main menu. Lifecycle and promotion copy now points to
`/help`. Guide commands do not consume outstanding administrator forms. No new
provider extraction, database migration or production Telegram test was needed.
Verification: the final full workstation suite passed 1,683 tests with zero
skips against isolated PostgreSQL 17/Redis 7.4; ten existing Windows curl-cffi
selector warnings remained. Ruff, offline schema comparison, wheel build and
isolated imports of the new help modules passed. Keep the README English even
when documenting Persian UI labels; user-facing strings belong in bot texts.

Quality-button follow-up: sender/chat ownership was already enforced by both
the in-memory menu and scoped SQL lookup. A cached ownership rejection used to
fall through to SQL, causing unnecessary I/O and a misleading storage error during
an outage. `menu_not_owned` now remains a terminal callback alert, even for an
expired cached menu. Ownership is checked before download policy/quotas/jobs/FSM
writes; the owner's menu is not consumed by rejected clicks. Regression coverage
includes all seven providers, group administrators/main owners as other senders,
hot/restarted persistence, cross-chat access and 100 concurrent rejected clicks.
The affected workflow/group/access/transfer suites and architecture checks passed
205 tests; PostgreSQL integration used an isolated PostgreSQL 17 container.

The [later Telegram/copy/load follow-up](COUPLYO_TELEGRAM_VERIFICATION.md) extends
the source port through migration 12: normalized channels/watch flags, immutable
daily history, canonical applicable panel/lifecycle copy and fixed report windows.
The workstation authenticated a fresh read-only Telegram bot session. There was no
authorized human session or controlled group, so live lifecycle/panel testing
remains unverified. Zero-retry authentication failed on a DC migration; two bounded
retries succeeded without changing credentials. Retry only on a bounded new probe
or newly available controlled-group evidence; do not run destructive live actions
against unrelated groups.

New failures reproduced: single-event SQL draining lagged sustained 1,000/s input;
an old queued message/crawl/profile/CSV/backup could recreate a purged invalid
account; membership/user delete locks could invert; current group rights could
misclassify old activity; report retries could drift to a later window; protected
forwarding could keep retrying one refused destination. These now have batch,
fence, fixed-window and bounded-route regressions. Full suite: 1,630 passed with
zero skips on real isolated PostgreSQL/Redis; no provider extraction changes.
Do not generalize activity-ingest load results to media throughput or Telegram
API limits. The final 60-second run offered/accepted/committed/counted 60,000 at
1,000/s, drained in 60.324 seconds, and lost/rejected/failed zero events. Ingest
p99 was 1.819 ms, loop-lag p99 11.943 ms and concurrent admin-statistics p95
5.233 ms; replay left counts unchanged. The single-event 10-second baseline
required 31.448 seconds to drain; the bounded batch equivalent took 10.433.
The evidence guide records source-specific exclusions and actual
physical-schema differences; do not describe it as a byte-identical ORM clone.

The later **actual-source** review supersedes the observed-members-only scope.
Source commit `e3e073276b489b72b798a3aecc5b3dd3c9361e8b` is mapped in
[source parity and dated evidence](COUPLYO_PARITY.md). Revision 11 implements the
category/FSM panel, source group-member crawler, channel controls and guarded
maintenance. Provider extraction and network acceptance were not changed.

Newly reproduced and fixed: `/admin` could be consumed twice and reset the just
opened FSM; raw unknown departures needed a durable negative fence; an old crawl
page or buffered message could otherwise revive a departure; expired claim owners
must not commit; two maintenance confirmations must register as waiting before
the first operation drains active handlers. SQL outage tests now defer typed
join/message/leave events to STATE and replay once. Crawl coverage distinguishes
human IDs, skipped accounts, Telegram total and convergence; it never deactivates
members merely absent from a capped result. Retry requires a new claim, due-time
expiry, changed rights or a new explicit run, rather than an unchanged error loop.

Source adaptations are explicit: real peer hashes only; fresh owner tokens;
logical allowlisted public-data JSON rather than executing uploaded PostgreSQL
archives; no couple-product tables. The panel backup is not a full database
backup. Live Telegram and production deployment remain unverified. Final local
suite results are appended after verification.

Final source-parity workstation verification: **1,596 passed, zero skipped or
failed**, in 114.21 seconds against dedicated PostgreSQL 17 and Redis 7.4.
Ten existing Windows curl-cffi selector warnings remain; the new unawaited mock
warnings were corrected. Ruff lint and format passed for 308 Python files.
The generated schema matches revision 11; the wheel rebuilt successfully and its
isolated module imports excluded Couplyo, private docs and runtime files. The
rewriting agent implemented the panel and the separate supervising agent reviewed
it read-only; their findings were addressed before final verification.

Additional recovery evidence: a membership event remains pending while a second
consumer owns its group's uncommitted creation; it is applied after that group
commits. A later verified human can replace an earlier skipped roster identity
without false convergence. Roster reactivation records a new evidence boundary
so an older explicit leave cannot undo it. Failed worker stop/restart paths
attempt all affected workers and always release the maintenance gate.

Revision 10 adds downloader administration, observed human membership, safe typed
CACHE, separate persistent STATE and SQL operational jobs/outbox. See
[dated architecture and verification evidence](ADMINISTRATION.md). This change
updates infrastructure only; provider extraction, credentials and upstream
acceptance have not been retested or changed.

Verified locally with dedicated PostgreSQL 17/Redis 7.4: duplicate activity is
idempotent; commit-before-ACK replay preserves counts; stale leave/join evidence
cannot overwrite newer explicit state; malformed/WRONGTYPE cache objects repair;
SQL writes survive cache-write failure; wrong-owner/expired confirmations and
revoked permissions cannot run jobs. Telegram RPCs are simulated. The complete workstation suite passed 1,472 tests with zero skips/failures. The new Compose STATE service passed health/configuration and graceful-restart persistence checks. Production
upgrade, live membership and backup restoration remain unverified.

Two pre-release defects were reproduced and corrected: the old port-reservation
test supplied only two free candidates after a third service was added, and a
positional Telethon forward call placed the random-ID list in the target field.
The forward regression now serializes the real installed TL request and checks
source, target, message IDs and persistent random ID. A missing session peer uses
bot-scoped typed SQL recovery; it does not prove membership. Retry failed origin
methods only under the conditions already recorded in the provider sections.

## How to use this record

- Record the date, provider, media identity, code/package version, connection scope,
  extraction method, safe error code, and what was actually verified.
- Separate extraction, CDN sampling, full consumption to EOF, completeness checks,
  Telegram registration and delivery. Success at one stage does not prove the next.
- Do not repeat a rejected method on the same account/egress/version in a retry
  loop. Retry after a meaningful change: a new upstream fix, an authorized refreshed
  session, a verified connection change, cooldown expiry or confirmed origin recovery.
- Keep credentials, cookie paths/values, proxy addresses, signed URLs, Telegram
  access hashes, raw extractor stderr and private runtime reports out of this file.
- A workstation failure does not prove production failure. A GitHub runner success
  does not prove production success. No SSH connection to the separate production
  host has been available in these investigations.

Detailed dated evidence is in [provider verification](PROVIDER_VERIFICATION.md),
[YouTube/Instagram access](YOUTUBE_INSTAGRAM_ACCESS.md), and
[operations](OPERATIONS.md). Sanitized Actions reports provide independent evidence;
private workstation reports remain in ignored `.runtime/`.

## Spotify addition — October 16, 2026

The later exact-track fix now has a successful workstation full-stream result for
`1gihuPhrLraKYrJMAEONyc` (Feel So Close - Radio Edit). General video results included
209-second Radio Edits that correctly fail the 206-second Spotify match guard.
The Music songs catalog supplied official audio `Nh_bdvA8cVM`, Calvin Harris and
208 seconds. The independent Spotify client now reads that typed catalog before
the existing video queries, preserving strict title/artist/version/duration checks,
bounded responses/candidates, the original deadline and stop-on-429 cooldown.
Flat Music extraction was tested and rejected because it drops artist/duration;
do not infer those fields from the search query or loosen checks to fill them.
After correcting only the diagnostic's missing FFmpeg PATH, the exact track passed
complete EOF/audio validation: **3,326,802 bytes**, **207.912 seconds**, **17.2988
seconds total**. This supersedes the earlier unverified workstation recovery claim
below; Telegram delivery and production deployment remain untested. **345 affected
tests passed with zero skips**, including provider boundaries; changed-file lint
and formatting passed. See [dated catalog and full-stream evidence](SPOTIFY.md#october-16-verified-radio-edit-catalog-recovery).

Later operator logs identify the actual disappearing-start-status chain:
`spotify_no_matching_audio` during `resolve`, zero transferred/uploaded bytes,
then terminal-edit `ReplyMarkupInvalidError` and `transfer_status_removed`.
This instance did not publish a file and was unrelated to file expiry. Empty
inline markup is now replaced by explicit `buttons=None` for terminal/Stop edits.
An uneditable transfer status receives one bounded terminal-text reply before
cleanup; an unconfirmed replacement keeps the old status without retrying sends.
Real installed-Telethon request construction reproduces the previous invalid
markup and verifies the correction for success/error/cancellation.
Spotify now permits one artist/title-only query after a genuine no-match result;
both queries share the original deadline and strict artist/version/duration rules.
The supplied Radio Edit is identified on official Spotify/artist pages, but its
new workstation metadata/flat-search attempts did not succeed. No live download
recovery is claimed. See [dated log and access evidence](SPOTIFY.md#october-16-disappearing-start-status-and-radio-edit).
Validation: 411 affected tests passed with zero skips; all 323 Python files passed
Ruff lint/format, including provider/application architecture and language checks.

Delivery follow-up: workstation regressions reproduced an expired Telegram file
reference whose original message was gone, and a cleanup repository returning a
fresh file early because its clock was ahead. Cached sends now rebuild once after
a definite reference rejection and an unavailable/still-rejected refresh. Exact
origin invalidation preserves a concurrently repaired anchor; the existing content
lock coalesces the new transfer. Ambiguous send timeouts and permission rejections
never trigger this fallback. The expiry worker checks both stored deadlines against
the application clock before deleting, retaining the existing 30-second policy.
See [dated delivery evidence and limits](SPOTIFY.md#october-16-delivery-follow-up).
These are reproduced code paths, not a diagnosis from the user's production logs.
Final validation covered all 1,785 current tests across the ordinary full run
(1,625 passed, 159 service-dependent skips) and the real isolated SQL/Redis suite
(212 passed, including the new database repair case). Ruff lint/format passed for
323 Python files; the ten existing Windows curl-cffi warnings remained. No live
Telegram send, production connection or new extraction retry was performed.

Read [Spotify research, failed approaches and verified full streams](SPOTIFY.md)
before changing this provider. It implements public track metadata and a matched
YouTube MP3, not original Spotify audio. Never promote `audioPreview` to a full
download, invent lossless/320-kbps choices, or claim the repository applications
were executed when only their code/network approaches were examined.

Spotify embed metadata and flat yt-dlp search worked here. Old SpotifyDown API
connection, Spotdown `song-details` 404, Fabdl `invalid origin` 403 (including the
changed-origin experiment), Spowload redirect budget and Song.link 401 failed.
SpotMate's current browser script requires Turnstile. Do not retry those old
snippets unchanged or use embedded third-party credentials. Revisit after a
documented endpoint/client change, authorized access or new connection evidence.
Native M4A URL extraction succeeded, but its CDN connection failed; libcurl DoH
then timed out. A fresh URL is not proof of a consumable file.

The independent public conversion path fully consumed three matched MP3s through
the real generic audio-integrity pipeline, with metadata in 0.4503–0.6290 seconds,
resolution in 6.1462–8.0950 seconds and complete streams in 6.2161–10.5492 seconds.
See the dated table for exact IDs, bytes, durations and hashes. The first loose
matching rule accepted a four-second-shorter recording; it was replaced by a
two-second duration bound and ten-result search. Old 210-second success is not
final-head evidence for the 214-second track. Failed first-track resolution was
followed only after changed matching and probe-budget code. Production and
Telegram delivery were not exercised by these workstation tests.
Final suite: 1,770 passed with zero failures/skips against real isolated SQL/Redis;
1,000 identical successful and failed calls coalesced, cancelled/oversized workers
were reaped, sender/chat isolation survived SQL restoration, and 200 concurrent
file writes produced one file row with 200 separate delivery receipts. Lint and
format passed for 322 Python files; the isolated wheel includes all six Spotify
modules. No new dependency, database migration or Docker service was introduced.

## Earlier cross-provider verification (October 15)

Earlier October 15 checks passed fresh anonymous project inspection/resolution and
complete transfer validation for both reported identities. Instagram used its
public resolver/original CDN; YouTube used its independent public converter.
The latest YouTube follow-up separates first-response and repeat-catalog speed
from upstream conversion time; a fresh cold workstation metadata lookup failed
in that later connection state. See the dated access sections for successes,
failures, byte counts and the untested production/Telegram boundary.

## TikTok provider (October 15)

The new independent six-file TikTok provider uses an anonymous TikWM-compatible
JSON request as its measured fast path, with bounded native Universal/SIGI page
extraction as fallback. Research compared the primary yt-dlp, TikTok-Api,
Tikorgzo/TikWM and tt-dlp implementations; the bot adds no browser/runtime dependency.
See [method comparison, implementation and evidence](TIKTOK.md).

The same payload supplies video/photo/music choices. Fresh sources are bounded
to 45 seconds/128 posts, selected without another metadata lookup, and shared by
concurrent identical inspections. Signed endpoints are excluded from SQL snapshots;
callbacks refresh the same post/quality. Photo identity survives carousel reordering
and URL signature/host changes. User acknowledgment, quality edit, original-link
reply, caption and file-only 30-second expiry use existing generic infrastructure.

Two anonymous public posts returned three choices in 1.5604 and 1.629 seconds.
The first selection took 0.000073 seconds; 100 warm inspections took 0.005919
seconds with no additional metadata requests. The first post's two MP4 versions
(2,004,627 and 2,953,029 bytes) and soundtrack were fully consumed through the
application's actual stream/integrity checks. These are workstation observations,
not Telegram delivery, server acceptance or large-scale throughput evidence.

Native access timed out with both plain and browser-TLS transports. Another
public-reference post and two old short tokens were rejected by the resolver;
native fallback was unavailable on this egress. Their causes remain unproven.
Short-token canonical recovery is bounded and covered with simulated HTTP;
do not report those rejected live cases as successful or retry them unchanged.

Final full verification passed **1,409 tests with zero skips**, with dedicated real
PostgreSQL 16/Redis. Existing presentation/delivery/caption/file-expiry tests include
TikTok, and real FFmpeg with a Telegram mock verifies complete prepared video
delivery when resolver dimensions/codec are undeclared. Lint/format passed for
251 Python files. The isolated wheel contains the complete provider and excludes
runtime credentials/sessions/logs. No dependency or schema migration was added;
no real Telegram message or production server verification was performed.

## October 15 updated architecture reference

The operator supplied the corrected `docss` Couplyo architecture/porting/database
reference dated October 15 (source runtime `0264f9126f727707a04b188c6dfdbca0faccd146`).
Its architectural contracts are applied to the downloader domain; product-specific
couple/crush/profile/roster/broadcast tables are not downloader features. The
existing native asyncpg, frozen DTOs and additive migration history are retained.
See the updated [reference mapping](REFERENCE_MAPPING.md) and the new
[database dictionary](DATABASE.md) for the full implementation and ownership map.

The follow-up separates five container composition stages, adds immutable group
read snapshots and a bounded side-effect-free self-permission probe, reconciles
explicit stale exits with current membership, and debounces lazy non-admin checks.
Partial responses, inaccessible channels and transient errors remain UNKNOWN.
Input-user lookup recovery uses valid channel hashes/self participant or a complete
matching basic-chat roster, with the original deadline and concurrency bound.

Migration 9 preserves first known add attribution from applied audit records,
repairs invalid admin dates and enforces their standing constraint. The old
migration chain is unchanged; all demotion/removal/migration paths clear the date.
The downloaded-file expiry service now owns deadlines/retry/persistence through
framework-independent ports; the Telegram adapter owns serialization/RPC/FloodWait
translation and polling. The existing 30-second file-only behavior is preserved.

Full workstation validation passed **1,325 tests with zero skips**, using dedicated
real PostgreSQL 16 and Redis services. Tests include concurrent upgrade from
revision 8, constraint enforcement, audit retention, private UoW rollback,
startup failure/cancellation cleanup, probe fallbacks/UNKNOWN and every existing
provider/quality/reply/expiry regression. Lint and formatting pass for 243 Python
files. The ten existing Windows curl selector warnings remain. The offline schema
reference also builds a matching disposable PostgreSQL schema. Telegram calls are
simulated; no production host, real chat or origin acceptance was tested here.

Final migration review also covers inherited attribution through retired group
identities, with cycle-safe lineage traversal. After that SQL refinement and its
regression test, **47 affected group/schema/architecture tests passed** against
the same dedicated PostgreSQL instance. The initial unisolated wheel command
could not import local setuptools; the normal isolated pip build succeeded.
All **50 database/infrastructure integration tests** then passed with the final
migration SQL. Final lint/format passed for 244 Python files. The wheel contains
the new service/DTO/port/probe modules, excludes environment/session/log files,
and imports successfully in an isolated Python process.

## Downloaded file expiry (October 15)

The operator clarified that automatic deletion applies only to downloaded files
sent by the bot, from every platform; ordinary messages and progress/quality UI
must remain. Final file captions now include an instruction to save/forward the
file before its 30-second deletion deadline. Generic Telegram delivery schedules
only confirmed user-facing publication IDs, covering fresh media and cached
resends in private chats and groups. The original submitted link is never queued.

Migration 8 adds the bot-scoped `file_message_expiry` queue and an origin-reference
index. One managed worker, owned by the container after Telegram authentication,
recovers overdue deadlines on restart and batches deletion with bounded concurrency
and explicit FloodWait/backoff. Failed cleanup writes cannot cause another file
publication. Pending writes survive request cancellation in the running worker.
Storage-channel copies are preserved and their captions omit the expiry notice.

Confirmed deletions remove only cache anchors matching that exact account/peer/
message, including Redis invalidation. Two-day completion tombstones reject late
producer writes that would restore an already-expired origin. User history and
metadata are preserved. Fresh download remains possible when the user-facing
message was the only file anchor. See [operations](OPERATIONS.md#downloaded-file-expiry)
for deadlines, queue recovery and the external permission/API timing boundary.

Workstation tests simulate Telegram sends/deletions and verify the 30-second
boundary, private/basic-group/supergroup peers, per-file captions, cached resends,
original-link preservation, storage exclusion, cancellation, offline persistence,
FloodWait, lost permissions, batching and managed shutdown. Separate tests use
dedicated real PostgreSQL/Redis to verify account isolation, additive migration,
retry timestamps, restart recovery, Redis failure, late-cache-write rejection and
tombstone pruning. No real chat, production host or source media was contacted.

The complete workstation run passed **1,267 tests with zero skips** using dedicated
PostgreSQL 16 and Redis services. After adding cached-resend/permission/cancellation
cases and clarifying the internal-storage caption, **92 affected tests** passed.
Lint and formatting passed for 235 Python files. The ten existing Windows curl
selector warnings remained. No dependency or environment setting was added;
the additive expiry queue is migration 8.

## Inspection acknowledgment and original-link replies (October 15)

The operator requested a visible format-discovery message for every download,
edited into the quality menu, with the delivered file replying to the submitted
link. All six provider handlers now acknowledge every accepted link before
application cache/provider inspection, including immediate cache hits. Generic
Telegram send/edit/cleanup lives in `bot/inspection.py`; provider extraction,
sessions, URL rules and callback prefixes remain independently owned.

The same message becomes the existing site-specific quality text/buttons or a
recoverable inspection error. Large Instagram albums edit the first page and
keep remaining choices in additional pages. A deleted acknowledgment permits one
replacement response; cancellation/unhandled storage failure attempts bounded
cleanup. A write-permission rejection prevents origin work. Timeout/connection
failure during acknowledgment does not cancel otherwise useful discovery.
Acknowledgments retain premium-emoji adaptation. There are no detached UI tasks
or automatic retries of ambiguous sends. See [operations](OPERATIONS.md#quality-menu-timing)
for acknowledgment/cleanup deadlines and timing fields.

Menu persistence still records the user's original message ID. Workstation tests
exercise fresh/cached inspection, slow discovery, per-request ownership,
private/group replies, recoverable errors, deletion, cancellation, storage failure,
album pagination, access rejection and premium entities. Fresh file publication
and cached resend both keep the original-link reply ID and identical captions
for every provider. Origin responses and Telegram RPCs are simulated for this
presentation change; no production host or real chat was contacted.

Final workstation validation passed **1,214 tests**, with **31 integration tests
skipped** because dedicated local test services were not configured. Lint and
format checks passed for 230 Python files. The local editable installation was
updated to the existing `0.2.0` project layout and its already-pinned dependencies
before the final run; no dependency declaration or database migration changed.

## Transfer status finalization (October 15)

The reported Instagram statuses remained at 65% upload or at an estimated 0%
after publication. Workstation regression tests reproduced three defects in the
shared reporter: a terminal edit was attempted only once, an existing edit
FloodWait skipped it entirely, and exit cancelled an in-flight progress edit
before sending the terminal text. A cancelled client await does not establish
that Telegram never received the earlier edit. These are code-level findings;
the production request/error trace for the two reported messages was unavailable.

The reporter now stops scheduling updates, waits for its bounded in-flight edit,
then confirms the terminal text and clears the stop keyboard. It retries only
idempotent status edits, up to three attempts within a 15-second finalization
budget, respecting Telegram's edit cooldown. Each RPC has a three-second deadline.
An edit cooldown beyond the budget or a persistent/permanent failure falls back
to deleting the obsolete status with a separate bounded RPC; it never repeats
file publication. Both edit and delete can still be rejected during a Telegram
outage or loss of permissions. Failures are logged rather than treated as proof
that the file was not delivered. The in-flight drain and optional deletion add
at most three seconds each outside the finalization budget.

The quality handler explicitly marks successful delivery complete and finalizes
its status before conversation-state persistence, including error/cancellation
paths. A slow conversation-state write must not leave a delivered file displaying
an active upload. Provider extraction, quality selection and file-cache identities
are unchanged. See [operations](OPERATIONS.md#transfer-status-messages) for events.

Workstation tests cover the shared DownloadService/quality-handler/premium-message
path for all six sites, private/group delivery, progressive/HLS/external/cache
methods, the two reported progress shapes, short/long cooldowns, transient edits,
timeouts, permanent errors, cleanup failure, cancellation and slow state writes.
They assert one publication/resend and one delivery-history write despite status
retries. A separate simulated batch starts 1,000 terminal editors independently
and verifies their reporter tasks finish. Origin extraction and Telegram RPCs are
simulated in these tests; this is not a live Telegram, CDN or production replay.
The complete workstation suite passed 1,164 tests with zero skips using isolated
real PostgreSQL 17 and Redis 7.4; Ruff checks and formatting passed. The ten
existing curl-cffi Windows event-loop warnings remain unrelated to status edits.
Do not retry edits inside a returned FloodWait or resend a file to repair its
status. Retry only after cooldown or changed connection/permission evidence.

## Provider identity and isolation

October 15 presentation update: site-specific menu/file copy now lives in
`bot/texts/downloads.py`. Instagram's source caption is deliberately omitted from
bot captions to avoid repeating source advertising. New files and cached resends
use the same profile and loaded premium catalog; file publication previously did
not apply the premium adapter. Mocked workstation tests verify all six sites,
UTF-16 bounds/entities and permission fallback. See [presentation evidence](PREMIUM_EMOJI.md#site-specific-file-captions).
No fresh origin or live Telegram rendering claim follows from these UI tests.

| Provider | Stable identity | Do not use as the durable file identity |
| --- | --- | --- |
| YouTube | Validated 11-character video ID, with the selected codec/resolution/frame-rate quality key | `pp`, tracking parameters, temporary Googlevideo URLs or playlist position |
| Instagram | Requested shortcode/media ID; each carousel item has its own numeric ID; story identity includes its story ID | `dlrf`, signed CDN URL, album position without a stable item ID |
| SoundCloud | Origin numeric track ID with a supported full-stream quality | Mutable uploader/track slug, search-result title or a preview URL |
| Spotify | Validated 22-character track ID plus `matched_mp3_128`, scoped to the bot | Share/tracking query, search result position, preview URL, converter signature or YouTube ID as the Spotify identity |
| Pinterest | Pin ID plus the provider's stable child/rendition identity | Tracking query, signed CDN URL or a short preview mistaken for the full pin video |
| XVideos | Origin canonical video identity, normalized by that provider across public/embed aliases | Title slug, old URL spelling or CDN URL |
| XNXX | Origin canonical video identity and stable quality key | Title slug or CDN URL |

The file key also includes the Telegram account. Provider sessions, account cookies,
headers, HLS discovery and format parsing must remain inside their owning provider's
six-file folder. Shared models and generic transfer/database/Telegram infrastructure
are allowed. Similar provider implementations must not be merged into a shared
concrete extractor in violation of `AGENTS.md`.

## Group lifecycle and admission (October 15)

The supplied Couplyo reference was used for negative group identity, private UoW,
unknown standing, replay and event ordering. Basic-group/supergroup downloads now
retain the original link reply, owner-only quality choice and same-chat delivery
for all six providers. No provider implementation was shared or changed.

Migration 7 adds bot-scoped snapshots, a durable replay-safe audit and a rolling
60-second group quota. `GROUP_LINKS_PER_MINUTE` defaults to five; the per-user
interval still applies globally. Real isolated workstation PostgreSQL tests sent
1,000 concurrent group admissions: five grants, 995 group denials, five users and
five owned conversations. Denials do not consume the other quota; rollback and
restart preserve the rules. Ordinary observations must not advance an existing
lifecycle watermark or resurrect a removed group. Telegram access/timeout errors
are unknown evidence, not a removal/demotion. Group migration carries quota once.

The installed Telethon `ChatAction` build logic omits many promotions/demotions.
Use raw self-participant updates plus service messages, not that event alone.
Tests must construct patched `MessageService` with keyword `action=`: its unified
constructor otherwise interprets the fourth positional argument as message text,
leaving action empty. An initial test failed for this fixture mistake; corrected
Telegram-shaped fixtures exercised the unchanged raw-service interpretation.

Group flow/origin/Telegram tests use simulated network boundaries; no live group
or production claim follows. The database tests use isolated real PostgreSQL 17
and Redis 7.4. Bots need admin standing or disabled Telegram privacy mode to
receive unaddressed group links. Startup cannot invent an inventory of unseen
groups; received activity provides lazy discovery. Full design, deployment and
verification scope are in [the group guide](GROUPS.md).
Final current-tree validation passed **998 tests, zero skips**, with dedicated
PostgreSQL/Redis; lint/format and architecture checks passed for 235 Python files.

## YouTube

October 15 temporary availability change: YouTube is disabled for ordinary users
and hidden from their start/sites menus. Only the positive numeric Telegram user
ID configured as `MAIN_ADMIN_ID` may use it, including in groups. Empty means
disabled for everyone; usernames, group IDs and group admin roles do not grant
access. Reject links before quota/history/state/provider work. Check old callback
prefixes and the loaded media site, then enforce policy again in the application
service before metadata/history/cache or transfer work. Existing downloaded files
and durable catalogs remain stored; they do not grant authorization. No migration
is necessary. Restart the coordinator after changing configuration. See the
[dated policy evidence](YOUTUBE_INSTAGRAM_ACCESS.md#october-15-temporary-main-administrator-access).

October 15 cold-menu follow-up: the critical path included four independent CDN
stream openings before any quality choice. Exact playable watch responses already
contain complete clear H264/audio profiles even when SABR exposes no direct URLs.
The converter's actual filename was checked separately: requesting 360/720/1080
for `jNQXAC9IVRw` reported 240p every time, matching prior file validation. The
provider now cross-checks that reported height with one exact origin size and
complete duration/audio metadata. Only a match defers the file sample to selection;
unknown/inconsistent metadata retains ordinary probing. A candidate never exits
`resolve` until its bytes pass the existing dimension/codec/audio checks. Expired
candidates refresh only the selected conversion. Failed selection invalidates
snapshots and forces byte discovery for 60 seconds (128-entry bound).

In a reversed-order four-menu workstation comparison, ordinary discovery took
2.493634/4.000386 seconds and the optimized catalog path 1.329349/1.364519 seconds:
2.41x by the means, with identical four choices and unchanged concurrency. Real
conversion/CDN traffic was used, but origin metadata was recorded/preloaded and
Telegram was not contacted. Real PostgreSQL admission/history/menu measured
1.402028 seconds, then 0.009982 seconds after source/application-cache expiry;
both durable menus retained four choices. These are menu results, not a fresh
origin/production guarantee or proof of improved bandwidth. The selected 720p
header still took 3.450066/1.631546 seconds; some work has deliberately moved to
selection. See [the dated catalog evidence](YOUTUBE_INSTAGRAM_ACCESS.md#october-15-cross-checked-catalog-and-selected-file-probes).

New transport evidence was negative: anonymous HTTP/3 plus Google DoH watch reads
timed out at seven seconds for both tested IDs. Do not repeat that profile without
changed connection evidence. The first menu comparison and a later single-slot
conversion attempt returned HTTP 400 and were discarded as speed measurements.
A subsequent single conversion returned HTTP 200, justifying the successful
comparison after recovery; no login bypass, unlimited-service or automatic-retry
claim follows. More concurrency did not cause the measured improvement.

The new selected-file path also passed complete production delivery preparation
with mocked Telegram: 360p video 76,705,138 bytes/2236.4 seconds, with faststart and
preview, and MP3 35,783,165 bytes/2236.44 seconds. All bytes were acknowledged by
the test uploader. The final full suite passed **1,033 tests, zero skips** with
isolated real PostgreSQL 17 and Redis 7.4; lint/format and provider architecture
checks passed. This is local correctness/concurrency evidence, not a production
throughput claim. Runtime/research bodies and signed URLs remain ignored.

October 15 initial-response follow-up: PyTubeFix 11.2.0 `AsyncYouTube` did not
return formats within a separate 12-second subprocess deadline. Its async facade
also calls synchronous Innertube methods. It was not added as a dependency.
Sharing Chrome's pool for rendition probes gave small variable results, not an
established speedup, and was not integrated. A direct cold workstation inspection
also failed metadata access in this connection state; do not turn that into a
successful cold-download claim.

The actual correction acknowledges slow YouTube work after 150 ms and edits the
reply into a text menu, avoiding the external-thumbnail RPC before buttons can be
used. Full verified catalogs now outlive their short signed-source snapshots:
default 300 seconds, bounded at 128 entries, only complete discovery eligible for
the extended TTL. Callback-only or incomplete discovery cannot overwrite a full
catalog with one quality. Selection refreshes/validates the chosen source; refresh
failure invalidates the snapshots. This fixes repeated four-rendition work after
the application's 60-second metadata cache expires without extending URL expiry.
Cold full menus still depend on the external service. Fresh conversion/header
tests plus mocked Telegram responses measured first acknowledgment at about
0.15 seconds, full menus at 3.127-4.790 seconds, and local cached menu execution
under 1 ms with source/origin snapshots cleared. These last timings exclude real
Telegram and SQL; they are not server latency promises. Short video and MP3 again
passed full real transfer validation. See [the dated response investigation](YOUTUBE_INSTAGRAM_ACCESS.md#october-15-initial-response-and-complete-catalog-reuse).
A further full dispatcher/admission/history/menu run used real PostgreSQL: fresh
conversion menu 3.119256 seconds, then 0.010506 seconds after simulating the user
interval and source/application-cache expiry. Both durable menus kept four
qualities. Telegram stayed simulated and origin metadata was preloaded; this
establishes SQL-path repeat-request savings, not a cold-origin/server guarantee.

October 15 unindexed-video fix: the newly reported `-YVIEsTEZCw` was absent from
the public search index even though its own watch page returned exact playable
metadata. Revision `5c89838` reproduced the metadata error after 14.409 seconds.
Origin-first metadata and an immediate verified guest route produced a complete
menu in 5.061/5.130 seconds without a native worker. Complete 360p, 720p and MP3
transfers passed duration/integrity and preparation checks. Telegram calls were
simulated; no production upload or universal access claim follows. The old reported
ID and a short video also passed fresh menus, and the short video's full video/audio
passed. See [the new dated evidence](YOUTUBE_INSTAGRAM_ACCESS.md#october-15-unindexed-video-metadata-and-menu-latency)
for byte counts, boundaries, failed query/oEmbed attempts and retry conditions.

Metadata no longer depends exclusively on third-party search. Preserve exact-ID
checks, three-second/4 MiB origin bounds, bounded caching/coalescing, fail-fast
metadata/key cancellation and the shared origin 429 cooldown. Do not strip a
leading minus from an ID or substitute a neighboring search result. The observed
index miss does not establish that a leading minus caused it. Default anonymous
playable VODs may use the verified resolver first; explicit native client/PO-token
configuration and account pools retain their existing native-first path.

October 15 performance follow-up: complete-file and reversed-order comparisons
did not establish a repeatable HTTP/2 improvement on the guest relay. Do not
promote a short prefix timing to a full-file speed claim. Newly reviewed Cobalt
instances returned empty tunnels or failed; loader.to completed an exact-ID job
but returned advertising redirects instead of media. Neither was integrated.
See [the dated performance evidence](YOUTUBE_INSTAGRAM_ACCESS.md#october-15-complete-file-performance-comparison).

Native split progressive tracks now use provider-owned 8 MiB ranges below the
documented 10 MB throttling threshold, followed by one local codec-copy mux. The
old DASH path bypassed the range policy inside network FFmpeg, then needed a
second mux. Bounded 256 KiB staging reduces disk opens and worker dispatches.
Loopback replay verifies an improvement under **explicit simulated large-request
throttling**; unthrottled replay is slower because of extra request round trips.
The real anonymous relay transfer still took about 39 seconds. No several-fold
live relay/CDN or Telegram speed increase has been demonstrated.

Compared the maintained Python implementations yt-dlp and pytubefix, including
their Innertube clients, JavaScript solvers and PO-token integration. Legacy pytube
or youtube-dl is not evidence of a working alternative for a modern challenge.

| Attempt / observation | Result and lesson | When to reconsider |
| --- | --- | --- |
| yt-dlp stable `2026.8.19`, default and `web_embedded`, reported `l6zflbTGNFQ` | October 10/13 runners returned `youtube_login_required`; no complete stream was downloaded | New extractor/client fix or accepted authorized session/egress |
| `visionos`, `tv_downgraded`, `mweb` attempts; pytubefix `ANDROID_VR` | Earlier workstation attempts also failed; another client name is not a universal cure | A documented upstream client/context change |
| Node installed, EJS installed | Solves supported signature challenges; does not by itself authenticate player access | Inspect the actual safe error code, not only Node availability |
| PO-token plugin installed without its helper | No running token service is created by installing the Python plugin | A configured healthy helper and a successful full-stream check |
| October 14 pytubefix `11.2.0`, `WEB` automatic-token and `ANDROID_VR` paths | Both stopped with `URLError` on this workstation; no extraction/download success | A working connection; adding this library alone is not an established fix |
| October 14 yt-dlp with Chrome impersonation and IPv4 | Timed out here; no complete stream | Changed transport evidence; do not force this as a default without proof |
| Official nightly `2026.10.10.233141.dev0` | Contains new `tv_samsung`; October 14 independent check still returned `youtube_login_required` for this exact video, with zero full streams | Authorized accepted session/egress or a new evidenced origin fix; the upgrade alone did not resolve this input |
| October 15 workstation public-DNS comparison, no account | yt-dlp extracted 65 formats; PyTubeFix 11.2.0 `ANDROID_VR` extracted 25 streams. PyTubeFix's native download returned HTTP 403, while small HTTP Range header probes succeeded | Compare extraction and actual CDN transfer separately; library metadata is insufficient |
| October 15 complete guest YouTube transfer with corrected DNS | Project transfer/MP4 validation consumed 58,521,501 video bytes (1,300.5 s) and 21,048,861 audio bytes (1,300.55 s) to EOF, twice; the second run used the optional production DNS tunnel | This verifies those accepted sources on this workstation, not fresh extraction or Telegram delivery |
| Fresh extraction after the successful guest capture | Native yt-dlp worker, repeated API extraction and PyTubeFix were subsequently challenged; `VISION_OS` returned bot detection and `TV_EMBED` timed out | Do not claim that DNS alone cures the player challenge; wait for changed origin/connection evidence rather than hammering |
| Later October 15 local bgutil 2.0.2 helper with native mweb/web_safari and forced player/GVS token contexts | The healthy helper actually minted tokens, but the exact video still returned a login challenge; TV Samsung bootstrap/alternate API-host and HTTP/3 methods also failed | Token generation alone is not accepted player access; revisit on a documented origin/client change |
| Simatwa/ytube-api public protocol comparison | The current request-key API requires a canonical `link` query parameter omitted by the reviewed wrapper; adding it produced a fresh anonymous download URL. The metadata host redirected from `wwd` to `mw.mp3juice.blog`; use the verified HTTPS endpoint | Revisit protocol/host changes after a specific failure; do not guess player names or treat HTTP 200 alone as media |
| Final native project YouTube fallback, reported exact ID | Fresh anonymous inspection listed actual 360p/720p/1080p H264+audio and MP3 128. Every listed option reached EOF and passed project integrity checks: 36,176,593 / 56,705,073 / 143,789,188 / 20,808,765 bytes respectively; video duration 1300.5 s, audio 1300.536 s | Verified on this workstation, without Telegram or production access; third-party service availability and quotas remain external |
| Public signed URL reused after expiry | Returned 404. Fresh key/conversion immediately followed by full consumption worked. URLs expire in approximately 60 seconds; native selection refreshes short-lived sources and validates exact ID/dimensions | Regenerate on selection after snapshot/URL expiry; do not persist signed URLs or loop the expired one |
| Public converter requested with `format=m4a` | HTTP 400; MP3 128 succeeded completely and is labeled as conversion rather than original AAC | Only add other audio formats after current API acceptance and full validation |
| Later October 15 production upload remained at zero after download | Supplied logs show a failed transfer, not an active upload: 14.338 s size probe, 14.921 s download, 0 uploaded bytes, failure in `require_video_info`. Workstation reproduction found a valid prepared `moov` of 1,360,563 bytes exceeding the old 1 MiB inspection bound | Check preparation/index/thumbnail bounds together; raw EOF verification alone does not exercise delivery preparation |
| YouTube preparation/performance correction | Full fresh 720p transfer passed actual delivery preparation and preview generation with simulated Telegram RPCs: 57,470,469 bytes acknowledged, 1300.5 s, faststart and preview confirmed. Public menu baseline 12.865 s; later complete four-choice menu 5.558 s. A separate run took 8.463 s, showing external variance | Workstation and mocked Telegram evidence, not a measured production upload speed or a fixed response-time guarantee |

Upstream [PR 17805](https://github.com/yt-dlp/yt-dlp/pull/17805) changed default
clients on October 10. The [official nightly release](https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/tag/2026.10.10.233141)
records that change and the updated embedded Safari user agent. This is a specific
upstream fix, not a floating automatic upgrade.

For native discovery, retain CDN verification before quality ranking. The public
catalog optimization above may defer byte verification only with the two matching
metadata sources, and must verify the selected file before returning it.
An inaccessible preferred rendition
must not conceal an accessible alternative. Video and its separate audio must both
work. Preserve the provider's small HTTP ranges during audio validation. Explicit
`YOUTUBE_PLAYER_CLIENTS` and configured token-helper clients remain authoritative;
leave overrides blank to use the pinned extractor's new defaults without a helper.

One stalled account must not consume the entire request deadline. Account failover
is bounded to three attempts, 25 seconds per attempt when alternatives exist. Do
not try the identical authenticated default client again under the name `guest`.
HTTP 429 stops player/account failover and enters cooldown.
Rotate bounded account selections fairly and keep the default fallback reachable
even when many database accounts exist; otherwise a healthy later account can be
permanently hidden behind the attempt cap.

## Instagram

October 15 menu-latency follow-up: avoid reading an entire 1 MiB sample when a
complete MP4 index is already present. A leading `mdat` now sends metadata lookup
to a 64 KiB suffix, expanding once to 1 MiB if the movie index is larger. The
provider coalesces equal sample reads and shared DASH audio inside one inspection
or selection, bounded to 4 MiB/128 results. The cache ends with that operation;
selections still verify fresh bytes and reject changed dimensions. Resolver
validation and the immediately following discovery share their checks. Post-refresh
locks are keyed by account/post instead of serializing unrelated posts. Menus use
text/buttons without a Telegram external-thumbnail fetch, and log their actual
elapsed time after sending.

Guest canonical public-post fallback uses an eight-second native budget, adjustable
with `INSTAGRAM_NATIVE_TIMEOUT_SECONDS` (1-35). Successful anonymous resolver routes
are preferred for that exact identity for five minutes, capped at 128 identities;
other posts retain native discovery and their complete quality catalogs. A failed
preferred route allows one native recovery, without another resolver loop. Stories,
shares and authenticated requests retain native-only behavior. Native and public
429 cooldowns stop alternatives. The post page now precedes a homepage bootstrap;
a usable page or page LSD avoids that request. No credentials or concurrency
increase were introduced.

A reversed-order replay against revision `2a57873`, using recorded three-post
response shapes and explicitly paced mock HTTP bodies, retained exactly the same
quality identities/dimensions/protocols. Audio reel: 13 to 7 sample requests and
3,155,968 to 22,528 consumed bytes; muted reel: 9 to 7 and 9,437,184 to 114,688;
carousel: 12 to 6 and 12,582,912 to 98,304. Those are controlled discovery savings,
not live CDN/Telegram throughput. See [the dated Instagram speed evidence](YOUTUBE_INSTAGRAM_ACCESS.md#october-15-instagram-quality-menu-latency).

Fresh live workstation checks were negative in this connection state: the old
exact-reel inspection failed after 60.0229 seconds; intermediate/current variants
failed after 33.0098/33.0163 seconds. The resolver returned a matching CDN URL, but
native TCP and HTTP/3 CDN access timed out. HTTP/2 Cloudflare DoH, HTTP/3 Cloudflare
DoH/IPv4 and HTTP/2 Google DoH/IPv4 small probes also timed out at five seconds;
Google's JSON DNS checks timed out as well. The returned `_nc_ht` matched its URL
host, so there was no evidenced regional-host alternative. No hostname rewrite or
new network-profile fallback was integrated. Retry only after connection recovery
or new transport evidence; none of these runs is a successful live download or
production menu-speed measurement. Instaloader's synchronous request/sleep/retry
path was re-reviewed, not installed or retried as a presumed transport cure.

Compared yt-dlp, Instaloader, Instagrapi and gallery-dl against their current primary
source. Different POST envelopes and document IDs are distinct methods, not aliases.

| Attempt / observation | Result and lesson | When to reconsider |
| --- | --- | --- |
| Reported `DeR9eU8idi0`, logged-out root response `xig_polaris_media: null` | Not proof that the reel is deleted/private; continue supported alternatives | Query/session/connection changes or the next supported method |
| Instaloader shortcode document `27128499623469141` | October 13 workstation returned `execution error`; older `8845758582119845` did not recover it | A documented query change, rather than repeatedly guessing IDs |
| Current Instagrapi document `27830990013244856` | Added October 14 with `PolarisPostRootQuery`, its current variables and available session LSD/DTSG | Verify from the actual connection; retain legacy fallback for different deployments |
| October 10 and 13 independent runner tests of the reported reel | All eight qualities reached EOF with audio; 11.7–11.815 seconds against rounded 12 seconds | Historical evidence only; run fresh checks for a new deployment |
| October 14 independent check at `9a8cd23` | All eight qualities again reached EOF and passed completeness checks; sizes 359,898–2,232,676 bytes | This verifies that connection, not the failing workstation or production host |
| Later carousel/reel requests in a multi-link audit | HTTP 429; more immediate API attempts would create a retry storm | Cooldown expiry, refreshed authorized access or origin recovery |
| October 14 direct workstation tests | Transport/extraction timeouts, including after the new query; no local full download | A working connection or authorized session; do not call this a successful fix |
| October 15 public DNS and HTTP/3, guest only | Corrected DNS over TCP still timed out; HTTP/3 returned HTTP 200 generic HTML, but neither page nor embed exposed media | HTTP 200 is connectivity evidence, not extraction success |
| Instaloader 4.15.4 and current project queries on the HTTP/3 route | Instaloader's native TCP request timed out; its request through HTTP/3 returned `execution error`/no media. Current root, web and shortcode requests also failed | A documented origin/query/connection change; swapping to Instaloader alone is not a verified fix |
| Fresh post LSD, matching browser UA, guest mobile REST and Relay action envelope | Post token/UA changes did not recover GraphQL; mobile REST returned 404; web REST returned HTML; action Relay returned 401 | Do not ship guessed query IDs, call HTML a media response, or repeat these unchanged methods |
| Later October 15 fresh guest browser on exact reel | Real browser rendered an owner-set under-15 content restriction, without a playable video. This establishes a post-specific guest restriction on this connection, beyond earlier transport failures | Do not call all Instagram media broken or infer the user/creator's actual age |
| Anonymous public resolver, followed by original Instagram CDN | `dwnld.nichind.dev` returned an original CDN URL for the exact shortcode. HTTP/3/DoH consumed 1,468,542 bytes, 11.7 s, with audio; SHA-256 exactly matched the October 14 successful same-input audit | This is a third-party resolver dependency; it is not direct native GraphQL success or proof that every public service works |
| Native provider with anonymous resolver fallback and default network configuration | Fresh inspection, selected-quality resolution and project full-stream validation succeeded on this workstation; automatic HTTP/3 CDN recovery was required. Repeated final check completed in 59.417 s, transfer in 2.698 s | Preserve identity/CDN/duration checks, provider scope, bounded retries and admission. Never forward cookies, stories or private share tokens to the resolver |
| Curl streaming queue/early-close behavior | Upstream async streaming queues body chunks without a consumer cap and close waits for EOF. Added a 256 KiB Python receive window, libcurl pause/replay, early cancellation and pre-header cancellation cleanup | Keep slow-consumer/full-byte/reused-handle/cancellation regressions when upgrading curl-cffi; the Python queue bound is not a total native-memory guarantee |

Authenticated media-info responses may omit the shortcode. Accept only an exact
numeric identity match, including the known `POLARIS_`/owner-ID wrappers. Reject
another post's identity. Optional page/API login failure may fall through to a
working supported query; exhausted alternatives must preserve a safe specific
login/query/network diagnostic. Do not replace it with a misleading deletion claim.

The provider uses its own Chrome TLS/HTTP2 transport through `curl-cffi`. After
pulling code, install project dependencies; missing `curl_cffi` prevents importing
the provider. Cookie rotation belongs to the owning domain-scoped jar. Never send
session cookies, CSRF, LSD or DTSG to CDN hosts. Use the same configured proxy for
extraction, source sampling and full transfer.

The operator explicitly requires **anonymous access only** for the current recovery
investigation. Do not ask for or use account cookies as its solution. The October 15
[guest transport comparison](YOUTUBE_INSTAGRAM_ACCESS.md#anonymous-github-library-and-transport-comparison-october-15)
records complete YouTube evidence, fresh-extraction failures and the later successful
Instagram resolver path. Provider/account configuration supported by older deployments is
not evidence that any credentials were used in these tests.

Failed bootstraps are coalesced for ten seconds rather than repeated by every
concurrent request. Missing LSD is cached briefly, not forever. A timed-out guest
must yield to a configured authorized account; raw `TimeoutError` must not bypass
account failover. Rate-limit errors stop all alternatives.

Try an authorized media-info API before a homepage bootstrap. REST access does not
need the page's LSD token; a blocked homepage must not consume the authorized
account's budget before its working API is contacted.

## SoundCloud

Both reported tracks, `quavoofficial/away` and `octobersveryown/quebec`, exposed
protected full AAC streams in dated origin checks. SAMPLE-AES/SAMPLE-AES-CTR with
Apple/Widevine key formats, broken ordinary MP3 alternatives, and an unauthorized
guest original-download response were observed. Legacy stream routes did not
produce a working ordinary full stream. These attempts must not be repeated as a
blind fallback loop or described as successes.

Normal public `gdaal/mojezeh` completed three full qualities in the October 10 audit,
approximately 212 seconds each. Supported full streams and authorized creator
original downloads remain valid paths. An authorized account can expose additional
formats, but is not proof of access to a protected stream.

Do not implement DRM license/decryption logic, substitute another song by title,
or advertise a preview as the requested full track. Reconsider a failed track when
the origin exposes a supported full stream or an authorized original download.
Use the numeric track ID; mutable slugs require fresh discovery.

## Pinterest

October 15 menu-latency follow-up: the four-request provider bound was retained.
Exact shared HLS audio playlists are now coalesced only inside one discovery;
selections refresh pin/source metadata in a fresh batch. The memo is bounded to
128 results and 4 MiB of encoded playlist text, including safe failed-result
identities, with no cross-client or cross-discovery reuse. A transport failure
keeps `pinterest_transport_failure` when reconstructed for followers. Do not
repeat an already failed shared playlist for every rendition in the same batch.

MP4 prefix probes close after a complete `moov` or before media payload. EOF
metadata uses a 64 KiB suffix with one expansion to the former 256 KiB limit.
Keep preview-duration, actual dimensions/codec, CDN allowlist, complete HLS/audio
and selection-refresh checks. Skipping the public page would lose published
modern encodes; both API metadata and page enrichment remain on the cold path.

Workstation live ABBA comparisons preserved identical 23- and six-quality catalogs
on the two known pins. The 23-choice pin averaged 4.999497 seconds before and
3.854550 afterward; request count fell 31 to 27, MP4 returned sample bytes
1,179,648 to 162,816. The six-choice pin averaged 2.354261 to 2.223617 seconds;
that small difference may include network variation. A subsequent selection
refreshed both MP4/HLS and consumed the MP4 to EOF (971,092 bytes). No live Telegram
or production test was performed. See [dated evidence](PROVIDER_VERIFICATION.md#october-15-2026-quality-menu-latency)
for individual times, source scope, payload accounting and preserved failures.

The same investigation removed external-thumbnail waits from Pinterest,
SoundCloud, XVideos and XNXX menus; Instagram/YouTube already avoid them. Quality
buttons now arrive in a text reply without an external photo or link-preview
fetch. Final-file artwork is independent. Separate inspection/storage/Telegram
durations are logged to identify remaining production latency. Controlled mock
handler timings must not be presented as native Telegram speed measurements.

October 10 independent checks completed all 29 qualities of two pins. Pin
`2885187256207927` also completed full progressive/HLS streams and Telegram
registration in an earlier check without publishing messages.

An HTTP-successful MP4 can be only a short preview. Discovery/probing must not
overwrite the full advertised duration with the shorter preview duration. Reject
such a rendition. Telegram's external URL cache previously returned a mismatching
size: unpublished registration detected it and triggered the local streaming path.
Do not trust Telegram external fetch acceptance alone or discard other qualities
because one was cached.

## XVideos

Public URLs and embed URLs can resolve to the same canonical video. Some dated
samples redirected successfully in one run and returned 404 in another. HTTP 404
is recorded as a failed input; it is not evidence that every video on this provider
is broken. Use current public samples for comparisons and preserve their exact ID.

The October 10 final audit completed 13 full qualities from two working samples.
The longest source was approximately 1,238 seconds and the largest output was
420,388,463 bytes. Workstation connection timeouts were separate failed attempts.
Do not retry an obsolete/deleted URL recursively or choose another video because
its title matches. Carry the source duration into progressive/HLS verification.

## XNXX

The October 10 final audit completed seven full qualities of one working input,
approximately 469 seconds each; another input returned 404. Earlier runs had
different valid/deleted samples. Treat deletion and connection failure separately
from parser/transfer defects. Public/embed/title aliases must normalize inside
this provider; do not import the XVideos implementation because shapes are similar.
Carry origin duration into progressive/HLS verification.

## Cross-provider mistakes already corrected

| Mistake | Required behavior |
| --- | --- |
| Building a repeat-link quality menu only from saved Telegram files | Discover the full current provider menu; the full redacted catalog is a short outage fallback independent of file rows |
| Trusting old Telegram references because metadata advertises a full duration | Rebuild legacy unverified/incompatible files once on demand; persist only a successfully validated replacement |
| Publishing an external URL result before checking it | Register without publication first; check known size, expected duration and video compatibility; fall back before any chat message |
| FFmpeg HLS/fMP4 returning only an initial segment | Preserve `-http_seekable 0`, successful completion, no skipped segments and completeness validation |
| Letting full audio conceal short video | Validate received video and required audio separately |
| Ignoring initial MP4 samples when fragments follow | Include initial sample-table duration plus fragment duration; VP9 regression reproduces this |
| Dropping required small YouTube HTTP ranges while validating audio | Validate M4A directly; feed other ranged audio into a bounded cancellable FFmpeg pipe |
| Calling every stream-friendly MP4 compatible with every Telegram player | Preserve the newer classic-faststart preparation, preview registration and cached-playback repair from upstream |
| Repeating work for concurrent identical requests | One producer per durable content/quality/account key; followers publish concurrently after persistence |
| Interpreting host network totals as bot download traffic | Host metrics include other processes and Docker interfaces; use transfer payload counters and stage timings for bot-specific traffic |

Selecting all host interfaces can count bridge/veth traffic more than once. Set
`METRICS_NETWORK_INTERFACE` to the primary interface, such as `eth0`, when measuring
host uplink traffic. This still does not measure Telegram-to-origin external fetches.

The 1,000-distinct-input local benchmark completed all simulated Telegram transfers
in 12.066 seconds with 1,000 HTTP connections and 64 upload parts, without dropped
logs. This is synthetic pipeline evidence, not production Telegram throughput or
a guarantee that every origin will accept 1,000 requests.

## Deployment, state and logging lessons

- Python runs on the host through `python main.py`; PostgreSQL and Redis run in
  Docker. Node is a host YouTube challenge runtime. Do not put the whole bot or
  Node into Docker unless the operator changes that deployment requirement.
- Inside Docker, `postgres:5432` refers to the Compose service. A native host
  process must use the published `127.0.0.1` address and configured host port.
  The earlier startup `gaierror` occurred before Telegram handlers could run;
  repeatedly sending `/start` cannot repair database DNS/configuration.
- Ports/passwords in `.env` construct the native database/Redis URLs when explicit
  URLs are blank. Docker reads the same password settings. Do not paste secrets
  into source or publish `.env` while investigating a connection mismatch.
- Preserve database volumes and additive migrations. Earlier migrations adopt old
  state, full-file verification, video preview and classic-faststart statuses;
  deleting data to repair cached playback loses user history unnecessarily.
- Only one coordinator for a Telegram account should run at once. Stop the old
  process before restarting; do not start another real bot as a diagnostic test.
- `/start` is optional for link handling. User creation/history and durable owned
  menus belong to the normal link workflow. Telegram callbacks validate owner,
  chat, bot and quality identity before transfer; restarted menus refresh missing
  sources rather than persisting signed URLs.
- SQL is authoritative for history, unique files, state and one-link-per-minute
  admission. Redis is an accelerator, not the only durable source of truth. A
  shared producer lock plus a unique database key prevents duplicate media rows.
- Reused files have no new origin download payload. External URL downloads occur
  on Telegram's network; local counters cannot measure their origin traffic.
- Inspect `logs/activity.log`, `logs/system.log`, `logs/telegram.log` and
  `logs/performance.jsonl` together. Interval metrics alone cannot reveal whether
  failure happened at extraction, CDN verification, remuxing, registration or send.
  Preserve safe error codes and phase timings; suppress raw library exception text
  because it may contain signed URLs/credentials. Track dropped log records and
  writer errors rather than assuming every event reached disk.

## Verification and upgrade checklist

1. Fetch and integrate upstream without force-pushing or discarding local work.
2. Read this file and the provider's prior evidence. Change one evidenced mechanism.
3. Install the pinned dependencies with `python -m pip install -e .`; `git pull`
   alone does not change the running virtual environment.
4. Run provider/architecture tests and relevant transport regression checks.
5. Use `python tools/diagnose_video_access.py --live` for the exact reported video
   inputs, or `tools/audit_providers.py --all-qualities --stream --strict` for all
   providers. Record failure honestly. Do not print private reports or credentials.
6. Recheck from an independent runner if comparing connections helps. Do not route
   production downloads through CI or claim that the production host was updated.
7. Update this record and dated evidence with actual counts/results, then commit
   and push code and documentation together. Preserve failed observations.

At `9a8cd23`, October 14 Linux CI passed 769 tests with PostgreSQL/Redis. The
[Instagram check](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38603495934)
passed; the [YouTube check](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38603501343)
failed before any download. Neither check published Telegram messages. Later local
and final publication results are recorded in the dated access report.

After the final account ordering/rotation fixes, local validation passed 767 tests
with five dedicated-database tests skipped. At `d042689`, the final
[Linux code/database run](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38605040464)
passed **772 tests** with PostgreSQL/Redis, lint and formatting. These successful
code checks do not change the recorded local connection failures or YouTube's
remaining independent-runner login challenge.

## October 14 request-rate follow-up

At this stage the operator's publication condition required successful reported
YouTube/Instagram downloads together with the 1,000-links/second requirement.
Publication was held. A subsequent explicit reference-layout request separately
authorizes committing/pushing the structural work and the preserved improvements.
Neither publication nor passing simulations establishes successful origin access
or the still-failed distinct-cold throughput target.

New WSL HTTP probes of the exact YouTube/Instagram identities both failed with
`URLError` wrapping `OSError`, errno 101 (`ENETUNREACH`), without an HTTP response
or certificate error. This differs from the previous independent YouTube
`youtube_login_required` response and Windows extraction timeouts. A fresh
authorized route/session is the retry condition; larger concurrency, another
unchanged guest retry or Node installation does not repair these failures. No
remote production access was configured or used in this follow-up.

The incoming request investigation found eight SQL calls per cached link, seven
usable connections in the old eight-connection pool, an O(n) menu-cache rebuild,
capacity counting duplicate inspection followers and queue waits outside the
metadata deadline. The optimized cached path uses three atomic statements;
catalog/alias writes add one statement for cold inputs. Ownership guards retain
newer requests, rollback retains quota, history is durable before origin work,
and a menu commits with completed history. In-process quota leases can only deny,
never grant without SQL. Snapshot serialization projects explicit immutable
fields; credentials and signed sources remain excluded.

New handler admission reserves a bounded slot before SQL/pool waits. Excess
requests do not consume quota. Rejection messages have a 32-RPC cap, five-second
timeout and bounded five-second user/chat/code suppression. Failed rejection RPCs
are contained. This suppression does not apply to accepted menus. Distinct
metadata keys share work with duplicate followers; the 90-second deadline includes
both keyed-lock and worker waits. The SQL pool defaults to 32, configurable 2-64.

Actual PostgreSQL 17 tests on workstation Linux (eight qualities, simulated
origins/Telegram) injected 15,000 arrivals at 1,000/s. The committed cached
baseline took 49.960 seconds, p95 42.096 seconds. Optimized cached processing
completed in 15.029 seconds, p95 0.146 seconds, with no exception/log loss. An
initially cold 100-link shared workload completed all 15,000 menus in 15.036
seconds, p95 0.048 seconds, with exactly 100 source inspections. A same-user
5,000-arrival test performed one SQL admission check, created one history/menu
and issued one rate warning; the remaining 4,999 attempts were rejected locally.

Preserve these **failed** approaches as well:

- Raising the old SQL pool alone reduced the initial Windows five-second burst
  from 22.452 to 13.120 seconds, still failing response latency. Cache changes
  alone remained 13.076 seconds. Compound SQL steps reduced this to 8.204 then
  5.849 seconds. A longer Windows run still failed (18.668 seconds, p95 5.607).
- With distinct cold links, a simulated 50 ms origin and eight workers,
  1,776 of 5,000 inputs produced menus; 3,224 hit controlled overload. Peak
  active handlers was exactly 1,000. Zero escaped exceptions does not make this
  an all-download success.
- Increasing workers to 64 at pool size 32 produced 12,575 of 15,000 menus;
  2,425 were rejected. A CPU profile identified two recursive dataclass snapshots
  per inspection. Explicit projections improved this to 13,645 menus, still
  rejecting 1,355. Increasing the pool to 64 reached 14,673 menus but rejected
  327, p95 1.434 seconds: the all-menu/response target still failed.

Do not repeat these configurations expecting every cold request to pass without
new storage/extraction capacity or an evidenced bottleneck change. Increasing
queue capacity alone hides overload as latency. The real origin and Telegram
quotas remain outside the simulated benchmark; the cold target has not been
established universally. Request-rate is separate from the previous 1,000-task
file-transfer test. Full methodology, commands and results are in
[the request throughput report](REQUEST_THROUGHPUT.md).

Final current-tree validation passed **790 tests** with dedicated real
PostgreSQL/Redis services, zero skips, and two expected Windows curl-cffi event-loop
warnings. Lint, formatting, provider boundaries and English-comment checks passed.
These code checks do not turn the failed cold-load or live-access probes into
successful downloads.

The final cached-path rerun used the application's default 10,000-record log
queues per stream, retained all file logs and disabled stdout. All 15,000 menus
and histories completed in 15.029 seconds at 1,000 incoming links/s, p95 0.055
seconds, peak 125 handlers and maximum loop delay 7.8 ms. No requests were rejected,
and no log records were dropped or failed to write. Sources/Telegram remained
simulated; the distinct-cold and real-access publication conditions remain open.

## October 14 Couplyo reference-layout update

The supplied `PROJECT_REFERENCE_FA.md` was read as architectural source material,
including its remaining-risk sections. Its embedded example agent prompt is not
an additional operator instruction. The latest explicit request authorizes this
structure update and commit/push separately from the earlier unresolved access
investigation.

Application layers now live at the root, beside `main.py` and `container.py`.
Native SQL model definitions, the six-revision registry, a fresh-connection Unit
of Work, a central dispatcher, a conversation service/storage boundary, UI text,
keyboards, emoji presentation and previews have separate owners. Provider folders
retain their required original paths and six independently owned files. Existing
data, source IDs, callback prefixes and session paths are retained.

New lessons: unknown/malformed state should return safe MAIN without erasing the
original evidence; state patches must merge atomically rather than get/set; a UoW
must rollback and release on cancellation; an external Redis pool needs explicit
client ownership (`Redis.from_pool`) to close it; file cache envelopes must verify
key identity and every field/type, including new defaulted fields. Session opening
must wait for the worker on cancellation and close its result. Archive/packaging
checks must include the new root packages, and async/language architecture checks
must scan root code as well as `src`.

The post-refactor Linux cached test completed all 15,000 inputs at 1,000/s in
15.031 seconds, p95 75.4 ms, maximum loop delay 6.3 ms and peak 130 handlers.
It used real SQL, the dispatcher, eight qualities and 10,000-record log queues,
with no capacity rejections, lost logs or escaped exceptions. Sources and Telegram
were simulated. The earlier failed cold runs and live-access restrictions remain
valid evidence. See [the mapping](REFERENCE_MAPPING.md) for exact boundaries and
backend choices; no production host was accessed or updated.

Final validation of the reference update passed **814 tests** with real dedicated
PostgreSQL/Redis, zero skips, lint/formatting, root/provider architecture and English
comments. The 0.2.0 wheel builds and its root/provider imports load in isolation.
The native migration entry applied all six revisions to the disposable test
database. No real Telegram session was authenticated and no messages were sent.

## October 14 premium emoji catalog

Validation: 111 affected presentation, handler, progress, workflow and architecture
tests passed. Ruff lint and formatting checks passed across the application and
tests. Live Telegram artwork rendering was not part of this validation.

All thirteen supplied short names are registered in `bot/button_icons.py`, alongside
the existing IconsEmoji fallback. Selection uses actual custom-emoji metadata and
semantic priorities, with explicit operator packs taking precedence. Missing packs
are isolated, background metadata work is bounded and cancelled/awaited during
shutdown, and FloodWait pauses catalog requests without blocking user handling.
No IDs are guessed from addemoji previews and no unrelated seasonal/finance symbols
are inserted into download-status text merely to use every pack.

Telegram entities must wrap the document's actual Unicode alt. Normalize only
presentation-selector variants, recalculate UTF-16 offsets afterwards, and avoid
partial matches inside joined/skin-tone emoji sequences. Keep explicit caller
entities unchanged. Start/provider replies, home edits and progress message edits
must all use the same adapter; adding packs to initial replies alone left status
edits plain. On permission/custom-entity rejection retry the reply/edit once in
Unicode, then suppress custom sends for that runtime. Never hide normal FloodWait
from progress rate handling.

Public preview pages were readable for some packs but unavailable for others;
they do not establish live metadata/rendering success. Tests use simulated Telegram
documents and RPCs. No real account was authenticated or messaged during this
update. Read [the catalog guide](PREMIUM_EMOJI.md) for registration, selection,
configuration and retry conditions.

## October 15 production deployment drift

The operator's safe production report shows yt-dlp 2026.8.19 rather than the
checkout's pinned nightly and an unavailable selected Node runtime. Default
YouTube/Instagram cookies, proxies and a token helper are absent; database accounts
were not checked. Upgrade the actual running virtual environment and fix its
runtime PATH before attributing the remaining result to the latest extractor.
These missing pieces do not prove that a challenged guest route will then work.

Fresh workstation checks still received no complete stream from either reported
identity. GitHub answered HTTP 200, YouTube timed out at TCP connect, and the
Instagram browser transport returned timeout code 28. This differs from production's
reported login/GraphQL errors. No production host was contacted by the agent.
The current official nightly/document remain the versions already implemented;
no speculative extraction alternative was added.

Fatal extraction errors must take precedence over incidental earlier warnings.
HTTP 429 must stop YouTube failover even when stderr also mentions sign-in.
Do not suppress Instagram HTTP 403 into an empty-media result, or claim that an
unknown GraphQL error proves network denial. Preserve safe operation/status and
schema/session/execution categories, with no response text or credential values.
Successful-request detail stays at DEBUG to avoid routine per-sample INFO traffic.

The diagnostic now flags package-pin/runtime drift; optional `--network` performs
bounded header-only probes, reports safe exception chains and separates transport
failure from received HTTP rejection. It uses separate provider transports and
does not send account cookies or run per user request. A failed transport setup
for one provider must not conceal the other's result. See the dated
[access report](YOUTUBE_INSTAGRAM_ACCESS.md#october-15-deployment-evidence-and-diagnostic-corrections)
for exact scope, observations and production upgrade commands.

Validation: 188 affected provider, diagnostic, architecture, nonblocking and
code-language tests passed, with two expected Windows curl-cffi selector warnings.
Ruff lint/formatting passed. These are code checks; both new local full-stream
attempts remain failed, and production download recovery remains unverified.

The operator's next production report confirms the editable reinstall succeeded:
yt-dlp now matches 2026.10.10.233141.dev0 and the only reported deployment issue
is unavailable Node. Header-only probes received YouTube HTTP 200 in 0.101 seconds
and Instagram HTTP 200 in 0.241 seconds. Subsequent real bot requests still returned
YouTube login-required and Instagram HTTP 403. This disproves a total production
connectivity outage; it does not prove player/GraphQL/CDN authorization. Do not
keep prescribing a package reinstall after the pin already matches. Node runtime
availability still needs repair, but is unrelated to Instagram and does not by
itself clear YouTube login challenges. The failing Instagram operation must be
identified from the safe origin-response/query logs or a same-host live check;
HTTP 403 alone does not establish whether a query, session or IP policy caused it.

The operator then supplied the production `--live` result at 07:49 UTC on October
15: Node 24.21.0 is available, package pins match and `deployment_issues` is empty.
YouTube's two guest player attempts both returned `youtube_login_required`.
Instagram's `web_graphql` returned a GraphQL error classified as `rejected`;
`shortcode_graphql` returned HTTP 403 in 0.146 seconds. Both inspections failed
before resolution, with zero full streams or Telegram messages. The diagnostic
still reported no environment-default cookies, proxy or helper; database accounts
were not queried. Do not attribute this result to missing Node, old packages,
Telegram upload, or the OS restart banner. These installation issues are resolved.
The next useful experiment is an authorized session/accepted route, not another
unchanged guest run. The GraphQL rejection's policy reason is not established.

The diagnostic's `.env` loader previously differed from `main.py` and its live
audit child. It now uses the same explicit-file precedence, BOM-safe UTF-8 and
disabled interpolation. A regression test proves stale inherited values cannot
override the file and literal `${...}` inside credentials remains unchanged.
This corrects reporting parity; it is not a live-access workaround.

The 34 affected diagnostic, architecture, provider-boundary, nonblocking and
code-language tests passed, with one expected Windows curl-cffi selector warning;
lint and formatting passed. No new unchanged-condition origin retry was performed.

At the operator's explicit request, one fresh bounded workstation full-stream run
tested the exact YouTube `l6zflbTGNFQ` and Instagram `DeR9eU8idi0` identities at
07:53 UTC on October 15. Python 3.13.14, Node 22.23.2 and all reported package pins
were available, with no deployment issues or configured default cookies/proxies/helper.
Both inspections failed with extraction timeouts before resolving any source;
zero complete streams and zero Telegram messages were produced. Instagram's
bootstrap, page, ruling, root and web GraphQL requests each logged connection
failures at approximately ten seconds, without origin HTTP status. This is not the
production login/403 result and not evidence that the reported posts were deleted.
The private report is `.runtime/workstation-20261015-direct.json`; raw reports and
local network configuration remain ignored. No extraction code changed for this
check. Further identical local guest retries need changed route/session evidence.
