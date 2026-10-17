# Administration and downloader security review

October 17 UI correction: [Couplyo keyboard behavior](ADMINISTRATION.md#couplyo-keyboard-behavior)
supersedes the historical reply-only UI decision below. Categories/forms remain
reply menus; native inline controls preserve owner-bound callbacks and fresh SQL
authorization. Destructive confirmation still uses expiring single-use SQL tokens.
Tests cover concurrent replay, forged owners, delegated roles, expiry, empty
filters and restart. Typed labels and callback strings cannot confirm operations.

The subsequent [actual-source registration/security audit](REGISTRATION_SECURITY_AUDIT.md)
adds cold sender resolution, durable private profile recovery, parent/member
handoff and poison-entry protections, with a matrix of remaining registration
conditions. Its final verification is recorded in the knowledge base.

October 16, 2026. Reviewed the runtime checkout starting at `3166eae`, including
Telegram dispatch, installation roles, state/menu ownership, rate admission,
administrative jobs and confirmations, SQL construction, import/restore,
provider URL entry points, subprocess invocation, transfers and logging.
This is a code/workstation assessment; no production server or live Telegram
account was accessed, no messages were sent, and no exploitation of external
services was attempted.

## Corrections and reviewed boundaries

| Boundary | Findings and resulting behavior |
| --- | --- |
| Admin UI | The category menus used reply keyboards, but directories, settings, pagination and confirmations still sent inline buttons. The registered runtime now emits ordinary reply keyboards for these paths as well. Old admin inline callbacks are rejected with instructions to reopen `/admin`. |
| Reply text and command spoofing | Text is looked up only in the current private owner's SQL conversation map. It cannot supply an action, target or confirmation token. Navigating/resetting clears the map; mapped menus expire after 20 minutes. Fresh numeric-ID/SQL authorization precedes execution. Group admin rights, usernames, names, forwarded authors and typed `adm:...` strings do not grant installation authority. |
| Stale confirmations | A new destructive confirmation receives a distinct operation reference in its label. Text from a previous confirmation cannot confirm a different current job. SQL confirmation tokens remain owner-bound, expiring, single-use and transactionally consumed. Concurrent same-owner UI work is serialized in the coordinator. |
| Quota bypass | Production previously had a fixed user interval with independent inspection/download stages. Revision 13 introduces fresh configurable rolling SQL windows. Every format selection, including an old/cached menu, consumes the download window; a full download window also blocks fresh links. Cross-chat and cross-process attempts share the same bot/user windows. |
| Group quota | The group row serializes active/send/migration checks and group admissions. Rejected user attempts do not consume group slots. Per-group overrides take precedence over the live global default. |
| File selection and cancellation | Quality menus and Stop requests remain bound to sender and chat, before quota/history/transfer work. Restored menus preserve that SQL scope. Quality bounds now explicitly reject negative indexes as well. YouTube policy is checked against the stored media site, not just a callback prefix. |
| Remote media input | FFmpeg previously inherited its broad protocol defaults. Remote inputs now permit only HTTP/HTTPS transport and supported crypto; they cannot open `file:`, concat or arbitrary other protocols. Pipe previews/streamed audio allow only `pipe`; staged local MP4 processing allows only `file`, preventing network fetches from staged content. CR/LF/NUL injection in FFmpeg source headers is rejected. |
| SQL construction | Reviewed the security-linter SQL findings: values use asyncpg parameters, selectable fields/order/filter fragments use explicit allowlists, and restore identifiers come from allowlisted tables and database column metadata. Imported text is not executable SQL. |
| Imports and backups | Existing root authorization, size/row caps, duplicate-key rejection, bot scoping, protected roles and additive restore remain. Backups do not restore roles, FSM, live jobs or quota grants. A failed quota transaction cannot create a user/conversation or consume a group slot. |
| Worker authority | Jobs/effects recheck current authorization and policy; confirmation/leases prevent repeating completed actions. Cached roles are not authoritative. |
| Secrets and parsing | Runtime `.env`, cookies, sessions and logs remain ignored. Logs suppress credential fields and raw signed URLs. Provider sessions remain separate. The XML linter warning was checked against the existing 512 KiB cap and explicit DOCTYPE/ENTITY rejection. Telegram's MD5 upload checksum is not used for authentication or signing. |

## Verification scope

New tests exercise reply-only download settings, user/group edits, persistence
across conversation-manager recreation, stale confirmation labels, typed callback
payloads, other owners, role revocation and disabled old admin callbacks.
Real PostgreSQL 17 tests exercise 100 concurrent requests through four repository
instances, live quota increases/decreases, restart, cross-chat and bot isolation,
group overrides, rollback behavior and adoption of legacy cooldown timestamps.
An application-handler test permits the first format and answers the next click
with remaining wait time without a second delivery. Existing ownership, import,
state, job, cancellation and architecture regressions remain applicable.

A real FFmpeg process was denied a local-file URL at the remote-media boundary;
header injection cases are rejected before process creation. Existing media tests
that supplied local file paths as remote sources were adapted to serve their same
neutral fixtures over local HTTP. They still decode and validate the generated
audio/video; the security restriction is not disabled for tests.

The Ruff security-rule scan was manually triaged, in addition to the ordinary
lint/format and architecture checks. OSV's batch service rejected the diagnostic
request, so that route did not produce an audit result. A separate primary-source
check of [PyPI's version metadata API](https://docs.pypi.org/api/json/) returned
HTTP 200 and empty `vulnerabilities` lists for the installed versions of Telethon,
HTTPX, HTTPcore, asyncpg, Redis, cryptg, yt-dlp, curl-cffi,
bgutil-ytdlp-pot-provider, psutil and python-dotenv. This result covers those eleven
packages' published metadata on this date; it is not an exhaustive vulnerability
scan of transitive/native dependencies, FFmpeg, Telegram or the host OS.

## Deployment boundaries

Final workstation validation passed 1,841 full-suite cases with zero skips and
three subsequent new regressions, covering all 1,844 currently collected cases.
The full run retained ten existing curl-cffi Windows selector warnings. Ruff
lint/format passed for all 349 Python files; provider/application architecture,
the generated schema comparison and wheel packaging checks passed. All 27
security-rule findings were triaged (23 allowlisted SQL constructions, one
Telegram checksum, one empty-password sentinel, one bounded fallback exception
and one already-guarded XML parser). No runtime dependency was added or upgraded.

Upgrade/restart the actual bot to apply revision 13; the workstation changes do
not deploy themselves. Only Telegram-issued sender IDs and the configured bot
identity are trusted. Another party can copy the bot's public name or artwork;
application code cannot prevent that visual impersonation or protect a stolen
bot token/session from being used outside this process.

Provider URL guards, response caps and the restricted FFmpeg protocols do not
prove protection against every compromised upstream resolver, nested HTTP
playlist destination or DNS rebinding route. Production egress isolation and
private-network/firewall policy, credential/session permissions, backups and OS
patching were not inspected here. No claim of absolute security or live
penetration-test coverage follows from passing workstation regressions.
