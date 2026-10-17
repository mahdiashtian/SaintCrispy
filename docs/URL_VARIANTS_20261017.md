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

The new runtime changes are in SoundCloud, YouTube, Instagram and TikTok. Spotify,
Pinterest, XVideos and XNXX already supported the audited forms and needed no
extractor changes. This is an audit of supported individual-content links; it
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

## Local regression validation

The combined URL matrix, all eight provider downloader regressions, SoundCloud
client/parser tests, source security, provider identity/access/isolation,
application architecture and code-language checks passed: **1,110 tests, zero
failures or skips**. There was one existing curl-cffi warning about Windows'
Proactor event loop using its selector-thread fallback. Ruff lint and formatting
checks passed for the affected provider code and new tests. These are workstation
results; no CI or production success is inferred from them.

## Primary references

- [SoundCloud official widget API](https://developers.soundcloud.com/docs/api/html5-widget)
  and [official older HTTP-target example](https://developers.soundcloud.com/blog/html5-widget-api/).
- [yt-dlp SoundCloud extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/soundcloud.py),
  [YouTube extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/youtube/_video.py),
  [Instagram extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/instagram.py)
  and [TikTok extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/tiktok.py),
  inspected October 17 for established URL variants. They informed the provider-local
  parsing rules; no concrete implementation is imported across providers.
