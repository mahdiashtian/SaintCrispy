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
