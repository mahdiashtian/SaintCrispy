# Provider URL variants: October 17, 2026

## Report and verified cause

The reported input was
`https://m.soundcloud.com/amin-bakhshi-372445912/gdaal-sarneveshtslowed-reverb`.
It failed with `soundcloud_client_id_unavailable` on the bot. Anonymous workstation
HTTPX checks reproduced an important difference: both URLs returned HTTP 200, but
the mobile page had no desktop hydration and used mobile script assets. The
desktop equivalent contained hydration and the desktop bootstrap assets. A 200
response from the mobile site therefore did not establish usable playback metadata.

SoundCloud now normalizes mobile/www permalinks to HTTPS `soundcloud.com` before
requesting HTML, including after a short-link redirect. Public bootstrap discovery
accepts HTML script attributes and JavaScript client-ID assignments with either
quote style. If a supplied track page lacks bootstrap data, it makes at most one
desktop homepage lookup. Only the provider's trusted desktop asset origin is
requested; asset redirects are not followed, duplicate assets are not retried, and
HTTP 429 stops discovery. Sessions and optional OAuth credentials remain owned by
SoundCloud.

## Input coverage

Every provider retains its own URL implementation and identity rules. The audit
tests pass alternate forms through the application's actual URL extractors and
router, then compare the owning provider's content identity.

| Provider | Forms covered by the URL audit and provider regressions |
| --- | --- |
| SoundCloud | Desktop, www/mobile, `on.soundcloud.com`, widget `/player/?url=...`, numeric `api.soundcloud.com` / `api-v2.soundcloud.com` track targets, encoded track URNs |
| YouTube | Desktop/mobile/music/nocookie and `youtu.be` hosts in the existing allowlist; watch, shorts, live, embed, v/e, legacy watch/root-fragment forms; attribution, redirect and oEmbed wrappers |
| Instagram | Existing allowed hosts, p/reel/reels/tv and username prefixes, post embed/captioned forms; existing share and authenticated story flows remain covered |
| TikTok | Existing desktop/mobile/short hosts, video/photo posts, share/video, embed, embed/v2 and player/v1 forms |
| Spotify | Existing open/play hosts, locale and embed track paths, spotify.link/spoti.fi shares |
| Pinterest | Existing country/locale hosts, numeric and descriptive slug pin paths, sent links, pin.it shares |
| XVideos | Existing xvideos/xvideos2/localized host allowlist, video dot/hyphen/numeric paths, embedframe and quickies |
| XNXX | Existing xnxx/xnxx3 host allowlist, dot/hyphen/plain video paths, titleless links |

The first pass's runtime changes were in SoundCloud, YouTube, Instagram and TikTok. Spotify,
Pinterest, XVideos and XNXX already supported the audited forms and needed no
extractor changes at that point. The deeper follow-up below adds further inputs.
This is an audit of supported individual-content links; it
does not add collection, channel, playlist or arbitrary-domain support.

SoundCloud widgets unwrap only into validated provider URLs. Numeric API targets
use SoundCloud's own API client, verify the returned track ID, and preserve a
supplied private-track token only where needed for access. Fallback media page
URLs omit the API query. Private tokens remain significant for temporary alias
keys. YouTube wrappers are bounded and revalidate every nested destination.
Tests reject lookalike domains, credentials, unsupported ports and foreign nested
targets before making requests. Expanding accepted paths does not expand a
provider's credential/session boundary.

## Workstation origin evidence

The exact reported mobile URL passed anonymous inspection and resolution with the
changed provider: track ID `1595188473`, title
`Gdaal-sarnevesht(slowed & reverb )`, duration 294 seconds, available `aac_96` HLS.
Inspection plus resolution took 5.201 seconds in this single workstation sample.

A separate bounded check consumed the complete selected stream through the real
`bot.transfers.streaming.media_chunks` path, including FFmpeg and application
duration/integrity checks: **3,625,826 bytes to EOF in 28.83 seconds**. The bound was
55 seconds / 32 MiB; it was not reached. No Telegram publication, production
restart or production-origin measurement was performed. No full media file,
signed source URL, client ID or credential is stored in this evidence.

One initial standalone diagnostic inadvertently imported the original checkout
through its editable virtualenv installation and repeated the old bootstrap
error. Setting `PYTHONPATH` to the managed worktree root and `src` corrected the
probe; the results above came from the changed code. Future standalone probes
must check their import root when reusing another checkout's virtualenv.

The other seven providers received deterministic unit/regression checks here,
not fresh origin downloads. Earlier documented access failures are not replaced
by this URL audit. In particular, the older DRM-only SoundCloud cases were not
retried: a new public track and a verified mobile/desktop structure difference
justified this investigation. Revisit those older failures only after changed
origin behavior, authorized access or materially different connection evidence.

## Deeper follow-up: app and player aliases

The second review looked beyond combinations of the existing host allowlists.
It found additional established inputs and followed them into real provider
discovery/selection, rather than testing only the extracted ID:

| Provider | Added input | Request behavior |
| --- | --- | --- |
| Spotify | `spotify:track:<id>`, old `embed.spotify.com/?uri=...`, `open.spotify.com/embed?uri=...` with a track URI or validated Spotify URL | Resolve the same 22-character ID and fetch the canonical track metadata page; no request to the old player host |
| Pinterest | `assets.pinterest.com/ext/embed.html?id=<id>`, `in.pinterest.com`, `au.pinterest.com` | Resolve the numeric pin and use the provider's existing canonical API/page discovery |
| TikTok | `www.tiktokv.com` share/embed/post aliases | Normalize to the same `www.tiktok.com` post before the existing metadata path |
| XVideos | `static-hw.xvideos.com/swf/xv-player.swf?id_video=<id>` | Map to the ordinary video page; never fetch or execute a Flash file |
| YouTube | Legacy `youtube.googleapis.com/v/<id>` | Preserve the video ID and use normal canonical extraction |

The review also covered existing Instagram post/reel/story/share and XNXX
mobile/old-path inputs. No additional verified single-item host/path was found
requiring runtime changes there. XNXX mobile/video/xnxx3 variants have extra
discovery-and-selection regressions. Collection or live-recording support is not
implied by accepting another URL spelling for a single item.

Duplicate identity parameters, excessive nested wrappers, malformed native URIs
and unrelated targets are rejected before I/O. Spotify's native URI also passes
the actual group handler/menu test with the original sender ownership. Hosts
remain explicit; accepting an assets host as a wrapper does not allow arbitrary
paths to be fetched from it.

### Fresh anonymous workstation evidence

- Pinterest's official embed HTML and `embed.js` returned HTTP 200; the script
  reads the `id` query parameter. The embed input for pin `2885187256207927`
  discovered 23 qualities. Its selected 234x416 MP4 completed the actual
  `media_chunks` plus `validated_mp4` integrity path: **223,586 bytes to EOF**,
  seven-second expected duration, 10.477 seconds for inspection/selection/transfer.
- TikTok's old share input for post `6718335390845095173` discovered three choices
  and completed the same real transfer/integrity path: **2,004,627 bytes to EOF**,
  ten-second expected duration, 4.684 seconds overall.
- The Spotify native track URI reached the correct canonical embed endpoint but
  failed with `spotify_metadata_unavailable` after 1.510 seconds. A single
  response-structure diagnostic confirmed HTTP 200 with `__NEXT_DATA__`, but
  `pageProps` was an error-shaped page without `state.data.entity`. This is not a
  successful metadata lookup or full download. The legacy player host separately
  returned HTTP 403; the current query-widget shell returned HTTP 200, which does
  not prove track access. Local normalization avoids requesting either widget
  shell. No unchanged download retry, guessed metadata or alternative recording
  was substituted. Revisit after usable origin metadata or changed connection
  evidence; the existing deterministic metadata/matching regressions still pass.

Each full-transfer case had a 45-second / 24-MiB bound and stayed below it. No
cookies, Telegram connection/publication, production access or full-file disk
storage was used. Earlier Instagram/XVideos/XNXX connection failures were not
retried under unchanged conditions. Their input behavior was checked with
deterministic regressions, not claimed as fresh successful origin downloads.

Additional primary references: [Spotify URI specification](https://developer.spotify.com/documentation/web-api/concepts/spotify-uris-ids),
[Spotify iframe API](https://developer.spotify.com/documentation/embeds/references/iframe-api),
the [spotify-uri author's legacy embed formatter](https://github.com/TooTallNate/spotify-uri),
[Pinterest's own embed script](https://assets.pinterest.com/js/embed.js),
and the upstream [XVideos](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/xvideos.py)
and [XNXX](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/xnxx.py)
extractors. The TikTok/YouTube primary sources below establish their older aliases.

## Local regression validation

The combined URL matrix, all eight provider downloader regressions, SoundCloud
client/parser tests, source security, provider identity/access/isolation,
application architecture and code-language checks passed: **1,110 tests, zero
failures or skips**. There was one existing curl-cffi warning about Windows'
Proactor event loop using its selector-thread fallback. Ruff lint and formatting
checks passed for the affected provider code and new tests. These are workstation
results; no CI or production success is inferred from them.

The deeper follow-up's final combined run passed **1,161 tests, zero failures or
skips**, including provider architecture/isolation/security checks and the new
app/widget discovery, selection and handler regressions. The same single Windows
curl warning remained. Affected-code Ruff lint/format and Git whitespace checks
passed. No dependencies, database migration or cross-provider implementation was
added.

## Primary references

- [SoundCloud official widget API](https://developers.soundcloud.com/docs/api/html5-widget)
  and [official older HTTP-target example](https://developers.soundcloud.com/blog/html5-widget-api/).
- [yt-dlp SoundCloud extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/soundcloud.py),
  [YouTube extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/youtube/_video.py),
  [Instagram extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/instagram.py)
  and [TikTok extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/tiktok.py),
  inspected October 17 for established URL variants. They informed the provider-local
  parsing rules; no concrete implementation is imported across providers.
