# Downloader knowledge base

Last updated: October 15, 2026. This is the shared record of verified findings,
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

Latest October 15 result: both reported identities passed fresh anonymous native
provider inspection/resolution and complete project transfer validation. Instagram
used its public resolver and original CDN; YouTube used a separately implemented
public converter fallback. See the final dated section of the access investigation
for byte counts, durations, hashes, dependencies and the untested Telegram boundary.

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
| Pinterest | Pin ID plus the provider's stable child/rendition identity | Tracking query, signed CDN URL or a short preview mistaken for the full pin video |
| XVideos | Origin canonical video identity, normalized by that provider across public/embed aliases | Title slug, old URL spelling or CDN URL |
| XNXX | Origin canonical video identity and stable quality key | Title slug or CDN URL |

The file key also includes the Telegram account. Provider sessions, account cookies,
headers, HLS discovery and format parsing must remain inside their owning provider's
six-file folder. Shared models and generic transfer/database/Telegram infrastructure
are allowed. Similar provider implementations must not be merged into a shared
concrete extractor in violation of `AGENTS.md`.

## YouTube

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
