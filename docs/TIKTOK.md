# TikTok provider and speed investigation

Implemented October 15, 2026. TikTok follows the same six-file independent provider
boundary as every existing site: `client.py`, `parser.py`, `downloader.py`,
`handler.py`, `urls.py`, and `__init__.py`. It implements `Downloader` directly and
imports no other provider's extractor, authentication, transport or HLS code.

## Research and method choice

The following primary sources were reviewed. Their claims are not automatically
accepted as performance evidence for this installation:

| Source | Relevant approach | Decision |
| --- | --- | --- |
| [yt-dlp TikTok extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/tiktok.py) | Universal/SIGI page state, native format metadata and application endpoints; handling of unsupported/video-only formats | Implement a small independent public-page fallback; avoid launching a process for each ordinary request |
| [DavidTeather TikTok-Api](https://github.com/davidteather/TikTok-Api/blob/main/TikTokApi/tiktok.py) | Playwright browser sessions and session recovery | Browser startup and maintenance are unnecessary for the working anonymous metadata route; no new browser dependency |
| [Tikorgzo](https://github.com/Scoofszlo/Tikorgzo) and its [TikWM extractor](https://github.com/Scoofszlo/Tikorgzo/blob/main/src/tikorgzo/core/extractors/tikwm/extractor.py) | Separate direct and TikWM extractors; its TikWM integration uses browser automation | Test a direct asynchronous TikWM form request first; this worked without a browser in the measured workstation state |
| [TikWM Python example](https://github.com/yasiir564/tikwm/blob/main/app.py) | Form endpoint with public URL and HD flag | Use the compatible endpoint, bounded asynchronous HTTP and safe error handling; do not import its synchronous application |
| [tt-dlp](https://github.com/bajaam/tt-dlp) | Standalone downloader with queue/profile/photo workflows | Reuse architectural observations rather than adding a second download/queue system |

The provider is implemented locally; these applications are not installed or run
inside the bot. Existing HTTPX and generic transfer infrastructure are sufficient.
TikWM is an external anonymous resolver, independent of this repository; its
availability and access to individual posts remain external dependencies.

## Fast path

1. Validate a real TikTok post/share host and strip tracking query/fragment data.
2. Send one asynchronous form request with the public URL and `hd=1`.
3. Require successful structured data and verify the numeric post ID against the
   submitted canonical identity. Never replace a known post with another result.
4. Build the available HD/standard video, ordered photo and soundtrack choices
   from the same response. No per-quality CDN probe or FFmpeg/browser launch delays
   the quality menu. Declared HD is labeled HD; unmeasured pixel dimensions/codecs
   are not invented. A watermark-only fallback is explicitly labeled.
5. Keep bounded source metadata for **45 seconds**, at most **128 post entries**
   and 128 aliases. Equal simultaneous requests share one producer. Different
   posts have independent locks and four bounded metadata slots.
6. A quality click uses those fresh source URLs immediately. Expired/evicted or
   restored durable menus refresh only the same post and require the exact selected
   quality key; a missing HD/photo asset cannot silently become another choice.

The generic service also provides its existing metadata single-flight/cache,
SQL catalog fallback and reusable Telegram-file cache. Signed endpoints are removed
from durable menu/catalog snapshots. Photo keys use stable asset paths rather than
carousel positions, so reordering, CDN host rotation and renewed signatures cannot
assign an old cached photo to a different slide.

All byte limits, full media validation, previews, classic faststart preparation and
bounded Telegram uploading remain in generic transfer infrastructure. Discovery
success alone does not certify CDN availability or a complete download. Complete
video consumption verifies duration, audio presence when advertised, and byte size
when supplied before publication.

## Inputs and fallback

Supported input shapes include full `/@author/video/<id>` and
`/@author/photo/<id>` posts; `m.tiktok.com`, `vm.tiktok.com`, `vt.tiktok.com` and
`tiktok.com/t/<token>` shares; and `/embed/v2/<id>` / `/player/v1/<id>` identities.
Profile/live/Douyin URLs are outside this provider's post contract. Private posts
cannot be authenticated through the anonymous resolver.

The public TikTok page is an independent fallback. It follows at most five
validated TikTok redirect hops and reads bounded Universal/SIGI structured data.
Only complete progressive video candidates are exposed; unsupported VVC and known
video-only HVC1 paths are excluded. A known post redirecting to a different ID is
rejected before fetching that other post.

If a resolver rejects a short token, a valid canonical identity obtained from
TikTok's redirect permits one request using that new canonical input. This is not
a loop retrying the rejected token. It shares the native attempt's existing slot
and deadline. It requires access to the actual TikTok short-link host.

Each metadata method has a default **6-second** timeout including its capacity
wait. The whole provider operation, including its identity lock, is bounded by
`2 * timeout + 1` seconds. Response bodies are capped at 2 MiB for resolver JSON
and 5 MiB for native HTML. Cancellation closes responses and releases capacity.
HTTP 429 sets a bounded resolver cooldown from `Retry-After`; failures do not start
an unbounded retry loop. A failed native network attempt has a 30-second cooldown.

Resolver HTTP and native TikTok HTTP use separate owned sessions. Resolver requests
strip authorization/cookie headers and contain only the normalized public URL/HD
flag. The native/CDN proxy is explicit; ambient workstation proxy/cookies are not
copied into those sessions. Source headers contain public browser headers/referer,
and generic CDN transfer uses the selected source's explicit egress profile.

## Telegram integration

TikTok uses callback prefix `tt:` and the same request ownership and quota checks
as other public providers. Every accepted link receives an inspection acknowledgment
which is edited into the quality menu. The chosen file replies to the original
submitted link in the same private chat/group. Captions include TikTok attribution
and the existing save-before-deletion notice. Confirmed user-facing files are
deleted after 30 seconds by the existing durable expiry worker; status/menu/input
messages and storage-channel copies retain their existing lifecycle.

No database migration or new dependency is needed: metadata/history/file tables
already use a provider-scoped identity, and all provider HTTP pools close with
the container. TikTok is available to ordinary users; the temporary YouTube-only
administrator gate is unchanged.

## Configuration and reproduction

Existing installations work with these defaults; no `.env` modification is required:

```dotenv
TIKTOK_RESOLVER_URL=https://www.tikwm.com/api/
TIKTOK_METADATA_TIMEOUT_SECONDS=6
TIKTOK_PROXY=
```

An empty resolver value selects native public-page extraction only. A custom
resolver must be a public HTTPS endpoint implementing the same JSON contract.
The timeout accepts finite numbers from 1 through 20 seconds. The optional HTTP
proxy is for native TikTok access and CDN transfer. Do not paste credentials into
source, reports or chat.

After the normal pull/reinstall/restart, TikTok appears in `/start` and the sites
help. For a credential-safe origin/full-stream check without sending chat messages:

```bash
python tools/audit_providers.py --sites tiktok --all-qualities --stream --strict \
  --case-timeout 90 --transfer-timeout 45 --max-stream-mib 64 \
  --output .runtime/tiktok-audit.json
```

Add `--ffmpeg /path/to/ffmpeg` if FFmpeg is not on PATH. No Telegram RPC is made
unless the separate explicit `--telegram` option is used.

## Workstation evidence, October 15

Measured on the Windows workstation with Python 3.14.5 and HTTPX 0.28.1, with
anonymous/direct network sessions. This is not production latency or a throughput
SLA. No real Telegram message was sent.

| Case | Observation |
| --- | --- |
| Public post `6718335390845095173` | Provider cold inspection 1.5604 s; three choices; first selection 0.000073 s; 100 warm inspections 0.005919 s; only one metadata request across all these operations |
| Public post `6742501081818877190` | Independent cold inspection 1.629 s; three choices |
| First public post, HD | Full 2,004,627-byte video consumed; verified duration 10.467 s; streaming consumption 3.317 s |
| First public post, standard | Full 2,953,029-byte video consumed; verified duration 10.467 s; streaming consumption 4.187 s |
| First public post, soundtrack | Full normalized 86,770-byte stream consumed; verified duration 10.841 s; streaming consumption 0.711 s |
| Native connection to first post | Plain HTTP attempt timed out at 12.131 s during initial research; a separate browser-TLS attempt timed out at 5.047 s. Native extraction is not verified live from this egress. |
| Public-reference post `6748451240264420610` | Resolver rejected the input; native access timed out. The available evidence does not distinguish deletion/privacy from upstream access failure. |
| Reference short tokens `ZTR45GpSF` and `ZGdm3K8rk` | Resolver returned its URL-parsing rejection and local direct TikTok access timed out. Do not claim these old tokens were downloaded. Short canonical recovery is verified with controlled HTTP responses; production short-link acceptance needs its actual egress. |

The initial raw HTTP metadata measurement for the first post was 2.364 s. Its CDN
range responses were valid 206 MP4/audio responses. Those probes are investigation
evidence and are deliberately not part of the provider's per-request menu path.
Full-stream verification was subsequently performed through the application's
actual integrity/transfer pipeline. No complete media file or signed CDN URL is
published in this documentation; private raw evidence stays under ignored runtime
and research directories.

Do not repeat the rejected inputs under unchanged conditions. Retry after a new
working/current URL, corrected upstream parsing, cooldown expiry, a verified egress
change or other meaningful evidence. A successful supported public post does not
prove every old/private/geoblocked input is accessible.

## Final validation

The final complete workstation suite passed **1,409 tests with zero skips**, using
dedicated real PostgreSQL 16 and Redis instances. Tests cover independent provider
boundaries, URL/identity rejection, native state variants, source refresh after
expiry/restart, photo reordering, single-flight/cancellation, 429 cooldown, bounded
responses, canonical share recovery, Telegram menu ownership, original-message
replies, captions and file-only expiry. An additional real-FFmpeg/Telegram-mock
delivery case checks that TikTok's undeclared codec/dimensions still produce a
complete video with a valid preview, rejecting a short external-fetch result.

Ruff lint and formatting passed for 251 Python files. The existing ten Windows
curl-cffi selector warnings remain. The built wheel includes all six TikTok files,
excludes environment/session/log files and imports in an isolated process. The
existing database schema reference remains unchanged and passes its drift check.
No production login/deployment or real Telegram send was performed.
