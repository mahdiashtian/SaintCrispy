# Spotify tracks: matched audio and workstation verification

October 16, 2026. This provider accepts individual public Spotify **track** URLs.
October 17 input follow-up adds `spotify:track:` URIs and legacy track widgets;
they resolve to the same canonical track. The fresh workstation metadata check
failed on an error-shaped Spotify page, so it is not additional live download
evidence. See [exact observations and retry conditions](URL_VARIANTS_20261017.md#fresh-anonymous-workstation-evidence).
It does not decrypt or download Spotify's protected original audio. Metadata comes
from the public Spotify embed page; audio comes from a checked YouTube match and
a public MP3 converter. Menus and captions identify it as matched audio.

## Implementation choice and research

| Examined implementation or service | Observation and decision |
| --- | --- |
| [spotDL](https://github.com/spotDL/spotify-downloader/tree/cd4a4203f5b12bd6dbbdf22d7674807858d35e05) | Its downloader locates Spotify songs on YouTube. Studied the bounded result search and recording metadata approach; did not add its synchronous application/dependency stack. |
| [Sunnify](https://github.com/sunnypatell/sunnify-spotify-downloader/blob/1c54763a7615264aa77c4dca9376359df6339e5a/spotifydown_api.py) | Its public embed parser exposes track identity, artists and duration without developer credentials. Studied this route and its deprecated-API findings; did not add the desktop GUI. |
| [Spotify Get Track](https://developer.spotify.com/documentation/web-api/reference/get-track) | Metadata and preview fields are not a full-song download endpoint. A preview must never be advertised as the complete track. |
| SpotifyDown-style API | The old API hostname failed to connect here. Its website returned Spotify web-player content; this did not establish a usable downloader. |
| Spotdown public snippets | The documented `song-details` path returned HTTP 404. Current website assets contain a Turnstile flow; old snippets were not shipped as working code. |
| Fabdl public endpoint | HTTP 403, `invalid origin`, both without an Origin header and after explicitly trying its own origin. No further unchanged retry was made. |
| SpotMate | Its current client requires Turnstile before metadata lookup; a simple old CSRF-only snippet was insufficient evidence. No browser/account automation was added. |
| Spowload / Song.link | The former exceeded the three-redirect diagnostic budget; the latter returned HTTP 401 without credentials. Neither was selected. |

The repositories were read and their underlying network approaches were tested;
the spotDL and Sunnify applications themselves were not installed or certified.
This implementation is independent Python code using the project's existing
HTTPX, curl-cffi, yt-dlp and FFmpeg dependencies. It imports no other provider.

A native yt-dlp search succeeded, as did native M4A URL extraction for a matching
video. Consuming that native CDN URL failed on this workstation: direct HTTPX
could not connect, and a separate libcurl/public-DoH test timed out resolving it.
The public MP3 conversion route returned an accessible stream and passed full
consumption. This egress-specific comparison motivated the default; it is not a
universal ranking of all downloader services or a production-server diagnosis.

## Identity, formats and completeness

- `open.spotify.com/track/<22-character base62 ID>` is the canonical identity.
  Supported locale/embed wrappers and tracking parameters collapse to that ID.
  `spotify.link`/`spoti.fi` redirects are bounded and must stay within Spotify's
  explicit host/path rules. Lookalikes, credentials, private destinations and
  unsupported album/playlist/episode/artist paths are rejected.
- The embed entity's type, ID and Spotify URI must all match. A title, real artist
  names and positive full duration are required. `audioPreview` is ignored.
- Inspection is one bounded Spotify metadata lookup and presents one explicit
  output choice: matched MP3 128 kbps. It does not imply that Spotify offers MP3.
  Searching/conversion runs only after selection; a missing valid match returns a
  clear error rather than substituting a random result.
- Search first reads at most ten YouTube Music song-catalog entries, preserving
  typed artist metadata and displayed duration. An unavailable or unmatched catalog
  falls back to the existing two bounded yt-dlp video queries (at most thirty
  candidates overall). A first video no-match permits one artist/title-only query.
  All three paths share the same search deadline; HTTP 429 stops all search paths.
  The title and primary artist must
  match, duration must be within **two seconds**, and differing live/remix/cover/
  instrumental/acoustic/slowed/sped/mashup/edit/remaster markers are rejected.
  Official artist/topic results are preferred. These checks reduce mismatches;
  they do not establish bit-identical Spotify audio or perfect catalog coverage.
- The converter's filename must identify MP3; its HTTPS CDN host and `vid` query
  must match the selected video. A capped header probe rejects non-audio bodies.
  Actual transfer still runs through the existing full-stream/duration validation;
  a valid header or URL alone never proves a complete download.
- No lossless/original/320-kbps claims or synthetic quality variants are shown.
  Transcoding a source to a larger bitrate would not recover missing fidelity.

## Runtime and configuration

Each of the six provider files owns its URL rules, parsing, extraction and handler.
The metadata HTTP session and anonymous converter browser are owned by Spotify;
YouTube/other provider cookies, credentials, runtime settings and sessions are not
reused. Only generic models, locks, Telegram handling and transfer infrastructure
remain shared. Direct YouTube links are also available to everyone as of October 17.

```dotenv
SPOTIFY_METADATA_TIMEOUT_SECONDS=8
SPOTIFY_SEARCH_TIMEOUT_SECONDS=20
SPOTIFY_PROXY=
SPOTIFY_RESOLVER_URL=https://cnv.cx
```

These defaults work without a Spotify account/API key. An empty resolver disables
audio resolution. The optional proxy is Spotify-scoped and applies consistently
to metadata, search, conversion and CDN delivery. No Node runtime is needed for
Spotify's flat search; existing YouTube challenge requirements remain separate.

Metadata is cached for 300 seconds and selected sources for 45 seconds, in bounded
128-entry caches. Equal-track requests share one inspection/resolution; failures
share a two-second cooldown, and HTTP 429 stops the affected upstream for 60 seconds.
Only two selected-song resolutions/search processes run concurrently. All queue,
request and process waits are bounded. Cancellation and oversized subprocess
output terminate/reap the worker. No full song is buffered in provider memory.

The normal sender/chat-owned quality buttons, shared group quota, per-user limit,
single-flight transfer, bot-scoped SQL file index and per-user receipts apply.
Persistent metadata excludes signed sources. No migration or extra Docker service
is needed. Public converter availability and origin limits remain external factors.

## Live workstation evidence

No Spotify account, account cookies, API key, proxy, Telegram send or production
server was used for these tests. Native metadata/search, conversion, complete EOF
consumption and the application's actual FFmpeg audio/duration path were checked.

| Spotify track | Metadata seconds | Resolve seconds | Full stream seconds | Output bytes | Spotify / selected / observed media seconds |
| --- | ---: | ---: | ---: | ---: | --- |
| `0VjIjW4GlUZAMYd2vXMi3b` — Blinding Lights | 0.5929 | 6.1462 | 6.2161 | 3,185,392 | 200 / 200 / 199.080 |
| `4uLU6hMCjMI75M1A2tKUQC` — Never Gonna Give You Up | 0.4503 | 8.0950 | 9.5314 | 3,409,289 | 214 / 214 / 213.072 |
| `7qiZfU4dY1lWllzX7mPBI3` — Shape of You | 0.6290 | 7.2128 | 10.5492 | 3,761,022 | 234 / 235 / 235.056 |

Each stream reached EOF and passed the existing duration tolerance, which permits
at most the greater of one second or 0.5% below the selected duration. Observed
media seconds are FFmpeg progress, not an audio fingerprint. Hashes of the tested
outputs, respectively:

```text
e209e5ed6d1335877d322df0b67bf6e2c89044fc45597c581b64dd0e29397cdd
620ac7e6cc9354f621ec41c57a011d2311c9149a0bd8c6ed76729ae095fd9637
b3eb330b522aef282fe91ec41e3defa020d3471c696776b6067c23e5b7b791ec
```

An earlier loose match accepted a 210-second result for the 214-second Rick Astley
track. That approach was rejected: the strict ten-result search selected a
214-second recording instead. The first application's Blinding Lights resolution
also failed with `spotify_source_unavailable`; the later stricter match and larger
CDN probe budget passed. These changes were combined, so no isolated causal speed
claim is made. The earlier independent 203-second MP3 full read was research
evidence, not the selected recording in the final table.

Reproduce at most five tracks per invocation; results omit credentials and signed
URLs and default to an ignored runtime report:

```bash
python tools/probe_spotify.py --full-stream
python tools/probe_spotify.py --url https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b --full-stream
```

Automated coverage additionally checks parser/URL attacks, wrong/short/altered
recordings, response caps, cancellation/reaping, rate limits, source refresh,
public-metadata restoration, 1,000 concurrent identical successes and failures,
ownership, group replies, captions and actual PostgreSQL/Redis persistence.
Simulated concurrency does not establish 1,000 successful upstream conversions
per second. Full-stream origin success does not certify Telegram publication.

Final full workstation suite: **1,770 passed, zero failures/skips**, using isolated
PostgreSQL 17 and Redis 7.4. Ten existing Windows curl-cffi selector warnings remain.
All 322 Python files passed lint/format checks; schema export stayed unchanged.
The wheel contains all six independent Spotify modules and imports in isolation
without local reference checkouts, credentials, sessions or research caches.

## October 16 delivery follow-up

The reported symptoms were investigated against upstream `f7ef024`. No production
host, live Telegram chat or fresh origin/converter request was used. The operator
clarified that this follow-up should check code only. The original working copy's
uncommitted administration changes were preserved in place; an isolated checkout
started from the latest upstream code.

Two failing workstation regressions established these specific defects:

- A verified cached document could receive `FileReferenceExpiredError` after its
  original message was removed. `resend` attempted refresh, but an absent origin
  raised a generic download error and left the unusable cache in place. The same
  problem occurred when a refreshed reference was explicitly rejected again.
- `FileExpiryService` trusted the rows returned by SQL's clock-based due query.
  If that clock was ahead of the application, it could delete a newly confirmed
  message before the application's 30-second deadline. A simulated early SQL row
  deleted immediately before the correction.

The Telegram adapter now identifies an unusable cached reference with a dedicated
framework-independent error, after explicit rejection or a failed pre-send origin
check. The service invalidates that exact origin in SQL/Redis and performs at most
one repair through the existing content lock. Twenty simultaneous rejected cached
sends share one new transfer; late invalidations cannot remove the new anchor.
Network timeouts, write-permission failures and other ambiguous sends do not cause
another publication. A failed storage-copy resend cannot create an unbounded loop.

Cleanup now requires both `delete_at` and `next_attempt_at` to have elapsed on the
application clock as well as SQL selecting the row. Every confirmed resend retains
its own deadline. The 30-second file-only policy remains intentional: users still
need to save/forward a file before that deadline. This guard addresses database
clock disagreement; it does not claim protection against arbitrary application
clock jumps or Telegram's own chat auto-delete policy.

New pipeline tests generate a four-second MP3 with real FFmpeg, serve it over local
HTTP and exercise the independent Spotify downloader, shared transfer/upload,
publication, persistence, cached resend and deletion adapter. Telegram and metadata
are simulated. Private and supergroup cases cover accepted external registration,
rejected external registration and an incomplete one-second external preview that
must fall back to complete streamed audio. Each publishes once, preserves the
original-link reply and keeps a second send alive when the first reaches 30 seconds.

Initial validation: 61 affected unit/pipeline tests passed. Eight real isolated
PostgreSQL 17/Redis 7.4 tests also passed, including 20 concurrent stale-reference
repairs and cross-cache persistence. The first database attempt failed because the
WSL test containers had stopped when their invocation ended; keeping the WSL
session active and restarting only those disposable containers restored access.
No production configuration or application service was changed.

Final validation: the ordinary full run passed 1,625 cases and skipped 159 for
missing test service URLs. The subsequent database-dependent suite passed all
212 cases with the isolated PostgreSQL/Redis services, including the new repair
case. Together these runs cover all 1,785 current collected tests; overlapping
tests are not counted twice. All 323 Python files passed Ruff lint/format checks,
provider/application architecture checks passed, and `git diff --check` passed.
The ordinary run retained the ten existing Windows curl-cffi selector warnings.
No live Telegram, production-host or fresh upstream-access claim follows.

## October 16 disappearing start status and Radio Edit

The operator's 14:50:17 UTC trace establishes a different failure from the earlier
cache/clock regressions: `spotify_no_matching_audio` at `match_recording`, failed
stage `resolve`, and zero source/upload bytes. Terminal status editing then failed
with `ReplyMarkupInvalidError`, followed by `transfer_status_removed`. Thus the
disappearing item was the starting/progress message; no audio had been published.

Both terminal progress edits and Stop callbacks constructed an empty
`ReplyInlineMarkup`. The installed Telethon client preserves this TL object and
serializes it into `EditMessageRequest`; it does not interpret it as removing the
keyboard. They now pass explicit `buttons=None`. New tests use the actual installed
Telethon `edit_message`, request construction and serialization, simulating rejection
of the previous empty markup for done/error/cancelled outcomes.

The transfer handler also supplies a terminal-text fallback. If an edit cannot be
confirmed, it attempts one reply to the original link within three seconds before
deleting the stale status. A failed/ambiguous fallback reply leaves the original
status and is not retried. No file is resent. An application-level regression now
reproduces the Spotify no-match error and checks visible explanation, absent media
publication, correct reply identity and cleanup ordering.

The reported identity is [Feel So Close - Radio Edit by Calvin Harris](https://open.spotify.com/track/1gihuPhrLraKYrJMAEONyc).
The artist's [official licensed Radio Edit upload](https://www.youtube.com/watch?v=Nh_bdvA8cVM)
is publicly indexed. These page observations establish catalog existence, not
what the production yt-dlp search returned or its CDN accessibility.

The first native workstation attempt returned `spotify_metadata_unavailable`.
A diagnostic invocation repeated that metadata lookup once before an independent
flat search with the public title/artist; metadata again failed and the search
timed out. No candidate list, complete source or Telegram publication was obtained.
Do not repeat these unchanged native probes without new connection evidence.
The raw diagnostic script remains ignored under `.runtime/`.

The provider adds one different artist/title-only query after the original
official-audio query produces a genuine no-match. Both subprocesses share the
original search deadline and remain bounded/reaped on timeout or cancellation.
Artist, full title, version markers and the two-second duration bound are unchanged;
an extended mix cannot substitute for a radio edit. Simulated catalog fixtures
cover the reported title, a rejected long music video, a matching radio edit,
wrong-version rejection, exactly two failed queries and second-worker cleanup.
This improves bounded search coverage; actual recovery of this exact track remains
unverified on the operator's connection.

Validation: 411 affected progress, Stop, Spotify, handler, presentation, file-expiry,
ownership, concurrency, architecture and code-language tests passed with zero skips.
All 323 Python files passed Ruff lint/format checks. No dependency, migration,
production session or file-expiry policy changed, and no real chat was messaged.

## October 16 verified Radio Edit catalog recovery

Started from upstream `8998d1c` in an isolated checkout, preserving the original
working copy's pending administration changes. Unlike the preceding failed access
state, a new Music songs query succeeded on this Windows workstation. That new
connection evidence justified one subsequent metadata/general-video comparison.
No Spotify account, account cookies, proxy or production connection was used.

The reported Spotify identity `1gihuPhrLraKYrJMAEONyc` returned **Feel So Close -
Radio Edit**, **Calvin Harris**, rounded duration **206 seconds**. The first general
video query returned Radio Edit uploads at **209 seconds**, outside the existing
two-second guard, and music videos at **247/249 seconds**. Its **208-second** lyric
upload lacked the Radio Edit title marker. Those concrete candidates correctly
fail the existing matcher; another general query alone cannot guarantee coverage.
This is a workstation reproduction, not the operator's production candidate trace.

The Music songs catalog returned the official audio identity `Nh_bdvA8cVM`, title
**Feel So Close (Radio Edit)**, a typed **Calvin Harris** artist endpoint and displayed
duration **3:28**. The installed pinned extractor's flat Music search returned that
same ID/title but omitted both artist and duration; feeding those flat results to
the matcher cannot work. Do not infer missing identity/duration from the query,
relax the duration/version guard, or repeat that flat approach unchanged.

The Spotify-owned client now reads one bounded Music songs response before the
existing video search paths. Its parser accepts only song shelves, audio-track
watch endpoints with agreeing IDs, typed artist links and an explicit duration.
Album text, query text and play counts cannot supply missing artist/duration.
Parsing stops at ten candidates / 5,000 visited nodes; HTTP response size remains
capped at 1 MiB. All matching rules, the original search deadline, two resolution
slots, rate-limit cooldown and existing video-worker cleanup remain in force.
An unavailable/schema-changed catalog falls back to the existing video queries;
a search HTTP 429 stops further attempts. All implementation remains within the
six-file Spotify provider, with no new dependency, setting or migration.

The first updated full-stream invocation resolved the source successfully but
could not start FFmpeg because it was absent from PATH. After explicitly selecting
the already-installed imageio-ffmpeg binary, one corrected invocation completed:

| Stage | Workstation result |
| --- | --- |
| Spotify metadata | 3.4931 seconds; 206-second track |
| Match, conversion and source probe | 8.0771 seconds; 208-second selected recording |
| Complete application audio stream | 5.7285 seconds; **3,326,802 bytes**, EOF and duration validation passed |
| Observed media duration | **207.912 seconds** |
| Total | 17.2988 seconds |
| Output SHA-256 | `5e5573fffd56e3dde4f6da85fd213b68fe0c7728d29c61d08d4c6a7d2d38f451` |

This checks the exact submitted track through the actual provider and generic
FFmpeg audio transfer path. It does not establish bit-identical Spotify audio,
Telegram publication or production deployment. Signed URLs and private reports
remain ignored under `.runtime/`. Retry a later failed route only after changed
connection/session/schema/cooldown evidence, rather than repeating it in a loop.

Validation: **345 affected tests passed, zero skips**, covering song-schema replay,
the reported general-video rejection and catalog match, complete provider source
resolution, rejected artists/versions/previews, malformed endpoints, candidate and
response bounds, catalog failure fallback, shared search deadline, HTTP 429,
subprocess cancellation/reaping, actual-FFmpeg simulated delivery, ownership,
inspection replies, transfer statuses, concurrency and provider/application
architecture. Lint/format checks passed for the changed Python files.
