# Spotify tracks: matched audio and workstation verification

October 16, 2026. This provider accepts individual public Spotify **track** URLs.
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
- Search reads at most ten yt-dlp flat results. The title and primary artist must
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
remain shared. Direct YouTube links keep their existing main-owner restriction.

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
