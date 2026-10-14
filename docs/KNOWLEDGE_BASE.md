# Downloader knowledge base

Last updated: October 14, 2026. This is the shared record of verified findings,
failed approaches and decisions for all providers. Read it before investigating a
provider. Update it when new evidence changes a conclusion; preserve dated results
rather than rewriting historical failures as successes.

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

## Provider identity and isolation

| Provider | Stable identity | Do not use as the durable file identity |
| --- | --- | --- |
| YouTube | Validated 11-character video ID, with the selected codec/resolution/frame-rate quality key | `pp`, tracking parameters, temporary Googlevideo URLs or playlist position |
| Instagram | Requested shortcode/media ID; each carousel item has its own numeric ID; story identity includes its story ID | `dlrf`, signed CDN URL, album position without a stable item ID |
| SoundCloud | Origin numeric track ID with a supported full-stream quality | Mutable uploader/track slug, search-result title or a preview URL |
| Pinterest | Pin ID plus the provider's stable child/rendition identity | Tracking query, signed CDN URL or a short preview mistaken for the full pin video |
| XVideos | Origin canonical video identity, normalized by that provider across public/embed aliases | Title slug, old URL spelling or CDN URL |
| XNXX | Origin canonical video identity and stable quality key | Title slug or CDN URL |

The file key also includes the Telegram account. Provider sessions, account cookies,
headers, HLS discovery and format parsing must remain inside their owning provider's
six-file folder. Shared models and generic transfer/database/Telegram infrastructure
are allowed. Similar provider implementations must not be merged into a shared
concrete extractor in violation of `AGENTS.md`.

## YouTube

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

Upstream [PR 17805](https://github.com/yt-dlp/yt-dlp/pull/17805) changed default
clients on October 10. The [official nightly release](https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/tag/2026.10.10.233141)
records that change and the updated embedded Safari user agent. This is a specific
upstream fix, not a floating automatic upgrade.

Retain CDN verification before quality ranking. An inaccessible preferred rendition
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
