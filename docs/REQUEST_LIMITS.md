# Request limits and administrator exemption

October 17, 2026. Open `/admin` in private as `MAIN_ADMIN_ID`, then choose
**تنظیمات دانلود** (download settings). Changes persist in the bot's PostgreSQL
`app_settings` and affect the next admission without restarting Python or Redis.
Changing these installation settings remains a main-administrator operation.

## Independent controls

| Control | Range / initial value | Enable switch |
| --- | --- | --- |
| User requests per rolling minute | 1–1000; initially 1 | Initially on |
| Default group links per rolling minute | 1–1000; initially `GROUP_LINKS_PER_MINUTE`, default 5 | Initially on |
| Minimum user request gap | 1–3600 seconds; stored default 5 seconds | Initially off |
| Minimum group link gap | 1–3600 seconds; stored default 5 seconds | Initially off |

Each of the four switches is independent. Editing a number does not enable its
switch. Disable both the user quota and user gap to remove user rate restrictions;
the group controls remain independent. Disabling the group quota also disables
enforcement of existing per-group numeric overrides. Re-enabling it preserves
those overrides and their precedence over the global default. Gaps are optional:
a per-minute quota can be used on its own, or a gap can spread bursts while the
quota still limits the total. Missing settings retain the previous quota behavior.

User limits are shared across private chats and groups and apply to all providers.
Inspection and download selection have separate windows: opening a quality menu
does not prevent its first download. Each later format selection, including a
cached file or an old menu, is another download admission. New links also respect
the current download window/gap. A group quota/gap counts admitted links across
ordinary users; quality selections do not consume another group link slot.

## Administrator behavior

The numeric `MAIN_ADMIN_ID` and non-banned delegated **bot administrators** stored
in this bot's `bot_users.is_admin` are exempt from all four download rate controls.
Their requests consume neither user nor group rate slots, including in a group
whose ordinary-user quota is already full. The role is read from SQL on every
admission. Grants and revocations take effect on the next request; cached profiles
and group-admin/owner snapshots never grant this exemption. Existing ordinary-user
timestamps remain in place when a role is granted/revoked.

Rate exemption preserves sender/chat ownership of quality and cancellation
buttons, group active/send permissions, bans, maintenance policy, file/resource
safety bounds and Telegram's own FloodWait limits. An exempt link still registers
its user and `INSPECTING` conversation in the same transaction; a failed write
rolls the admission back.

## Persistence and diagnostics

| Setting key | Type |
| --- | --- |
| `user_links_per_minute`, `group_links_per_minute` | Integer, 1–1000 |
| `user_quota_enabled`, `group_quota_enabled` | Boolean |
| `user_gap_seconds`, `group_gap_seconds` | Integer, 1–3600 |
| `user_gap_enabled`, `group_gap_enabled` | Boolean |

Existing revision-13 timestamp arrays supply both the rolling counts and the
latest accepted time for gaps, including gaps longer than 60 seconds. No new
schema revision, Redis service, environment key or destructive reset is required.
Normal startup still applies any pending migrations from older installations.
Ordinary admissions retain at most the most recent 1000 timestamps per window,
even when quotas are disabled. Re-enabling a quota evaluates the retained recent
history immediately; turning a switch off does not erase recent usage.

PostgreSQL group-row and bot/user advisory locks serialize concurrent admissions
across repository instances. Rejections spend neither quota and do not create a
conversation. The error distinguishes a minute quota from a minimum gap, gives
remaining seconds and Tehran retry time, and uses the longer wait when both
controls for a subject apply. Group gap warnings share the existing bounded,
five-second warning coalescing. `/start` and the shared `/help` renderer display
the current switches and enabled gaps instead of advertising a disabled quota.

## Workstation verification

`tests/test_admin_request_limits.py` uses real dedicated PostgreSQL and installed
handler code. It covers main/delegated exemption, fresh grant/revoke/ban decisions,
bot isolation, group permission checks, atomic exempt registration rollback,
optional gaps longer than one minute, restart/cross-chat state, independent live
switches, audited root-only edits, the actual panel input flow and owner-bound
quality selections. Concurrent cases include 100 cross-instance group/user gap
attempts, repeated exempt admin requests, and 1005 ordinary admissions with both
quotas disabled, verifying bounded timestamp storage and live re-enablement.

`tests/test_download_settings.py` covers all new inputs/switches and live help/start
copy. Telegram publication in these tests is simulated; they do not represent
live Telegram or production-server throughput measurements.

Final workstation regression run on October 17: `python -m pytest -q` with
dedicated PostgreSQL/Redis passed **2504 tests**, with no skips or failures, in
182.08 seconds. Ten existing Windows curl-cffi selector-thread warnings remain.
Repository-wide Ruff checks, formatting of changed Python files, generated-schema
verification and `git diff --check` also passed.
