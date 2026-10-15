# YouTube and Instagram access investigation — October 13, 2026

The reported URLs normalize to YouTube `l6zflbTGNFQ` and Instagram
`DeR9eU8idi0`. Their tracking parameters do not affect media identity.
Both failures reproduced on this workstation before the changes. The bot runs
on a separate server that was not available for this investigation; workstation
results cannot establish that server's current session or network health.

## Upstream implementations compared

| Implementation | Mechanism | Finding / action |
| --- | --- | --- |
| [yt-dlp YouTube](https://github.com/yt-dlp/yt-dlp/tree/master/yt_dlp/extractor/youtube) | Innertube clients, signature/`n` challenge solvers, scoped cookies and PO-token plugins | Retained the current stable release and matching EJS package; downloading metadata alone is insufficient to verify CDN access. |
| [pytubefix Innertube](https://github.com/JuanBindez/pytubefix/blob/main/pytubefix/innertube.py) | Client-specific player requests, including `VISIONOS`, OAuth and PO-token options | Compared current client contexts; changing clients is not a reliable cure for an origin login challenge. No interactive OAuth or automatic browser-cookie export was introduced. |
| [bgutil PO-token provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) | Separate HTTP server or script generates tokens; Python plugin supplies them to yt-dlp | Installing the Python plugin does not start its server. PO tokens do not guarantee acceptance of a challenged network. |
| [yt-dlp Instagram](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/instagram.py) | Account media API, browser impersonation, session bootstrap, Relay root query and page data | Retained the independent browser transport and account/session isolation. An optional API's login failure must not prevent another working path. |
| [Instaloader](https://github.com/instaloader/instaloader/blob/master/instaloader/instaloadercontext.py) | Shortcode GraphQL POST with document ID, variables, CSRF and session cookies | Shortcode POST now uses this minimal envelope instead of assigning an unrelated Relay friendly name. |
| [gallery-dl Instagram](https://github.com/mikf/gallery-dl/blob/master/gallery_dl/extractor/instagram.py) | REST/GraphQL alternatives, cookie session and updated CSRF state | Configured cookies now seed the provider's domain-scoped jar, allowing `Set-Cookie` rotation to take effect. |

The [YouTube PO-token guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
and [EJS guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS) describe separate
requirements: player access, token-dependent stream access and JavaScript
challenges. The [Instaloader shortcode-query issue](https://github.com/instaloader/instaloader/issues/2738)
also documents partial `execution error` responses for the current document ID;
an empty response does not establish that a reel is private or deleted.

## Reproduced origin behavior

- YouTube's default, `web_embedded`, `visionos`, `tv_downgraded` and `mweb`
  attempts returned `youtube_login_required` for the reported video here.
  A separate pytubefix `ANDROID_VR` extraction returned `BotDetection`.
- Instagram's root query returned null media; its current shortcode query,
  including Instaloader's minimal POST envelope, returned `execution error`.
  The older `8845758582119845` document did not provide a working alternative.
- The workstation's existing local HTTP proxy also failed both inputs. It was
  not saved as a production setting.
- No YouTube/Instagram cookies, provider proxy or PO-token server were configured
  in the local bot environment. Node and the pinned extraction packages exist.
- After the corrections, the strict local live check still failed visibly with
  `youtube_login_required` and `instagram_graphql_unavailable`. These are
  remaining access failures, not successful downloads.
- Previous independent-runner checks consumed this exact Instagram reel to EOF
  at 720×1280 and 1080×1920, with audio; see
  [provider verification](PROVIDER_VERIFICATION.md). These historical results
  do not count as a successful download on today's bot server.

## Corrections

YouTube now verifies complete playable choices inside its bounded player-attempt
sequence. If metadata succeeds but all CDN sources fail, a guest with default
settings can try `web_embedded`. Inspection and selected-quality resolution both
use this sequence. Explicit client settings and authenticated sessions remain
authoritative; HTTP 429 stops failover. Account locks also cover verification.

Instagram accepts rotated cookies and CSRF tokens in its own jar. Optional
HTTP 401, login JSON or a page login redirect can fall through to the current
shortcode query or embed. If all alternatives fail, it returns a safe
`instagram_login_required` or `instagram_graphql_unavailable` diagnostic instead
of hiding the evidence behind an empty-media message. JSON rate-limit errors,
including GraphQL code `1675004`, stop further requests and enter cooldown.

All provider logic stays in its owning folder. CDN requests do not receive the
Instagram account's cookies or CSRF headers. No third-party download service or
substitute media is used.

## Run on the bot host

Use the same environment and Python executable as the running bot:

```bash
python tools/diagnose_video_access.py
python tools/diagnose_video_access.py --live
```

The first command reports package/runtime health and whether credentials,
cookie-file access, proxies and the configured helper are present, without
printing their values or paths. The second consumes real origin streams to EOF
and writes `.runtime/video-access.json`. It exits nonzero when either input
cannot be downloaded completely. Neither command sends Telegram messages.
The diagnostic uses the environment's default accounts; it does not query
`site_accounts` in the production database. To test a database account, provide
that account's credentials locally through the corresponding default environment
variable for the diagnostic process, without publishing their values.

For YouTube, install the pinned project dependencies and Node >=22 (or a
supported Deno runtime), then configure an authorized writable Netscape file
with `YOUTUBE_COOKIES_FILE` if that server receives a login challenge. If stream
requests need PO tokens, start the matching helper separately:

```bash
docker run --name youtube-pot -d --init --restart unless-stopped \
  -p 127.0.0.1:4416:4416 brainicism/bgutil-ytdlp-pot-provider:2.0.2
```

Set `YOUTUBE_POT_BASE_URL=http://127.0.0.1:4416` for a bot running directly on the
host; leave `YOUTUBE_PLAYER_CLIENTS` empty to select `mweb,web_safari` with this
helper. A containerized bot needs a helper address reachable from its own
container. A healthy `/ping` checks availability, not successful token generation
or acceptance of the video.

For Instagram, set `INSTAGRAM_COOKIE` to a one-line Cookie header from an
authorized session when needed. If the bot's egress is rejected, configure an
accepted HTTP connection using `INSTAGRAM_PROXY` / `YOUTUBE_PROXY`. Extraction,
sampling and transfer must use the same provider connection. Changing a public
API key or repeatedly retrying the same rejected IP does not repair a session.

Restart the bot after changing its environment and rerun the live check. Do not
share cookie files, raw extractor stderr or proxy credentials in a diagnostic
report. Successful deployment and downloads on the separate production server
remain unverified until this check passes there.

Validation: **756 tests passed, 5 database integration tests skipped** locally;
lint and formatting passed. After the final CDN cooldown adjustment, the
affected provider, diagnostics and architecture checks passed **161 tests**.
The separate production host was not contacted or modified.

Fresh independent checks at code commit `4786edb`:

- [Linux code/database CI](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38449760618)
  passed, including lint and formatting.
- [Instagram full-stream check](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38449807417)
  passed all **8 offered qualities** of `DeR9eU8idi0`, including 720×1280 and
  1080×1920 with audio. Every stream reached EOF and passed completeness checks;
  durations were 11.7–11.815 seconds and sizes 359,898–2,232,676 bytes.
- [YouTube full-stream check](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38449820733)
  failed with `youtube_login_required` for `l6zflbTGNFQ`, before any stream could
  be consumed. The runner had no production account cookies. This is recorded
  as a remaining failure.

These checks sent no Telegram messages. The later diagnostic scope clarification
does not change either provider's extraction or transfer behavior.

## October 14 follow-up

The same two reported URLs were tested again on the workstation. After installing
the newly required `curl-cffi` dependency, the strict full-download attempt still
failed: YouTube exceeded its extraction deadline and Instagram timed out. No
complete file was received. Chrome impersonation plus IPv4 also timed out for
YouTube. Pytubefix `11.2.0` with `WEB` automatic-token and `ANDROID_VR` paths both
stopped with `URLError`. The available local HTTP route did not recover either
input and was not saved as a production setting.

Current upstream research identified two concrete changes beyond the earlier
investigation:

- The [October 10 official yt-dlp nightly](https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/tag/2026.10.10.233141)
  includes the new default `tv_samsung` client from [PR 17805](https://github.com/yt-dlp/yt-dlp/pull/17805).
  The project now pins `2026.10.10.233141.dev0`, keeping EJS `0.8.0`. It is an exact
  reviewed upstream release, not a floating nightly update. Explicit player/token
  settings remain authoritative; blank overrides use the new default client set.
- [Current Instagrapi media extraction](https://github.com/subzeroid/instagrapi/blob/master/instagrapi/mixins/media.py)
  uses document `27830990013244856`, `PolarisPostRootQuery`, current relay variables
  and available LSD/DTSG state. The independent Instagram client now tries this
  after the root query and before its legacy shortcode/embed paths. The optional
  `INSTAGRAM_WEB_DOC_ID` overrides this document. Secrets stay in the owning
  provider session and never reach CDN requests. Fixture tests reproduce recovery
  of the reported shortcode when the root is null and the media-info API rejects
  guest access; they are not live-origin proof.

Both providers now reserve time for account alternatives: at most three account
attempts, 25 seconds per attempt when alternatives exist, inside the existing
85-second inspection bound. A stalled guest/account is cancelled before the next
account starts. Identical authenticated default YouTube clients are not retried
under the `guest` label. Instagram deadline failures become safe provider errors
instead of raw timeouts bypassing account recovery. Failed/empty session bootstraps
are coalesced for ten seconds, then retried after possible recovery.

The updated exact-link workstation check still failed visibly with
`youtube_extraction_timeout` and `instagram_extraction_timeout`. No cookies,
provider proxy or token helper were configured locally. This investigation has
not established successful downloads on this workstation or the separate bot host.
The supplied production `metrics_interval` samples show two failed inspections;
their network counters cover the host, including unrelated processes/containers,
and do not identify an extractor's underlying error or measure bot media downloads.

An authorized Instagram REST media-info call now precedes homepage bootstrap:
a blocked page cannot exhaust the account's budget before a working authorized
API is contacted. Bounded account selections rotate fairly and retain the default
YouTube fallback with larger account pools.

At code commit `9a8cd23`, [Linux CI](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38603497634)
passed **769 tests** with PostgreSQL/Redis, lint and formatting. The fresh
[Instagram full-stream check](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38603495934)
passed all **eight qualities** of the exact reported URL, including tracking
parameters, video and audio, and complete 11.7–11.815 second durations. The fresh
[YouTube full-stream check](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38603501343)
still failed with `youtube_login_required` for the exact reported URL before any
download. These checks used the new pinned extractor; the YouTube upgrade alone
did not resolve the origin's access challenge. No chat messages were sent.

The [knowledge base](KNOWLEDGE_BASE.md) preserves methods, failures, verified findings
and retry conditions across all six providers. The remaining production-access
requirement is not hidden by a passing code suite or the Instagram runner result.

Final local validation after the authorized-API ordering and account-pool corrections:
**767 tests passed, 5 database integration tests skipped**; lint, formatting and
provider architecture checks passed. Missing test database configuration explains
the local skips; Linux CI supplies dedicated PostgreSQL/Redis services.

Final [Linux verification at `d042689`](https://github.com/mahdiashtian/SaintCrispy/actions/runs/38605040464)
passed **772 tests** with real PostgreSQL/Redis, lint and formatting. This closes
code verification for the additional ordering/rotation fixes; it is not a claim of
production access or a successful local download of either reported video.

The later request-rate investigation did not establish new origin access.
Workstation Ubuntu WSL probes of both exact identities failed before an HTTP
response with `URLError` / underlying `OSError` errno 101 (`ENETUNREACH`), with no
certificate verification error. This must not be mislabeled as proof that the
production host has the same route failure or that YouTube accepted a session.
Local authorized cookie/proxy/helper settings remained absent. The operator's
publication condition initially held the request-rate changes. The later explicit
reference-layout request separately authorizes their publication with the
structural update; the failed-access and cold-throughput findings remain open. See
[the request throughput report](REQUEST_THROUGHPUT.md) for separate successful
cached-path measurements and failed distinct-cold runs. No remote server was
accessed or updated during those local tests.

## October 15 deployment evidence and diagnostic corrections

The operator supplied a fresh report from the Linux production virtual environment:
Python 3.14.4, yt-dlp 2026.8.19, EJS 0.8.0, plugin 2.0.2 and curl-cffi 0.16.3.
The selected Node command was unavailable to that process. Default environment
cookies, provider proxies and a PO-token helper were absent. Database accounts
were not inspected. This establishes deployment drift from the repository's
2026.10.10.233141.dev0 extractor pin and an unavailable JavaScript runtime; it does
not prove that installing them alone will clear YouTube's login challenge.

The current official nightly release and Instagrapi media document were checked
again: the reviewed release and document already match this repository. Another
unverified document ID or client name was not added as a speculative cure.

The workstation's fresh full-stream check failed before either inspection completed:
`youtube_extraction_timeout` and `instagram_extraction_timeout`, with zero full
streams. A separate bounded HTTP check received GitHub HTTP 200 in 1.094 seconds
but no YouTube/Instagram HTTP response. The new network diagnostic localized the
YouTube failure to TCP connect (12.035 seconds); Instagram's browser transport
reported libcurl timeout code 28 (10.019 seconds), without identifying its inner
TCP/TLS stage. These are workstation results, distinct from production's login
and GraphQL responses. Node 22.23.2 and the pinned packages work on the workstation;
there were no default sessions, proxies or helper. No Telegram messages were sent.

Corrected two misleading diagnostic paths. A fatal YouTube network error now wins
over an unrelated earlier sign-in/JavaScript warning, and explicit HTTP 429 wins
over incidental sign-in text. Instagram retains HTTP 403 instead of discarding it
as empty media, logs the fixed operation name/status, and distinguishes schema,
session and execution GraphQL categories without storing upstream text. An unknown
GraphQL error no longer tells users that network rejection is proven. Optional
paths can still recover, and throttling stops alternatives as before.

`python tools/diagnose_video_access.py` now compares installed package versions to
the checkout's pins and reports an unavailable runtime under `deployment_issues`.
Add `--network` to probe headers only, independently per provider, using their
configured routes. This sends no account cookies, follows no login redirects,
consumes no media body, and does not establish successful extraction. Exception
identities and integer errno/libcurl codes are retained; messages, IPs, signed URLs,
proxy credentials and cookie values are not emitted. These explicit probes never
run per user request.

On the production host, stop the old Python coordinator before the upgrade:

```bash
cd ~/SaintCrispy
git pull --ff-only
. .venv/bin/activate
python -m pip install --upgrade -e .
command -v node
node --version
python tools/diagnose_video_access.py --network
```

If Node was installed through a version manager, activate it in the same shell or
supervisor environment as Python; configure `YOUTUBE_JS_RUNTIME=node:/absolute/path/to/node`
when that supervisor has a different PATH. Verify the reported runtime is available.
After deployment checks, use `--live` once to establish full origin access, then
restart with `python main.py`. If a current installation still receives a login
challenge, test an authorized session and accepted provider route. Installing Node
or reinstalling packages does not create those credentials. Instagram has no Node
requirement; its current session/query/route rejection needs its own evidence.
Do not retry the same rejected guest route in a loop or publish secrets.

### Operator's production result after reinstall

The next supplied report confirms that yt-dlp now matches the nightly pin. Node
remains unavailable (`node: command not found`); no default credentials, proxy or
helper were configured. Header-only production probes returned YouTube HTTP 200
in 0.101 seconds and Instagram HTTP 200 in 0.241 seconds. The operator's subsequent
bot messages still report `youtube_login_required` and `instagram_http_forbidden`.
The agent did not SSH into or modify that host.

Production can reach both public pages. This is distinct from the workstation's
TCP/browser timeouts and does not establish access to player APIs, GraphQL or CDN
media. The package mismatch is resolved; do not repeatedly reinstall unchanged
dependencies as the remedy. Restore a supported Node executable for the actual
Python/supervisor environment, then run the same-host `--live` diagnostic once.
Its safe `instagram_origin_response.operation` and `instagram_query_failed` logs
identify which extraction methods were rejected. A 403 is evidence of HTTP refusal,
not proof of its policy reason or proof that a cookie will always fix it. Authorized
session/accepted-route validation remains separate from JavaScript installation.

### Production full-stream check after Node installation

The operator's next `--live` report, timestamped October 15 at 07:49 UTC, shows
Node 24.21.0 available, current pinned packages and no deployment issues. Both
YouTube attempts returned `youtube_login_required`. Instagram's `web_graphql`
returned a GraphQL rejection; `shortcode_graphql` returned HTTP 403 in 0.146 seconds.
Neither provider finished inspection or resolved a source; zero complete streams
and zero Telegram messages were verified. Environment-default cookies/proxies/helper
remain absent, and database accounts were not checked. Missing Node and old Python
packages are no longer an explanation for this result. The shell's OS restart
notice does not explain an origin's structured login/403 responses.

Before the next origin test, change one evidenced condition: use an authorized
session, or verify an accepted provider route. Do not retest identical guest
conditions in a loop. A cookie is a testable recovery path, not a guaranteed cure
for every query/IP restriction.

For an authorized-session test on this host:

1. Export the operator's own YouTube session as a Netscape cookie file following
   [yt-dlp's official export instructions](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).
   Store it privately under the ignored `.runtime/credentials/` directory, writable
   by the bot user and readable only by that user (mode 600). Do not paste it into
   chat, source, logs or issue reports.
2. In the local `.env`, set `YOUTUBE_COOKIES_FILE` to that file's absolute server
   path. Set `INSTAGRAM_COOKIE` to the complete one-line Cookie header from the
   operator's authorized Instagram browser session. This is a header value, not
   the `Cookie:` prefix, a username/password, or a cookie-file path. The account
   must be able to view the requested post. Keep the two providers' sessions separate.
3. Run the non-live diagnostic once to confirm `cookies.readable/netscape/writable`
   for YouTube and `cookies_configured` for Instagram. This confirms configuration,
   not acceptance of the sessions. Run `--live` after this actual session change.
4. After a successful full-origin check, stop the old coordinator and start
   `python main.py` in the updated environment; an existing process retains its
   old configuration. If origin access is still refused, retain safe method/error
   evidence and validate the session and configured egress rather than retrying
   unbounded guest clients or claiming delivery success.

The diagnostic now loads `.env` exactly like the bot and audit child: explicit
file overrides inherited shell values, UTF-8 BOM is accepted and interpolation is
disabled. This fixes misleading configuration reporting; it cannot authenticate
a rejected origin request by itself.

### Fresh workstation test requested by the operator

At 07:53 UTC (11:23 Asia/Tehran) on October 15, one fresh full-stream audit tested
the exact two reported identities on this Windows workstation. Node 22.23.2,
Python 3.13.14 and the pinned packages were available; no default cookies, provider
proxy or helper were configured. The test used direct provider connections, a
180-second case limit and the diagnostic's normal 64 MiB per-stream cap. It made
no Telegram requests and retained no complete media on disk.

| Input | Inspection | Complete streams | Result |
| --- | --- | ---: | --- |
| YouTube `l6zflbTGNFQ` | Failed before source resolution | 0 | `youtube_extraction_timeout` |
| Instagram `DeR9eU8idi0` | Failed before source resolution | 0 | `instagram_extraction_timeout` |

Instagram's bootstrap, page, ruling, root GraphQL and web GraphQL requests logged
connection failures around ten seconds each, without HTTP responses. Neither
provider reached transfer/completeness verification. The private sanitized report
is `.runtime/workstation-20261015-direct.json`. These fresh failures are workstation
results; the production host was not used or changed. No alternative was claimed
successful and no new extraction fallback was added without live evidence.

## Anonymous GitHub library and transport comparison (October 15)

The operator subsequently required **guest access only**. This investigation used
no account cookies, OAuth, browser profile, Telegram account or production server.
Older authorized-session instructions above describe a different deployment option;
they are not the solution requested for this investigation.

Compared the existing pinned yt-dlp with [PyTubeFix](https://github.com/JuanBindez/pytubefix)
11.2.0 and the Instagram provider with [Instaloader](https://github.com/instaloader/instaloader)
4.15.4. Alternative libraries were installed under an ignored isolated research
directory, not into the application's runtime dependencies. Reviewed their current
[Innertube clients](https://github.com/JuanBindez/pytubefix/blob/main/pytubefix/innertube.py)
and [shortcode request implementation](https://github.com/instaloader/instaloader/blob/master/instaloader/instaloadercontext.py).
Also compared protocol envelopes from current gallery-dl, Instagrapi and
[Cobalt's Instagram implementation](https://github.com/imputnet/cobalt/blob/main/api/src/processing/services/instagram.js).
No third-party source was vendored or relicensed. Failed alternatives were not
added as automatic retries or advertised as working replacements.

### Connection findings

On this Windows workstation, system DNS returned non-global addresses for
`www.instagram.com`, `i.instagram.com` and `youtubei.googleapis.com`. HTTPS JSON DNS
answers from independent public resolvers contained global addresses. A corrected
DNS answer changes name resolution, not the workstation's egress IP or account.
No Windows network setting was edited and no origin IP was hardcoded into source.

Instagram TCP/TLS requests still timed out with public DNS. HTTP/3 with public DNS
returned HTTP 200 and approximately 590 KB of generic Instagram HTML in about two
seconds. It did not return the reel's media. A later request to the exact reel URL
returned the same generic page without an HTTP redirect. Six current JavaScript
entry assets were retrieved successfully; the loaded entries did not provide a
replacement post query or media. This does not prove that all dynamically loaded
Instagram code was inspected, or that the reel is unavailable elsewhere.

### Exact-input results

| Method | YouTube `l6zflbTGNFQ` | Instagram `DeR9eU8idi0` |
| --- | --- | --- |
| yt-dlp API with public DNS | Extracted 65 formats, advertised 1,301 s; accepted sources consumed fully below | Not substituted for the provider |
| PyTubeFix `ANDROID_VR` with public DNS | Extracted 25 streams; native `.download()` returned HTTP 403; HTTP Range header probes returned 206/MP4 | Not applicable |
| Subsequent fresh yt-dlp and PyTubeFix extraction | Login/bot challenge returned, including in the native worker | Not applicable |
| PyTubeFix `VISION_OS` / `TV_EMBED` | Bot detection / connection timeout | Not applicable |
| Native project queries through HTTP/3/public DNS | Not applicable | Root returned no media; current web/shortcode queries failed or were refused |
| Instaloader native TCP | Not applicable | Read timeout |
| Instaloader request through isolated HTTP/3 adapter | Not applicable | `BadResponseException`; JSON contained `execution error`, no media |
| Fresh post LSD; browser-profile-matching User-Agent | Not applicable | Did not recover GraphQL; query failure/403 persisted |
| Guest mobile REST / web REST | Not applicable | Mobile returned 404; web returned HTML, not media JSON |
| Public post/reel embeds | Not applicable | HTTP 200, no usable video/media node |
| Current action Relay envelope with real anonymous bootstrap fields | Not applicable | HTTP 401, no media |

The final Relay comparison used an actual envelope and document from the reviewed
implementation, including its action name and anonymous page bootstrap, rather
than blindly substituting document IDs. Its failure is not a reason to repeat it
under unchanged conditions. Likewise, an anonymous CSRF/bootstrap token is not an
account login or proof of origin acceptance.

### Complete YouTube transfer evidence

The project selection, byte streaming and MP4 duration validator consumed these
two accepted sources to EOF. No Telegram registration/publication was attempted
and no complete media was retained on disk.

| Selection | Received bytes | Measured media duration | Initial corrected-DNS transfer | Production DNS-tunnel transfer |
| --- | ---: | ---: | ---: | ---: |
| `video_360p30_h264_mp4` | 58,521,501 | 1,300.50 s | 54.805 s | 53.485 s |
| `audio_140-2_fa_aac` | 21,048,861 | 1,300.55 s | 23.045 s | 34.960 s |

Both runs produced identical SHA-256 hashes per source and passed the project's
completeness checks against the advertised duration. The second run reused the
already accepted metadata and tested the production tunnel separately. **It does
not establish successful fresh extraction.** Private signed metadata and detailed
reports remain under ignored `.runtime/`; no signed URLs or credentials are
included here.

### Changes supported by this evidence

- Added optional provider-owned Instagram HTTP/3/native DoH settings; default
  transport remains unchanged. This fixes the observed connectivity stage only.
- Added optional YouTube JSON DoH resolution. Metadata uses a synchronous resolver
  only inside its existing bounded async subprocess, never in the bot event loop.
  Sampling and transfer use an async loopback CONNECT tunnel with the same resolver.
  TLS remains end-to-end with hostname/certificate verification intact.
- The tunnel accepts only the provider's permitted hosts on port 443, rejects
  non-global DNS answers, bounds connections/lookups/cache/response bodies, coalesces
  duplicate DNS lookups, cools down failures and closes with application resources.
  Its ephemeral loopback port is internal, unrelated to PostgreSQL/Redis ports.
  An explicit YouTube proxy takes precedence over the optional DNS profile.
- Preserved the existing independent providers, cooldowns, process cancellation,
  metadata size limits and transfer validation. No new extractor library, account
  request, unconditional retry or guessed document fallback was introduced.

A prototype resolver had a Python built-in shadowing error; a regression test
identified it and the shared DNS-answer parser was corrected before the final
checks. The subsequent native extraction still received the origin challenge.
That repaired implementation bug must not be reported as the cause of Instagram's
refusal or proof that YouTube's player challenge is fixed.

Instagram guest extraction remains unresolved on this workstation. Fresh YouTube
extraction is intermittent/challenged despite the real complete-transfer success.
Revisit rejected methods after a documented upstream query/client change, a
verified connection change, cooldown expiry with origin recovery evidence, or
another concrete new condition. Do not request account cookies for this guest-only
task. See [operations](OPERATIONS.md#optional-guest-network-profiles) for opt-in
configuration and a bounded diagnostic, not a universal access guarantee.

Final relevant workstation checks: **281 passed, 8 skipped**. The skipped reference
integration cases require dedicated PostgreSQL/Redis test URLs, which were not
configured for this provider/network change. Provider boundaries, architecture,
nonblocking rules, diagnostics, startup, audit behavior, worker cancellation and
DNS transport tests passed. Ruff lint/format checks and `git diff --check` passed.
The DNS tests cover coalescing/cancelled followers, negative caching, private-address
and host/port refusal, oversized responses, subprocess resolver restoration and
opaque tunnel shutdown. No load claim about 1,000 successful origin extractions
per second follows from these checks; origin throttling remains independent of
application admission/concurrency limits.

## Later October 15: guest browser diagnosis and working Instagram fallback

A fresh isolated Playwright/Edge context imported no browser profile, account or
cookies. With public DNS and HTTP/3, the exact reel rendered an owner-set content
restriction for viewers under 15, with no video element. The browser's live
requests used experiment/promotion queries, rather than exposing an alternative
working media query. The same fresh-browser test on the exact YouTube URL returned
`LOGIN_REQUIRED` / the bot-confirmation message, without playable formats. This
distinguishes origin responses from a Python parsing failure; it does not establish
the viewer's actual age or prove the media cannot be obtained on another route.

Compared the anonymous service flow in [Inoue's SDK](https://github.com/Inoue-AI/Inoue-AI-Content-Downloader-SDK)
and [pybalt](https://github.com/nichind/pybalt). sssinstagram timed out, SnapSave's
current homepage offered Facebook rather than the reviewed old Instagram flow,
and SnapInsta remained behind a security-verification page in the fresh browser.
No challenge was solved, no third-party API key was used and no failed service was
added to a retry loop. The Cobalt registry hostname did not resolve here.

The public pybalt-compatible endpoint `https://dwnld.nichind.dev/` accepted an
anonymous Instagram request and returned a direct **original Instagram CDN** URL
whose filename identified `DeR9eU8idi0`. The regular CDN TCP route, including
corrected DNS, timed out. HTTP/3/DoH succeeded:

| Verification | Result |
| --- | --- |
| Complete bytes, consumed to EOF with required audio | 1,468,542 |
| Measured duration against rounded 12 s | 11.700 s |
| SHA-256 | `6f7cac9c0533c86e8d1fdfe3439bee9bca49d33682c0d5cd1614b0fa1dfbc150` |
| Identity cross-check | Exact byte hash matches the October 14 independent same-reel result |
| First isolated HTTP/3 full transfer | 2.373 s |
| Final native provider inspect/resolve/full verification | Passed, 59.417 s total; 2.698 s transfer |

The final native run cleared Instagram cookies/proxy/transport settings in its
own subprocess and used normal project composition choices: native inspection,
bounded public resolver fallback, original-CDN probing, selected-quality resolution
and full project integrity validation. It used the new bounded curl transport.
It published no Telegram message and retained no complete media on disk.

The fallback is provider-owned and only runs for short canonical public post/reel
identities in guest mode after native failure. Native HTTP 429 stops alternatives.
It never receives account cookies, stories, tracking parameters or private share
tokens. Returned URLs must be HTTPS on Instagram's CDN, with a matching shortcode
filename. A complete bounded MP4 header must establish dimensions, duration and
audio before exposing a choice. Arbitrary proxy/tunnel URLs, wrong identities,
picker/carousel responses and incomplete/silent headers are rejected. This path
currently exposes the one complete MP4 returned by the resolver; it does not
invent the full native DASH/rendition catalog.

The accepted resolver snapshot is retained briefly so the first callback does not
repeat the rejected native extraction. Every selection still checks the CDN;
invalid/expired sources refresh once under the existing bounded cache. A regression
test resolves 1,000 concurrent choices from that snapshot with one resolver call.
This is coalescing evidence, not a claim that the external service accepts 1,000
distinct extraction requests per second.

CDN transport recovery carries its HTTP/3/DoH profile into generic size probing and
full progressive transfer. No Instagram account jar is copied into that transfer
session. Curl now pauses receipt at a 256 KiB Python body-buffer window, replays
paused bytes once, and cancels early-close/pre-header requests without waiting for
the entire file. Local real-HTTP tests verify slow-consumer bounds, complete byte
hashes across handle reuse and cancellation. Native libcurl/QUIC/OS buffers remain
additional memory, independent of the Python window. See the upstream
[pause contract](https://curl.se/libcurl/c/curl_easy_pause.html).

Fresh YouTube extraction is still under investigation. Additional changed-method
checks (TV Samsung without web/config bootstrap; HTTP/3/browser transport; the
public resolver's audio/auto modes) did not recover it. Invidious's listed API-enabled
instance returned HTTP 500 without formats. Three documented Piped API instances
returned 502, 500/no streams, or a redirect without JSON. These failed methods were
not added as production fallbacks. The earlier complete YouTube transfers remain
valid dated evidence, not proof that fresh extraction now works.

Validation of this Instagram/transport batch: the complete workstation suite passed
with **858 passed, 18 skipped** (external/dedicated-service opt-in cases were not
configured). Ruff lint/format checks and diff whitespace checks passed. All six
provider boundaries remain intact; no provider implementation was shared. The
live success above is original-CDN full consumption, not Telegram delivery.
